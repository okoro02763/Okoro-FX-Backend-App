import hashlib
import hmac
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from .models import Payment, Profile, Withdrawal
from .referral import ensure_referral_code

User = get_user_model()

WEBHOOK_SECRET = 'sk_test_unit_test_secret'


def signed_post(client, url, payload, secret=WEBHOOK_SECRET):
    """POST a payload exactly as Paystack would, with a valid raw-body HMAC."""
    body = json.dumps(payload).encode('utf-8')
    signature = hmac.new(secret.encode('utf-8'), body, hashlib.sha512).hexdigest()
    return client.post(
        url,
        data=body,
        content_type='application/json',
        HTTP_X_PAYSTACK_SIGNATURE=signature,
    )


def paystack_verify_ok(reference, amount_kobo):
    return {
        'status': True,
        'message': 'Authorization URL created',
        'data': {
            'reference': reference,
            'status': 'success',
            'amount': amount_kobo,
            'currency': 'NGN',
        },
    }


class PaystackWebhookTests(TestCase):
    """The webhook is the only path that credits real money."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='payer@example.com', email='payer@example.com', password='pw'
        )
        self.profile = Profile.objects.create(user=self.user)
        self.url = reverse('paystack-webhook')
        # 10,000 charged = 1,015 credit + 15 fee, all in naira.
        self.payment = Payment.objects.create(
            user=self.user,
            reference='ref_abc123',
            amount_charged=Decimal('1015.00'),
            fee=Decimal('15.00'),
            credit_amount=Decimal('1000.00'),
            status='pending',
        )

    @override_settings(PAYSTACK_SECRET_KEY=WEBHOOK_SECRET)
    def test_valid_signed_webhook_credits_wallet(self):
        payload = {
            'event': 'charge.success',
            'data': {'reference': 'ref_abc123', 'status': 'success'},
        }
        with mock.patch(
            'forex.account_views.requests.get',
            return_value=mock.Mock(
                status_code=200,
                json=lambda: paystack_verify_ok('ref_abc123', 101500),
                raise_for_status=lambda: None,
            ),
        ):
            resp = signed_post(self.client, self.url, payload)

        self.assertEqual(resp.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.available_balance, Decimal('1000.00'))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, 'success')

    @override_settings(PAYSTACK_SECRET_KEY=WEBHOOK_SECRET)
    def test_unsigned_request_is_rejected(self):
        payload = {
            'event': 'charge.success',
            'data': {'reference': 'ref_abc123', 'status': 'success'},
        }
        resp = self.client.post(
            self.url, data=json.dumps(payload), content_type='application/json'
        )
        self.assertEqual(resp.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.available_balance, Decimal('0.00'))

    @override_settings(PAYSTACK_SECRET_KEY=WEBHOOK_SECRET)
    def test_wrong_secret_signature_is_rejected(self):
        payload = {
            'event': 'charge.success',
            'data': {'reference': 'ref_abc123', 'status': 'success'},
        }
        resp = signed_post(self.client, self.url, payload, secret='attacker_secret')
        self.assertEqual(resp.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.available_balance, Decimal('0.00'))

    @override_settings(PAYSTACK_SECRET_KEY='')
    def test_missing_secret_fails_closed(self):
        """An unconfigured secret must never mean 'trust the caller'."""
        payload = {
            'event': 'charge.success',
            'data': {'reference': 'ref_abc123', 'status': 'success'},
        }
        resp = signed_post(self.client, self.url, payload)
        self.assertEqual(resp.status_code, 503)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.available_balance, Decimal('0.00'))

    @override_settings(PAYSTACK_SECRET_KEY=WEBHOOK_SECRET)
    def test_forged_credit_amount_in_payload_is_ignored(self):
        """The credited value comes from the Payment row, not the callback."""
        payload = {
            'event': 'charge.success',
            'data': {
                'reference': 'ref_abc123',
                'status': 'success',
                'metadata': {'user_id': self.user.id, 'credit_amount': '99999999'},
            },
        }
        with mock.patch(
            'forex.account_views.requests.get',
            return_value=mock.Mock(
                status_code=200,
                json=lambda: paystack_verify_ok('ref_abc123', 101500),
                raise_for_status=lambda: None,
            ),
        ):
            signed_post(self.client, self.url, payload)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.available_balance, Decimal('1000.00'))

    @override_settings(PAYSTACK_SECRET_KEY=WEBHOOK_SECRET)
    def test_unknown_reference_is_rejected(self):
        payload = {'event': 'charge.success', 'data': {'reference': 'ref_made_up'}}
        resp = signed_post(self.client, self.url, payload)
        self.assertEqual(resp.status_code, 400)

    @override_settings(PAYSTACK_SECRET_KEY=WEBHOOK_SECRET)
    def test_underpaid_amount_is_rejected(self):
        """Paystack confirming a smaller charge than we recorded must not credit."""
        payload = {
            'event': 'charge.success',
            'data': {'reference': 'ref_abc123', 'status': 'success'},
        }
        with mock.patch(
            'forex.account_views.requests.get',
            return_value=mock.Mock(
                status_code=200,
                json=lambda: paystack_verify_ok('ref_abc123', 100),
                raise_for_status=lambda: None,
            ),
        ):
            resp = signed_post(self.client, self.url, payload)

        self.assertEqual(resp.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.available_balance, Decimal('0.00'))

    @override_settings(PAYSTACK_SECRET_KEY=WEBHOOK_SECRET)
    def test_redelivery_does_not_double_credit(self):
        payload = {
            'event': 'charge.success',
            'data': {'reference': 'ref_abc123', 'status': 'success'},
        }
        with mock.patch(
            'forex.account_views.requests.get',
            return_value=mock.Mock(
                status_code=200,
                json=lambda: paystack_verify_ok('ref_abc123', 101500),
                raise_for_status=lambda: None,
            ),
        ):
            signed_post(self.client, self.url, payload)
            resp = signed_post(self.client, self.url, payload)

        self.assertEqual(resp.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.available_balance, Decimal('1000.00'))

    @override_settings(PAYSTACK_SECRET_KEY=WEBHOOK_SECRET)
    def test_failed_paystack_status_does_not_credit(self):
        payload = {
            'event': 'charge.success',
            'data': {'reference': 'ref_abc123', 'status': 'success'},
        }
        failed = paystack_verify_ok('ref_abc123', 101500)
        failed['data']['status'] = 'failed'
        with mock.patch(
            'forex.account_views.requests.get',
            return_value=mock.Mock(
                status_code=200, json=lambda: failed, raise_for_status=lambda: None
            ),
        ):
            resp = signed_post(self.client, self.url, payload)

        self.assertEqual(resp.status_code, 400)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.available_balance, Decimal('0.00'))


class AuthorizationTests(TestCase):
    """settings.py fails closed; these endpoints must not drift back open."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='trader', email='trader@example.com', password='pw'
        )
        self.token = Token.objects.create(user=self.user)

    def auth(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.token.key}'}

    def test_close_trade_requires_authentication(self):
        resp = self.client.post(reverse('close-trade', args=[1]))
        self.assertEqual(resp.status_code, 401)

    def test_get_trades_requires_authentication(self):
        resp = self.client.get(reverse('trades'))
        self.assertEqual(resp.status_code, 401)

    def test_portfolio_requires_authentication(self):
        resp = self.client.get(reverse('portfolio'))
        self.assertEqual(resp.status_code, 401)

    def test_account_info_requires_admin(self):
        resp = self.client.get(reverse('account-info'), **self.auth())
        self.assertEqual(resp.status_code, 403)

    def test_account_info_blocks_anonymous(self):
        resp = self.client.get(reverse('account-info'))
        self.assertEqual(resp.status_code, 401)

    def test_fraud_scores_requires_admin(self):
        resp = self.client.get(reverse('fraud-users'), **self.auth())
        self.assertEqual(resp.status_code, 403)

    def test_start_bot_requires_admin(self):
        resp = self.client.post(reverse('bot-start'), **self.auth())
        self.assertEqual(resp.status_code, 403)

    def test_strategy_write_requires_admin(self):
        resp = self.client.put(
            reverse('strategy'),
            data=json.dumps({'strategy': 'sniper'}),
            content_type='application/json',
            **self.auth(),
        )
        self.assertEqual(resp.status_code, 403)

    def test_market_data_remains_public(self):
        """The landing page reads prices before signing in."""
        for name in ('currency-pairs', 'live-prices'):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)


class WithdrawalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='withdrawer', email='w@example.com', password='pw'
        )
        self.token = Token.objects.create(user=self.user)
        self.profile = Profile.objects.create(
            user=self.user, available_balance=Decimal('5000.00')
        )

    def post(self, amount):
        return self.client.post(
            reverse('account-withdraw'),
            data=json.dumps(
                {
                    'amount': amount,
                    'bankName': 'Test Bank',
                    'accountNumber': '0123456789',
                    'accountName': 'Test User',
                }
            ),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self.token.key}',
        )

    def test_nan_amount_is_rejected_not_500(self):
        self.assertEqual(self.post('NaN').status_code, 400)

    def test_infinite_amount_is_rejected_not_500(self):
        self.assertEqual(self.post('Infinity').status_code, 400)

    def test_overflow_amount_is_rejected_not_500(self):
        self.assertEqual(self.post('1e400').status_code, 400)

    def test_negative_amount_is_rejected(self):
        self.assertEqual(self.post('-500').status_code, 400)

    def test_below_minimum_is_rejected(self):
        self.assertEqual(self.post('10').status_code, 400)

    def test_above_balance_is_rejected(self):
        self.assertEqual(self.post('99999').status_code, 400)

    def test_valid_withdrawal_holds_funds(self):
        resp = self.post('2000')
        self.assertEqual(resp.status_code, 201)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.available_balance, Decimal('3000.00'))
        w = Withdrawal.objects.get()
        self.assertEqual(w.fee, Decimal('30.00'))
        self.assertEqual(w.net_amount, Decimal('1970.00'))


class FeeTests(TestCase):
    def test_fee_uses_half_up_rounding(self):
        from .account_views import compute_fee

        # 0.005 rounds up to 0.01 under ROUND_HALF_UP, but would round to
        # 0.00 under Python's default banker's rounding.
        self.assertEqual(compute_fee('0.5', '1'), Decimal('0.01'))
        self.assertEqual(compute_fee('1000', '1.5'), Decimal('15.00'))


class ReferralValidatorTests(TestCase):
    """Signup must reject an unknown referral code instead of ignoring it."""

    def setUp(self):
        self.referrer = User.objects.create_user(
            username='ref@example.com',
            email='ref@example.com',
            password='pw12345',
            first_name='Ref',
        )
        self.profile = Profile.objects.create(user=self.referrer)
        ensure_referral_code(self.profile)

    def _signup(self, email, code=None):
        body = {'name': 'A', 'email': email, 'password': 'pw12345'}
        if code is not None:
            body['referralCode'] = code
        return self.client.post(
            reverse('auth-signup'),
            data=json.dumps(body),
            content_type='application/json',
        )

    def test_signup_rejects_unknown_referral_code(self):
        resp = self._signup('a@example.com', 'NOPE1234')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['message'], 'Invalid referral code.')
        self.assertFalse(User.objects.filter(email='a@example.com').exists())

    def test_signup_accepts_valid_referral_code(self):
        resp = self._signup('b@example.com', self.profile.referral_code.lower())
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(email='b@example.com')
        self.assertEqual(user.profile.reffered_by_id, self.referrer.id)

    def test_validate_endpoint(self):
        bad = self.client.get(reverse('auth-validate-referral'), {'code': 'NOPE1234'})
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(bad.json()['valid'])

        good = self.client.get(
            reverse('auth-validate-referral'), {'code': self.profile.referral_code}
        )
        self.assertTrue(good.json()['valid'])
        self.assertEqual(good.json()['referrer'], 'Ref')


class SignupRoleTests(TestCase):
    """Signing up must never grant admin -- admins come from invites only."""

    def test_first_signup_is_a_regular_user(self):
        self.assertEqual(User.objects.count(), 0)
        resp = self.client.post(
            reverse('auth-signup'),
            data=json.dumps(
                {'name': 'First', 'email': 'first@example.com', 'password': 'pw12345'}
            ),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['user']['role'], 'user')
        self.assertEqual(
            User.objects.get(email='first@example.com').profile.role, 'user'
        )

    def test_set_role_command_promotes_and_demotes(self):
        user = User.objects.create_user(
            username='cmd@example.com',
            email='cmd@example.com',
            password='pw12345',
        )
        Profile.objects.create(user=user)

        call_command('set_role', 'cmd@example.com', 'admin')
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.role, 'admin')

        call_command('set_role', 'cmd@example.com', 'user')
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.role, 'user')

