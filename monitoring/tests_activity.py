"""Tests for activity alerts: scrubbing, cooldown, on_commit, cron endpoint."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from monitoring import activity, discord

WEBHOOK = 'https://discord.com/api/webhooks/123456789/test-token'
ACTIVITY_WEBHOOK = 'https://discord.com/api/webhooks/987654321/activity-token'


class ActivityEnvMixin:
    def _patch_env(self):
        patcher = mock.patch.dict(
            os.environ,
            {
                'DISCORD_WEBHOOK_URL': WEBHOOK,
                'DISCORD_ACTIVITY_WEBHOOK_URL': ACTIVITY_WEBHOOK,
                'DISCORD_ALERTS_ENABLED': 'true',
                'APP_ENV': 'test',
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def setUp(self):
        super().setUp()
        self._patch_env()
        discord.reset_state()
        activity._cooldowns.clear()
        cache.clear()
        self.addCleanup(cache.clear)


class ActivityScrubbingTests(ActivityEnvMixin, SimpleTestCase):
    def test_activity_message_scrubs_email_bearer_and_11_digit_number(self):
        with mock.patch.object(discord, '_enqueue', return_value=True) as enqueue:
            accepted = activity.send_activity(
                'KYC error for person@example.com with Bearer abc123 and NIN 12345678901',
                level='warning',
                fields={'detail': 'email person@example.com token abc123 number 12345678901'},
            )

        self.assertTrue(accepted)
        payload, url = enqueue.call_args[0]
        body = json.dumps(payload)
        self.assertEqual(url, ACTIVITY_WEBHOOK)
        for leaked in ('person@example.com', 'abc123', '12345678901'):
            self.assertNotIn(leaked, body)
        self.assertIn('[REDACTED', body)

    def test_activity_uses_activity_webhook_not_error_webhook(self):
        with mock.patch.object(discord, '_enqueue', return_value=True) as enqueue:
            activity.send_activity('New user signed up', level='info', user_id=7)
        self.assertEqual(enqueue.call_args[0][1], ACTIVITY_WEBHOOK)


class ActivityCooldownTests(ActivityEnvMixin, SimpleTestCase):
    def test_repeats_are_suppressed_until_cooldown_expires(self):
        clock = [1000.0]
        with mock.patch.object(activity, '_now', side_effect=lambda: clock[0]):
            with mock.patch.object(discord, '_enqueue', return_value=True) as enqueue:
                first = activity.send_activity(
                    'Price feed fallback',
                    level='warning',
                    dedupe_key='price-fallback:EURUSD',
                    cooldown_seconds=1800,
                )
                second = activity.send_activity(
                    'Price feed fallback',
                    level='warning',
                    dedupe_key='price-fallback:EURUSD',
                    cooldown_seconds=1800,
                )
                clock[0] += 1801
                third = activity.send_activity(
                    'Price feed fallback',
                    level='warning',
                    dedupe_key='price-fallback:EURUSD',
                    cooldown_seconds=1800,
                )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertTrue(third)
        self.assertEqual(enqueue.call_count, 2)


class OnCommitTests(ActivityEnvMixin, TestCase):
    def test_send_activity_on_commit_fires_after_commit(self):
        with mock.patch.object(discord, '_enqueue', return_value=True) as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                activity.send_activity_on_commit('Committed event', level='info')
        enqueue.assert_called_once()

    def test_send_activity_on_commit_is_dropped_on_rollback(self):
        with mock.patch.object(discord, '_enqueue', return_value=True) as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    from django.db import transaction as db_transaction

                    with db_transaction.atomic():
                        activity.send_activity_on_commit('Rolled-back event', level='info')
                        raise RuntimeError('rollback')
                except RuntimeError:
                    pass
        enqueue.assert_not_called()


class RunChecksEndpointTests(ActivityEnvMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.url = '/api/monitoring/run-checks/'
        patcher = mock.patch('monitoring.views.call_command', return_value=None)
        self.call_command = patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, secret=None, task=None):
        headers = {}
        if secret is not None:
            headers['HTTP_X_CRON_SECRET'] = secret
        query = f'?task={task}' if task else ''
        return self.client.post(self.url + query, {}, format='json', **headers)

    def test_missing_secret_returns_404(self):
        with mock.patch.dict(os.environ, {'CRON_SECRET': 'top-secret'}, clear=False):
            self.assertEqual(self._post().status_code, 404)
            self.call_command.assert_not_called()

    def test_wrong_secret_returns_404(self):
        with mock.patch.dict(os.environ, {'CRON_SECRET': 'top-secret'}, clear=False):
            self.assertEqual(self._post(secret='wrong').status_code, 404)
            self.call_command.assert_not_called()

    def test_correct_secret_runs_pending_check(self):
        with mock.patch.dict(os.environ, {'CRON_SECRET': 'top-secret'}, clear=False):
            response = self._post(secret='top-secret')
        self.assertEqual(response.status_code, 200)
        self.call_command.assert_called_once()
        self.assertEqual(self.call_command.call_args[0][0], 'check_pending_alerts')

    def test_daily_summary_is_404_when_disabled(self):
        with mock.patch.dict(
            os.environ,
            {'CRON_SECRET': 'top-secret', 'ENABLE_DAILY_SUMMARY': 'false'},
            clear=False,
        ):
            response = self._post(secret='top-secret', task='daily_summary')
        self.assertEqual(response.status_code, 404)
        self.call_command.assert_not_called()

    def test_daily_summary_runs_when_enabled(self):
        with mock.patch.dict(
            os.environ,
            {'CRON_SECRET': 'top-secret', 'ENABLE_DAILY_SUMMARY': 'true'},
            clear=False,
        ):
            response = self._post(secret='top-secret', task='daily_summary')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.call_command.call_args[0][0], 'daily_summary')


class ActivityRobustnessTests(ActivityEnvMixin, SimpleTestCase):
    def test_bad_input_never_raises(self):
        with mock.patch.object(discord, '_enqueue', return_value=True):
            self.assertFalse(activity.send_activity(None))
            self.assertFalse(activity.send_activity('bad fields', fields='not-a-dict'))

    def test_thresholds_are_configurable(self):
        with mock.patch.dict(
            os.environ,
            {
                'ALERT_LARGE_DEPOSIT': '100',
                'ALERT_LARGE_WITHDRAWAL': '200',
                'ALERT_LARGE_TRADE': '300',
            },
            clear=False,
        ):
            self.assertFalse(activity.is_large_deposit('99.99'))
            self.assertTrue(activity.is_large_deposit('100'))
            self.assertFalse(activity.is_large_withdrawal('199.99'))
            self.assertTrue(activity.is_large_withdrawal('200'))
            self.assertFalse(activity.is_large_trade('299.99'))
            self.assertTrue(activity.is_large_trade('300'))


class DailySummaryTests(ActivityEnvMixin, TestCase):
    def test_daily_summary_sends_one_message_with_counts_and_totals(self):
        from django.contrib.auth import get_user_model
        from forex.models import Payment, Withdrawal

        user = get_user_model().objects.create_user(
            username='summary@example.com',
            email='summary@example.com',
            password='x',
        )
        Payment.objects.create(
            user=user,
            reference='test-ref-summary',
            amount_charged='250.00',
            fee='0.00',
            credit_amount='250.00',
            currency='NGN',
            status='success',
            verified_at=timezone.now(),
        )
        Withdrawal.objects.create(
            user=user,
            amount='100.00',
            fee='0.00',
            net_amount='100.00',
            bank_name='Test Bank',
            account_number='0123456789',
            account_name='Test User',
            status='pending',
        )

        with mock.patch('monitoring.activity.send_activity') as send:
            call_command('daily_summary', stdout=StringIO())

        send.assert_called_once()
        fields = send.call_args.kwargs['fields']
        self.assertGreaterEqual(fields['new_users'], 1)
        self.assertEqual(fields['deposits'], '1 / 250 NGN')
        self.assertEqual(fields['withdrawals'], '1 / 100 NGN')
        self.assertEqual(fields['trades'], 0)


class PendingCheckTests(ActivityEnvMixin, TestCase):
    def test_no_message_when_nothing_is_pending(self):
        with mock.patch('monitoring.activity.send_activity') as send:
            call_command('check_pending_alerts', stdout=StringIO())
        send.assert_not_called()

    def test_one_summary_when_items_are_pending(self):
        from django.contrib.auth import get_user_model
        from forex.models import Withdrawal
        from kyc.models import KYCProfile

        user = get_user_model().objects.create_user(
            username='pending@example.com',
            email='pending@example.com',
            password='x',
        )
        withdrawal = Withdrawal.objects.create(
            user=user,
            amount='600.00',
            fee='9.00',
            net_amount='591.00',
            bank_name='Test Bank',
            account_number='0123456789',
            account_name='Test User',
            status='pending',
        )
        Withdrawal.objects.filter(pk=withdrawal.pk).update(
            created_at=timezone.now() - timedelta(hours=25)
        )
        KYCProfile.objects.create(
            user=user,
            status='pending',
            submitted_at=timezone.now() - timedelta(hours=25),
        )

        with mock.patch('monitoring.activity.send_activity') as send:
            call_command('check_pending_alerts', stdout=StringIO())

        send.assert_called_once()
        title = send.call_args[0][0]
        self.assertIn('1 withdrawals', title)
        self.assertIn('1 KYC reviews', title)
