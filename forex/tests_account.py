"""Tests for ``GET /api/account/`` and the KYC status helpers.

The production incident these guard against: ``GET /api/account/`` returned
500 with ``relation "kyc_kycprofile" does not exist`` because the ``kyc``
app shipped without a migrations package, so the table was never created on
the production database. The account endpoint must degrade to "not verified"
instead of failing, and the underlying error must still reach the logs.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import ProgrammingError
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

from forex.models import Profile
from kyc.models import KYCProfile

User = get_user_model()

MISSING_TABLE = 'relation "kyc_kycprofile" does not exist'


class AccountStatusTests(TestCase):
    """The shape and the content of the account payload."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='trader', email='trader@example.com', password='pw-123456'
        )
        self.token = Token.objects.create(user=self.user)

    def get(self, url='/api/account/'):
        return self.client.get(url, HTTP_AUTHORIZATION=f'Bearer {self.token.key}')

    def test_account_status_requires_authentication(self):
        self.assertEqual(self.client.get('/api/account/').status_code, 401)

    def test_account_status_without_any_kyc_profile(self):
        """No KYCProfile at all: 200 with a "none" verification status."""
        response = self.get()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['verification_status'], 'none')
        self.assertEqual(data['kycStatus'], 'none')
        self.assertFalse(data['can_trade_live'])
        self.assertFalse(data['liveVerified'])
        self.assertEqual(data['kyc'], [])

    def test_account_status_has_the_documented_shape(self):
        data = self.get().json()
        for key in (
            'account_type',
            'balance',
            'currency',
            'verification_status',
            'can_trade_live',
        ):
            self.assertIn(key, data)
        self.assertEqual(data['account_type'], 'demo')
        self.assertEqual(data['currency'], 'NGN')
        # The camelCase keys the deployed frontend already uses stay in sync
        # with the flat ones.
        self.assertEqual(data['accountType'], data['account_type'])
        self.assertEqual(data['availableBalance'], data['balance'])
        self.assertEqual(data['kycStatus'], data['verification_status'])
        self.assertEqual(data['liveVerified'], data['can_trade_live'])

    def test_balance_is_always_a_number_never_null(self):
        Profile.objects.create(
            user=self.user, available_balance=Decimal('1250.75')
        )
        data = self.get().json()
        self.assertIsNotNone(data['balance'])
        self.assertIsInstance(data['balance'], (int, float))
        self.assertEqual(data['balance'], 1250.75)

    def test_balance_of_a_fresh_profile_is_zero_not_null(self):
        data = self.get().json()
        self.assertIsNotNone(data['balance'])
        self.assertEqual(data['balance'], 0)

    def test_unusable_balance_degrades_to_zero(self):
        """A NULL/garbage balance must render as 0, not as a broken value."""
        from forex.account_views import _balance_number

        for value in (None, '', 'abc', float('nan'), float('inf')):
            with self.subTest(value=value):
                self.assertEqual(_balance_number(value), 0.0)

    def test_verification_status_for_every_kyc_status(self):
        cases = {
            'none': 'none',
            'pending': 'pending',
            'verified': 'verified',
            'rejected': 'rejected',
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                KYCProfile.objects.update_or_create(
                    user=self.user, defaults={'status': status}
                )
                data = self.get().json()
                self.assertEqual(data['verification_status'], expected)
                self.assertEqual(data['kycStatus'], expected)
                self.assertEqual(
                    data['can_trade_live'], status == 'verified'
                )
                self.assertEqual(data['liveVerified'], status == 'verified')

    def test_legacy_approved_document_still_counts_as_verified(self):
        """The old single-document flow must keep granting live access."""
        from forex.models import IdentityVerification

        IdentityVerification.objects.create(
            user=self.user,
            doc_type='nin',
            doc_number='12345678901',
            status='approved',
        )
        data = self.get().json()
        self.assertEqual(data['verification_status'], 'verified')
        self.assertTrue(data['can_trade_live'])


class AccountStatusSurvivesMissingKycTableTests(TestCase):
    """The exact production failure must no longer break the account page."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='trader', email='trader@example.com', password='pw-123456'
        )
        self.token = Token.objects.create(user=self.user)

    def get(self):
        return self.client.get(
            '/api/account/', HTTP_AUTHORIZATION=f'Bearer {self.token.key}'
        )

    def test_account_status_survives_a_missing_kyc_table(self):
        with mock.patch(
            'kyc.services.KYCProfile.objects.filter',
            side_effect=ProgrammingError(MISSING_TABLE),
        ):
            with self.assertLogs('kyc', level='ERROR'):
                response = self.get()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['verification_status'], 'none')
        self.assertFalse(data['can_trade_live'])

    def test_the_error_is_logged_not_swallowed(self):
        """A degraded response must never hide the root cause."""
        with mock.patch(
            'kyc.services.KYCProfile.objects.filter',
            side_effect=ProgrammingError(MISSING_TABLE),
        ):
            with self.assertLogs('kyc', level='ERROR') as logs:
                self.get()
        self.assertTrue(
            any(MISSING_TABLE in line for line in logs.output),
            logs.output,
        )

    def test_services_fail_closed_when_the_table_is_missing(self):
        from kyc.services import current_status, user_is_verified

        with mock.patch(
            'kyc.services.KYCProfile.objects.filter',
            side_effect=ProgrammingError(MISSING_TABLE),
        ):
            with self.assertLogs('kyc', level='ERROR'):
                self.assertFalse(user_is_verified(self.user))
                self.assertEqual(current_status(self.user), 'none')


class HealthEndpointTests(TestCase):
    """The readiness probe must answer with and without a trailing slash."""

    def test_health_answers_with_a_trailing_slash(self):
        response = self.client.get('/api/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')

    def test_health_answers_without_a_trailing_slash(self):
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')
