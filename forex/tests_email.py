"""Tests for the OKORO FX reusable email system.

No real email is ever delivered: DOM content is rendered locally, the
transport is forced onto Django's locmem backend, and every Termii HTTP call
is mocked. Run just these with::

    python manage.py test forex.tests_email
"""
import hashlib
import hmac
import json
import os
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .email_backend import TermiiEmailBackend, TermiiEmailError
from .email_service import EMAILS, build_template_message, send_template_email
from .models import PasswordResetToken, Payment, Profile

User = get_user_model()

LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'

# Context that satisfies every template's variables.
SAMPLE_CONTEXT = {
    'name': 'Ada',
    'action_url': 'https://okorofx.example.com/action?token=ACTIONTOKEN',
    'action_label': 'Continue',
    'amount': '1000.00',
    'currency': 'NGN',
    'reference': 'REF-123',
    'status': 'success',
    'net_amount': '985.00',
    'symbol': 'EUR/USD',
    'trade_type': 'BUY',
    'quantity': '1.00',
    'entry_price': '1.08050',
    'exit_price': '1.08100',
    'profit_loss': '0.50',
    'status_note': 'Reviewed and approved.',
    'ip_address': '127.0.0.1',
    'user_agent': 'Unit test agent',
    'inviter': 'Admin',
    'login_time': timezone.now(),
}


def signed_post(client, url, payload, secret):
    """POST a Paystack payload with a valid raw-body HMAC signature."""
    body = json.dumps(payload).encode('utf-8')
    signature = hmac.new(secret.encode('utf-8'), body, hashlib.sha512).hexdigest()
    return client.post(
        url,
        data=body,
        content_type='application/json',
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )


def _response(code='ok', message_id='m1', status_code=200):
    return mock.Mock(
        status_code=status_code,
        raise_for_status=lambda: None,
        json=lambda: {'code': code, 'message_id': message_id},
    )


class TemplateRenderingTests(TestCase):
    """Every registered template renders both an HTML and a text part."""

    def test_every_registered_template_renders_html_and_text(self):
        for name in EMAILS:
            with self.subTest(template=name):
                msg = build_template_message(
                    name, 'user@example.com', SAMPLE_CONTEXT
                )
                self.assertIsNotNone(msg, name)
                self.assertEqual(msg.to, ['user@example.com'])
                self.assertTrue(msg.body.strip(), f'{name} text body is empty')
                htmls = [c for c, ct in msg.alternatives if ct == 'text/html']
                self.assertEqual(len(htmls), 1, f'{name} missing html part')
                self.assertIn('OKORO', htmls[0])

    def test_unknown_template_raises_keyerror(self):
        with self.assertRaises(KeyError):
            build_template_message('not_a_template', 'a@b.com', {})

    def test_missing_recipient_returns_none(self):
        self.assertIsNone(build_template_message('welcome', '', SAMPLE_CONTEXT))
        self.assertIsNone(build_template_message('welcome', [], SAMPLE_CONTEXT))
        self.assertIsNone(send_template_email('welcome', '', SAMPLE_CONTEXT))

    def test_missing_context_falls_back_to_safe_defaults(self):
        msg = build_template_message('welcome', 'a@b.com')
        self.assertIsNotNone(msg)
        self.assertIn('there', msg.body)

    def test_subject_and_recipient_are_correct(self):
        msg = build_template_message(
            'deposit_confirmation', 'ada@example.com', SAMPLE_CONTEXT
        )
        self.assertEqual(msg.subject, 'OKORO FX - Deposit confirmed')
        self.assertEqual(msg.to, ['ada@example.com'])

    def test_subject_can_be_overridden(self):
        msg = build_template_message(
            'welcome', 'a@b.com', SAMPLE_CONTEXT, subject='Custom subject'
        )
        self.assertEqual(msg.subject, 'Custom subject')

    def test_text_alternative_contains_no_html_markup(self):
        msg = build_template_message('welcome', 'a@b.com', SAMPLE_CONTEXT)
        self.assertNotIn('<html', msg.body.lower())
        self.assertNotIn('<a ', msg.body.lower())


class TermiiSeedingTests(TestCase):
    """Only the declared Termii variables leave the app -- never body data."""

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_non_termii_transport_does_not_seed_termii_fields(self):
        msg = build_template_message('password_reset', 'a@b.com', SAMPLE_CONTEXT)
        self.assertIsNone(msg.termii_variables)
        self.assertIsNone(msg.termii_template_id)

    @override_settings(
        EMAIL_BACKEND='forex.email_backend.TermiiEmailBackend',
        TERMII_API_KEY='test-key',
        TERMII_TEMPLATE_ID='tpl-123',
    )
    def test_termii_transport_seeds_only_declared_variables(self):
        msg = build_template_message('password_reset', 'a@b.com', SAMPLE_CONTEXT)
        self.assertEqual(msg.termii_template_id, 'tpl-123')
        self.assertEqual(
            set(msg.termii_variables),
            {'name', 'action_url', 'action_label'},
        )
        # Amounts / references stay inside the rendered {{message}} body.
        self.assertNotIn('amount', msg.termii_variables)
        self.assertNotIn('reference', msg.termii_variables)


class SendTemplateEmailTests(TestCase):
    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_send_places_message_in_outbox(self):
        send_template_email('welcome', 'ada@example.com', {'name': 'Ada'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['ada@example.com'])
        self.assertEqual(mail.outbox[0].subject, 'Welcome to OKORO FX')

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_send_never_raises_when_backend_fails(self):
        with mock.patch(
            'forex.email_service.EmailMultiAlternatives.send',
            side_effect=RuntimeError('smtp down'),
        ):
            result = send_template_email(
                'welcome', 'ada@example.com', {'name': 'Ada'}
            )
        self.assertIsNone(result)

    def test_send_without_transport_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with override_settings(EMAIL_BACKEND=LOCMEM):
                self.assertIsNone(
                    send_template_email('welcome', 'a@b.com', {'name': 'A'})
                )


class TermiiBackendTests(TestCase):
    """The backend speaks only the documented /api/templates/send-email API."""

    def _settings(self, **overrides):
        base = dict(
            TERMII_API_KEY='test-key',
            TERMII_BASE_URL='https://v4.api.termii.com',
            TERMII_EMAIL_CONFIG_ID='cfg-1',
            TERMII_TEMPLATE_ID='tpl-1',
        )
        base.update(overrides)
        return base

    def _message(self):
        msg = EmailMessage(
            subject='Hello', body='Body text', to=['ada@example.com']
        )
        msg.termii_template_id = 'tpl-1'
        msg.termii_variables = {'name': 'Ada'}
        return msg

    def test_posts_documented_payload_and_counts_sent(self):
        with override_settings(**self._settings()):
            backend = TermiiEmailBackend()
        with mock.patch(
            'forex.email_backend.requests.post', return_value=_response()
        ) as post:
            sent = backend.send_messages([self._message()])

        self.assertEqual(sent, 1)
        url = post.call_args.args[0]
        payload = post.call_args.kwargs['json']
        self.assertTrue(url.endswith('/api/templates/send-email'))
        self.assertEqual(payload['api_key'], 'test-key')
        self.assertEqual(payload['email'], 'ada@example.com')
        self.assertEqual(payload['subject'], 'Hello')
        self.assertEqual(payload['email_configuration_id'], 'cfg-1')
        self.assertEqual(payload['template_id'], 'tpl-1')
        # Body is injected under TERMII_BODY_VARIABLE, extras merged in.
        self.assertEqual(
            payload['variables'], {'message': 'Body text', 'name': 'Ada'}
        )

    def test_api_error_code_raises(self):
        with override_settings(**self._settings()):
            backend = TermiiEmailBackend()
        with mock.patch(
            'forex.email_backend.requests.post',
            return_value=_response(code='error'),
        ):
            with self.assertRaises(TermiiEmailError):
                backend.send_messages([self._message()])

    def test_network_error_raises(self):
        with override_settings(**self._settings()):
            backend = TermiiEmailBackend()
        with mock.patch(
            'forex.email_backend.requests.post',
            side_effect=requests.RequestException('boom'),
        ):
            with self.assertRaises(TermiiEmailError):
                backend.send_messages([self._message()])

    def test_fail_silently_swallows_network_error(self):
        with override_settings(**self._settings()):
            backend = TermiiEmailBackend(fail_silently=True)
        with mock.patch(
            'forex.email_backend.requests.post',
            side_effect=requests.RequestException('boom'),
        ):
            self.assertEqual(backend.send_messages([self._message()]), 0)

    def test_missing_api_key_fails_loud(self):
        with override_settings(**self._settings(TERMII_API_KEY='')):
            backend = TermiiEmailBackend()
        with self.assertRaises(ImproperlyConfigured):
            backend.send_messages([self._message()])

    def test_missing_api_key_is_silent_when_asked(self):
        with override_settings(**self._settings(TERMII_API_KEY='')):
            backend = TermiiEmailBackend(fail_silently=True)
        self.assertEqual(backend.send_messages([self._message()]), 0)


class EmailSecurityTests(TestCase):
    """Reset/verification links are carried intact; secrets never leak."""

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_password_reset_email_carries_link_and_no_password(self):
        user = User.objects.create_user(
            username='ada@example.com',
            email='ada@example.com',
            password='SuperSecret123',
            first_name='Ada',
        )
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                reverse('auth-forgot-password'),
                data=json.dumps({'email': 'ada@example.com'}),
                content_type='application/json',
            )

        self.assertEqual(resp.status_code, 200)
        token = PasswordResetToken.objects.get(user=user)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn('/reset-password/', body)
        self.assertIn(token.key, body)
        self.assertNotIn('SuperSecret123', body)
        # The API must not echo the token back to the caller.
        self.assertNotIn(token.key, resp.content.decode())

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_verification_link_preserved_in_html_and_text(self):
        ctx = dict(
            SAMPLE_CONTEXT,
            action_url='https://okorofx.example.com/verify?token=VERIFYTOKEN',
        )
        msg = build_template_message('verify_email', 'a@b.com', ctx)
        html = msg.alternatives[0][0]
        self.assertIn('VERIFYTOKEN', html)
        self.assertIn('VERIFYTOKEN', msg.body)


class WorkflowEmailTests(TestCase):
    """Notifications fire from the real workflows, and only once."""

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_signup_sends_welcome_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                reverse('auth-signup'),
                data=json.dumps(
                    {'name': 'Ada', 'email': 'new@example.com', 'password': 'pw12345'}
                ),
                content_type='application/json',
            )

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['new@example.com'])
        self.assertEqual(mail.outbox[0].subject, 'Welcome to OKORO FX')

    @override_settings(EMAIL_BACKEND=LOCMEM)
    def test_login_sends_login_alert(self):
        user = User.objects.create_user(
            username='ada@example.com',
            email='ada@example.com',
            password='pw12345',
            first_name='Ada',
        )
        Profile.objects.create(user=user)

        resp = self.client.post(
            reverse('auth-login'),
            data=json.dumps({'email': 'ada@example.com', 'password': 'pw12345'}),
            content_type='application/json',
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].subject, 'New sign-in to your OKORO FX account'
        )

    @override_settings(EMAIL_BACKEND=LOCMEM, PAYSTACK_SECRET_KEY='sk_test_secret')
    def test_deposit_confirmation_sent_once_on_webhook_redelivery(self):
        user = User.objects.create_user(
            username='payer@example.com', email='payer@example.com', password='pw'
        )
        Profile.objects.create(user=user, available_balance=Decimal('0.00'))
        Payment.objects.create(
            user=user,
            reference='ref_email_1',
            amount_charged=Decimal('1015.00'),
            fee=Decimal('15.00'),
            credit_amount=Decimal('1000.00'),
            status='pending',
        )
        payload = {
            'event': 'charge.success',
            'data': {'reference': 'ref_email_1', 'status': 'success'},
        }
        verified = {
            'status': True,
            'data': {
                'reference': 'ref_email_1',
                'status': 'success',
                'amount': 101500,
                'currency': 'NGN',
            },
        }
        with mock.patch(
            'forex.account_views.requests.get',
            return_value=mock.Mock(
                status_code=200,
                json=lambda: verified,
                raise_for_status=lambda: None,
            ),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                signed_post(
                    self.client,
                    reverse('paystack-webhook'),
                    payload,
                    'sk_test_secret',
                )
                signed_post(
                    self.client,
                    reverse('paystack-webhook'),
                    payload,
                    'sk_test_secret',
                )

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].subject, 'OKORO FX - Deposit confirmed'
        )
