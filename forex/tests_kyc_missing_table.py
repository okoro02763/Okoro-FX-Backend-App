"""The KYC endpoints must degrade, not 500, when the tables are missing.

Production incident: the ``kyc`` migration had never been applied, so every
call raised ``relation "kyc_kycprofile" does not exist``. ``PUT
/api/kyc/details/`` answered **500**, and the frontend showed the raw error
instead of a retryable message -- even though ``kyc/views.py`` catches
``DatabaseError``. These tests pin down the actual response for each endpoint
so a future regression cannot reintroduce the raw 500.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.db import ProgrammingError
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

User = get_user_model()

MISSING_TABLE = 'relation "kyc_kycprofile" does not exist'


class MissingKYCTableTests(TestCase):
    """Simulate the un-migrated database and assert a friendly response."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='kyc_missing', email='kyc_missing@example.com', password='pw12345',
        )
        self.token, _ = Token.objects.get_or_create(user=self.user)
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.token.key}'}

    def _patch_missing_table(self):
        """Every read/write of the profile table raises the real error."""
        return mock.patch(
            'kyc.services.KYCProfile.objects.get_or_create',
            side_effect=ProgrammingError(MISSING_TABLE),
        )

    def test_put_details_does_not_return_500(self):
        with self._patch_missing_table():
            with self.assertLogs('kyc', level='ERROR'):
                response = self.client.put(
                    reverse('kyc-details'), data='{}',
                    content_type='application/json', **self.auth,
                )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'server_error')

    def test_get_status_does_not_return_500(self):
        with self._patch_missing_table():
            with self.assertLogs('kyc', level='ERROR'):
                response = self.client.get(reverse('kyc-status'), **self.auth)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'server_error')

    def test_put_details_still_requires_auth(self):
        """A missing table must not turn into an auth bypass."""
        response = self.client.put(
            reverse('kyc-details'), data='{}', content_type='application/json',
        )
        self.assertIn(response.status_code, (401, 403))
