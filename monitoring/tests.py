"""Tests for Discord alerting: scrubbing, dedupe, throttling and intake.

Run with::

    python manage.py test monitoring

The suite never performs a network call: ``monitoring.discord._post`` (or the
queue) is always patched, and alerting state is reset between tests.
"""

from __future__ import annotations

import json
import logging
import os
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase
from rest_framework.test import APIClient

from monitoring import config, discord, handlers
from monitoring.scrubbing import scrub_payload, scrub_text

WEBHOOK = 'https://discord.com/api/webhooks/123456789/test-token'
ALLOWED_ORIGIN = 'https://okorofx.netlify.app'


class AlertTestBase(SimpleTestCase):
    def setUp(self):
        discord.reset_state()
        patcher = mock.patch.dict(
            os.environ,
            {
                'DISCORD_WEBHOOK_URL': WEBHOOK,
                'DISCORD_ALERTS_ENABLED': 'true',
                'APP_ENV': 'test',
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class ScrubbingTests(AlertTestBase):
    def test_message_scrubbing_redacts_nin_and_bearer_token(self):
        text = scrub_text(
            'Customer sent Bearer abc123 while NIN 12345678901 failed'
        )
        self.assertNotIn('12345678901', text)
        self.assertNotIn('abc123', text)
        self.assertIn('NIN', text)
        self.assertIn('[REDACTED]', text)

    def test_sensitive_dict_keys_lose_their_values(self):
        cleaned = scrub_payload(
            {
                'password': 'hunter2',
                'document_number': '12345678901',
                'email': 'person@example.com',
                'phone': '08012345678',
                'status': 'ok',
            }
        )
        self.assertEqual(cleaned['password'], '[REDACTED]')
        self.assertEqual(cleaned['document_number'], '[REDACTED]')
        self.assertEqual(cleaned['email'], '[REDACTED]')
        self.assertEqual(cleaned['phone'], '[REDACTED]')
        self.assertEqual(cleaned['status'], 'ok')

    def test_webhook_url_is_never_echoed(self):
        text = scrub_text(f'posting to {WEBHOOK}')
        self.assertNotIn(WEBHOOK, text)
        self.assertIn('[REDACTED_WEBHOOK]', text)

    def test_discord_payload_contains_no_nin_or_bearer_token(self):
        """The requirement: the fake NIN/Bearer in an exception is redacted."""
        with mock.patch.object(discord, '_post', return_value=True) as post:
            accepted = discord.send_discord_alert(
                title='KYC error for NIN 12345678901',
                fields=[
                    (
                        'Detail',
                        'password=hunter2 token abc123 email a@b.com '
                        'phone 08012345678',
                    )
                ],
                description='Customer sent Bearer abc123 with NIN 12345678901',
                level='error',
                fingerprint=None,
                async_send=False,
            )

        self.assertTrue(accepted)
        payload = post.call_args[0][0]
        body = json.dumps(payload)
        for leaked in (
            '12345678901',
            'abc123',
            'hunter2',
            'a@b.com',
            '08012345678',
        ):
            self.assertNotIn(leaked, body)
        self.assertIn('[REDACTED]', body)


class DedupeTests(AlertTestBase):
    def test_same_fingerprint_sent_once_per_window(self):
        clock = [1000.0]
        with mock.patch.object(discord, '_now', side_effect=lambda: clock[0]):
            with mock.patch.object(discord, '_post', return_value=True) as post:
                first = discord.send_discord_alert(
                    'Boom', level='error', fingerprint='fp', async_send=False
                )
                second = discord.send_discord_alert(
                    'Boom', level='error', fingerprint='fp', async_send=False
                )
                clock[0] += discord.DEDUPE_WINDOW_SECONDS + 1
                third = discord.send_discord_alert(
                    'Boom', level='error', fingerprint='fp', async_send=False
                )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertTrue(third)
        self.assertEqual(post.call_count, 2)
        # The alert after the window reports how many were suppressed.
        body = json.dumps(post.call_args_list[1][0][0])
        self.assertIn('occurred 1 more time', body)

    def test_different_fingerprints_are_not_deduplicated(self):
        with mock.patch.object(discord, '_post', return_value=True) as post:
            self.assertTrue(
                discord.send_discord_alert(
                    'A', level='error', fingerprint='a', async_send=False
                )
            )
            self.assertTrue(
                discord.send_discord_alert(
                    'B', level='error', fingerprint='b', async_send=False
                )
            )
        self.assertEqual(post.call_count, 2)


class ThrottleTests(AlertTestBase):
    def test_global_cap_collapses_into_one_summary(self):
        clock = [1000.0]
        with mock.patch.object(discord, '_now', side_effect=lambda: clock[0]):
            with mock.patch.object(discord, '_post', return_value=True) as post:
                for index in range(discord.GLOBAL_MAX_ALERTS):
                    self.assertTrue(
                        discord.send_discord_alert(
                            f'Error {index}',
                            level='error',
                            fingerprint=f'fp-{index}',
                            async_send=False,
                        )
                    )
                # 21st alert is replaced by the throttle summary.
                self.assertTrue(
                    discord.send_discord_alert(
                        'Error over limit',
                        level='error',
                        fingerprint='fp-over',
                        async_send=False,
                    )
                )
                # 22nd is dropped: no second summary inside the same minute.
                self.assertFalse(
                    discord.send_discord_alert(
                        'Error again',
                        level='error',
                        fingerprint='fp-again',
                        async_send=False,
                    )
                )

        self.assertEqual(post.call_count, discord.GLOBAL_MAX_ALERTS + 1)
        summary = json.dumps(post.call_args_list[-1][0][0])
        self.assertIn('Too many errors, alerts throttled', summary)

    def test_window_slides_and_delivery_resumes(self):
        clock = [1000.0]
        with mock.patch.object(discord, '_now', side_effect=lambda: clock[0]):
            with mock.patch.object(discord, '_post', return_value=True) as post:
                for index in range(discord.GLOBAL_MAX_ALERTS):
                    discord.send_discord_alert(
                        f'Error {index}',
                        level='error',
                        fingerprint=f'fp-{index}',
                        async_send=False,
                    )
                clock[0] += discord.GLOBAL_WINDOW_SECONDS + 1
                self.assertTrue(
                    discord.send_discord_alert(
                        'Fresh after the window',
                        level='error',
                        fingerprint='fp-fresh',
                        async_send=False,
                    )
                )
        self.assertEqual(post.call_count, discord.GLOBAL_MAX_ALERTS + 1)


class ConfigTests(SimpleTestCase):
    def test_placeholder_webhook_disables_alerts(self):
        with mock.patch.dict(
            os.environ,
            {'DISCORD_WEBHOOK_URL': 'paste-your-webhook-url-here'},
            clear=False,
        ):
            self.assertFalse(config.alerts_enabled())

    def test_kill_switch_disables_alerts(self):
        with mock.patch.dict(
            os.environ,
            {
                'DISCORD_WEBHOOK_URL': WEBHOOK,
                'DISCORD_ALERTS_ENABLED': 'false',
            },
            clear=False,
        ):
            self.assertFalse(config.alerts_enabled())

    def test_persistent_slot_survives_repeated_calls(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(
                os.environ,
                {'DISCORD_WEBHOOK_URL': WEBHOOK, 'DISCORD_STATE_DIR': directory},
                clear=False,
            ):
                self.assertTrue(discord.claim_persistent_slot('boot', 600))
                self.assertFalse(discord.claim_persistent_slot('boot', 600))


class HandlerTests(AlertTestBase):
    def _logger(self):
        logger = logging.getLogger('monitoring-test-handler')
        logger.handlers = []
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handlers.DiscordLogHandler())
        self.addCleanup(logger.handlers.clear)
        return logger

    def _request(self):
        request = mock.Mock()
        request.path = '/api/kyc/submit/'
        request.method = 'POST'
        request.user = mock.Mock(is_authenticated=True, pk=42)
        request.resolver_match = mock.Mock(view_name='kyc-submit')
        request._monitoring_status = 500
        return request

    def test_handler_builds_and_scrubs_an_error_alert(self):
        logger = self._logger()
        request = self._request()

        with mock.patch.object(discord, '_enqueue', return_value=True) as enqueue:
            try:
                raise ValueError('NIN 12345678901 leaked Bearer abc123')
            except ValueError:
                logger.exception(
                    'KYC submission exploded',
                    extra={'request': request, 'status_code': 500},
                )

        self.assertEqual(enqueue.call_count, 1)
        payload, url = enqueue.call_args[0]
        body = json.dumps(payload)

        self.assertEqual(url, WEBHOOK)
        self.assertIn('/api/kyc/submit/', body)
        self.assertIn('POST', body)
        self.assertIn('kyc-submit', body)
        self.assertIn('ValueError', body)
        self.assertIn('42', body)
        self.assertIn('Traceback (last 15 lines)', body)
        # The exception message and title are scrubbed before delivery.
        self.assertNotIn('12345678901', body)
        self.assertNotIn('abc123', body)

    def test_handler_swallows_delivery_failures(self):
        logger = self._logger()
        with mock.patch(
            'monitoring.handlers.send_discord_alert',
            side_effect=RuntimeError('boom'),
        ):
            try:
                raise ValueError('original error')
            except ValueError:
                # Must not raise out of the logging handler.
                logger.exception('should stay quiet')

    def test_handler_ignores_below_error(self):
        logger = self._logger()
        with mock.patch.object(discord, '_enqueue', return_value=True) as enqueue:
            logger.warning('not an alert')
        enqueue.assert_not_called()


class ClientErrorEndpointTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient()
        self.url = '/api/monitoring/client-error/'
        patcher = mock.patch(
            'monitoring.views.notify_frontend_error', return_value=True
        )
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(cache.clear)

    def _post(self, data=None, origin=ALLOWED_ORIGIN):
        return self.client.post(
            self.url,
            data=data if data is not None else {'message': 'boom'},
            format='json',
            HTTP_ORIGIN=origin,
        )

    def test_accepts_a_well_formed_report(self):
        response = self._post(
            {
                'message': 'TypeError: x is undefined',
                'stack': 'at App (app.js:1)\nat main (main.js:2)',
                'route': '/terminal',
                'userAgent': 'Mozilla/5.0',
                'appVersion': '1.2.3',
            }
        )
        self.assertEqual(response.status_code, 202)
        self.notify.assert_called_once()
        kwargs = self.notify.call_args.kwargs
        self.assertEqual(kwargs['route'], '/terminal')
        self.assertEqual(kwargs['app_version'], '1.2.3')

    def test_rejects_disallowed_origin(self):
        response = self._post(origin='https://evil.example')
        self.assertEqual(response.status_code, 403)
        self.notify.assert_not_called()

    def test_rejects_oversized_body(self):
        response = self._post({'message': 'x' * 9000})
        self.assertEqual(response.status_code, 413)
        self.notify.assert_not_called()

    def test_requires_a_message(self):
        response = self._post({'route': '/terminal'})
        self.assertEqual(response.status_code, 400)

    def test_rate_limited_to_ten_per_minute_per_ip(self):
        statuses = [self._post().status_code for _ in range(11)]
        self.assertEqual(statuses[:10], [202] * 10)
        self.assertEqual(statuses[10], 429)

    def test_test_alert_endpoint_requires_staff(self):
        response = self.client.post('/api/monitoring/test-alert/', {}, format='json')
        self.assertIn(response.status_code, (401, 403))
