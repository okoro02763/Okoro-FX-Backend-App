"""End-to-end tests for the KYC verification wizard API.

The user-facing bug this pins down: the wizard used to keep the document type
and number in browser state only, so pressing "Continue" sent nothing to the
server and the later submission failed with "Please enter your document number
first."

These tests walk the whole happy path through the real endpoints and assert the
failure modes the fix had to close:

* save details -> save document (+number) -> upload front/back -> submit;
* submit without a number answers ``kyc_incomplete`` with ``missing``;
* uploads and empty updates never erase a saved document number.
"""

import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

from kyc.models import KYCProfile

User = get_user_model()

# PNG magic bytes: long enough for the content sniffer to accept the upload.
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + bytes(24)


class KYCFullFlowTests(TestCase):
    """Save -> upload -> submit, through the HTTP API only."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='kyc_flow', email='kyc_flow@example.com', password='pw12345',
        )
        self.token = Token.objects.create(user=self.user)
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.token.key}'}

    # -- helpers ---------------------------------------------------------

    def _complete_personal_details(self):
        return self.client.put(
            reverse('kyc-details'),
            data=json.dumps({
                'fullName': 'Jane Doe',
                'dateOfBirth': '1990-01-01',
                'country': 'Nigeria',
                'phone': '+234 801 234 5678',
                'addressLine': '14 Admiralty Way',
                'city': 'Lagos',
                'state': 'Lagos',
                'postalCode': '106104',
            }),
            content_type='application/json',
            **self.auth,
        )

    def _save_document(self, doc_type: str, doc_number: str | None = None):
        """Same endpoint the wizard's step-2 "Continue" now calls."""
        payload: dict = {'docType': doc_type}
        if doc_number is not None:
            payload['docNumber'] = doc_number
        return self.client.put(
            reverse('kyc-details'),
            data=json.dumps(payload),
            content_type='application/json',
            **self.auth,
        )

    def _upload(self, kind: str, doc_type: str | None = None, doc_number: str | None = None):
        form = {
            'kind': kind,
            'file': SimpleUploadedFile(f'{kind}.png', PNG_BYTES, content_type='image/png'),
        }
        if doc_type is not None:
            form['doc_type'] = doc_type
        if doc_number is not None:
            form['doc_number'] = doc_number
        return self.client.post(reverse('kyc-documents'), form, **self.auth)

    # -- tests -----------------------------------------------------------

    def test_full_flow_saves_and_submits(self):
        """The complete wizard flow ends with a 200 and status 'pending'."""
        response = self._complete_personal_details()
        self.assertEqual(response.status_code, 200)

        response = self._save_document('national_id', '89234567890')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['hasDocumentNumber'])
        self.assertEqual(body['documentNumberLast4'], '7890')
        self.assertEqual(body['docNumberMasked'], '••••7890')

        self.assertEqual(self._upload('id_front', doc_type='national_id').status_code, 201)
        self.assertEqual(self._upload('id_back', doc_type='national_id').status_code, 201)
        self.assertEqual(self._upload('selfie').status_code, 201)

        response = self.client.post(reverse('kyc-submit'), **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'pending')

    def test_submit_without_a_number_reports_missing(self):
        """No number -> one aggregate 400 listing every missing item."""
        self._complete_personal_details()
        self._save_document('national_id')  # type chosen, number skipped

        self._upload('id_front', doc_type='national_id')
        self._upload('id_back', doc_type='national_id')
        self._upload('selfie')

        response = self.client.post(reverse('kyc-submit'), **self.auth)
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data['detail'], 'Some information is missing')
        self.assertEqual(data['code'], 'kyc_incomplete')
        self.assertIn('document_number', data['missing'])

    def test_uploads_after_a_saved_number_do_not_erase_it(self):
        """Re-uploading a file must not clear the stored document number."""
        self._complete_personal_details()
        response = self._save_document('national_id', '89234567890')
        self.assertEqual(response.status_code, 200)

        self.assertEqual(self._upload('selfie').status_code, 201)

        response = self.client.get(reverse('kyc-status'), **self.auth)
        data = response.json()
        self.assertTrue(data['hasDocumentNumber'])
        self.assertEqual(data['documentNumberLast4'], '7890')

        self.user.kyc_profile.refresh_from_db()
        self.assertTrue(self.user.kyc_profile.doc_number_encrypted)

    def test_empty_document_number_never_overwrites_a_stored_one(self):
        """A blank "docNumber" must not wipe an earlier save."""
        self._complete_personal_details()
        self.assertEqual(self._save_document('national_id', '89234567890').status_code, 200)
        self.assertEqual(self._save_document('national_id', '').status_code, 200)

        self.user.kyc_profile.refresh_from_db()
        self.assertTrue(self.user.kyc_profile.doc_number_encrypted)
