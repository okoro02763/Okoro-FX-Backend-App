"""KYC API views.

All endpoints require authentication. Document numbers are encrypted at rest
and never returned in full; files live in private storage and are served only
through short-lived signed URLs minted for staff.
"""
import logging

from django.conf import settings
from django.core import signing
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from forex.authentication import BearerTokenAuthentication

from .crypto import encrypt_value
from .models import KYCProfile
from .services import (
    KYCStorageUnavailable,
    apply_document_fields,
    approve_profile,
    can_submit,
    get_or_create_profile,
    reject_profile,
    serialize_profile,
    submit_for_review,
)
from .validators import FileValidationError, sniff_content_type

logger = logging.getLogger('kyc')


def _server_error_response():
    """Return a friendly, *retryable* error without leaking internals.

    503 rather than 500: the database being unavailable is a transient outage
    the client should retry, and the frontend's retry logic keys off ``code``,
    not the status line. 500 told the UI "this is broken permanently".
    """
    return Response(
        {'detail': 'Something went wrong, please try again', 'code': 'server_error'},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )

VALID_DOC_TYPES = {
    'national_id', 'passport', 'drivers_license', 'voters_card', 'nin', 'bvn',
}
VALID_KINDS = {'id_front', 'id_back', 'selfie'}

# Signed URLs for uploaded files are valid for this many seconds.
SIGNED_URL_MAX_AGE = 300
SIGNING_SALT = 'kyc.document'


def _err(
    message: str,
    status_code: int = status.HTTP_400_BAD_REQUEST,
    code: str = 'invalid_request',
):
    """Every error in this app uses the same body shape.

    ``detail`` is the human-readable message and ``code`` is a stable
    machine-readable slug the frontend can branch on (for example
    ``kyc_incomplete``). The old ``message`` key is gone; the client reads
    ``detail`` and still falls back to ``message`` so it keeps working
    against a deployment that has not been updated yet.
    """
    return Response({'detail': message, 'code': code}, status=status_code)


# The personal-detail fields the wizard sends, and where they land on the
# profile. A key that is absent from the request body is left alone, which
# is what lets the wizard save the document type and number on their own
# without wiping the details, and the details without wiping the document.
_DETAIL_FIELDS = {
    'fullName': 'full_name',
    'country': 'country',
    'phone': 'phone',
    'addressLine': 'address_line',
    'city': 'city',
    'state': 'state',
    'postalCode': 'postal_code',
}
_DETAIL_MAX_LENGTHS = {
    'full_name': 150,
    'country': 80,
    'phone': 40,
    'address_line': 255,
    'city': 100,
    'state': 100,
    'postal_code': 20,
}

# Document types that have a reverse side worth photographing.
_NEEDS_BACK = {'national_id', 'drivers_license', 'voters_card'}
# Document types that are a number only -- no image upload required.
_NUMBER_ONLY = {'nin', 'bvn'}

# The personal details that must be present before a submission is accepted.
_REQUIRED_DETAILS = (
    'full_name',
    'date_of_birth',
    'country',
    'phone',
    'address_line',
    'city',
    'state',
    'postal_code',
)


def _missing_items(profile) -> list[str]:
    """Everything still missing from the profile, as machine-readable keys.

    All of it is reported at once so the client can show one list and send
    the user to the right step, instead of revealing one problem per
    submission attempt.
    """
    missing = [field for field in _REQUIRED_DETAILS if not getattr(profile, field)]

    if not profile.doc_type:
        missing.append('document_type')
    if not profile.doc_number_encrypted:
        missing.append('document_number')

    kinds = set(profile.documents.values_list('kind', flat=True))
    if 'selfie' not in kinds:
        missing.append('selfie')
    if profile.doc_type not in _NUMBER_ONLY:
        if 'id_front' not in kinds:
            missing.append('id_front')
        if profile.doc_type in _NEEDS_BACK and 'id_back' not in kinds:
            missing.append('id_back')

    return missing


def _kyc_throttle():
    return [ScopedRateThrottle()]


@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def kyc_status(request):
    """Current status + draft details + documents. No sensitive fields."""
    try:
        profile = get_or_create_profile(request.user)
        return Response(serialize_profile(profile))
    except KYCStorageUnavailable:
        logger.exception('KYC status failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()
    except Exception:  # noqa: BLE001
        logger.exception('KYC status failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()


@api_view(['PUT', 'PATCH'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def save_details(request):
    """Save the KYC draft.

    Partial update: only the keys present in the request body are written.
    The wizard saves the personal details in step 1 and the document type
    and number in step 2, so each step has to be able to write its own
    fields without wiping the others.

    The document number is encrypted before it is stored and is never echoed
    back in full.
    """
    try:
        profile = get_or_create_profile(request.user)
        if profile.status == 'pending':
            return _err(
                'Your documents are under review and cannot be edited.',
                status.HTTP_409_CONFLICT,
                'submission_locked',
            )
        if profile.status == 'verified':
            return _err(
                'Your identity is already verified.',
                status.HTTP_409_CONFLICT,
                'already_verified',
            )

        data = request.data or {}
        changed: list[str] = []

        # 1. Personal details (step 1).
        for key, field in _DETAIL_FIELDS.items():
            if key not in data:
                continue
            value = str(data.get(key) or '').strip()
            setattr(profile, field, value[: _DETAIL_MAX_LENGTHS[field]])
            changed.append(field)

        if 'dateOfBirth' in data:
            dob_raw = str(data.get('dateOfBirth') or '').strip()
            date_of_birth = None
            if dob_raw:
                try:
                    date_of_birth = timezone.datetime.strptime(dob_raw, '%Y-%m-%d').date()
                except ValueError:
                    return _err(
                        'dateOfBirth must be YYYY-MM-DD.',
                        status.HTTP_400_BAD_REQUEST,
                        'invalid_date_of_birth',
                    )

                # Server-side 18+ check (the client checks too, but never trust it).
                today = timezone.now().date()
                age = today.year - date_of_birth.year - (
                    (today.month, today.day) < (date_of_birth.month, date_of_birth.day)
                )
                if age < 18:
                    return _err(
                        'You must be at least 18 years old to verify.',
                        status.HTTP_400_BAD_REQUEST,
                        'underage',
                    )
            profile.date_of_birth = date_of_birth
            changed.append('date_of_birth')

        # 2. Document type and number (step 2).
        #
        # An empty value never overwrites a stored one: a client that sends
        # ``docNumber: ""`` must not be able to erase a number that was saved
        # earlier, and an upload must not clear it either.
        if 'docType' in data:
            doc_type = str(data.get('docType') or '').strip().lower()
            if doc_type and doc_type not in VALID_DOC_TYPES:
                return _err(
                    'Unknown document type.',
                    status.HTTP_400_BAD_REQUEST,
                    'invalid_doc_type',
                )
            if doc_type:
                profile.doc_type = doc_type[:30]
                changed.append('doc_type')

        if 'docNumber' in data:
            doc_number = str(data.get('docNumber') or '').strip()
            if doc_number:
                profile.doc_number_encrypted = encrypt_value(doc_number)
                changed.append('doc_number_encrypted')

        if changed:
            # `updated_at` is auto_now, so it has to be listed explicitly when
            # a partial save is used.
            profile.save(update_fields=[*changed, 'updated_at'])

        # Diagnostic breadcrumb for the "number looks saved but the server
        # never received it" class of bug. Only key names are logged -- never
        # a document number, a value, or a secret.
        logger.info(
            'KYC details saved: user=%s keys=%s changed=%s',
            profile.user_id,
            sorted(str(key) for key in data.keys()),
            changed,
        )
        return Response(serialize_profile(profile))
    except KYCStorageUnavailable:
        logger.exception('Save KYC details failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()
    except Exception:  # noqa: BLE001
        logger.exception('Save KYC details failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def upload_document(request):
    """Upload (or replace) one document: id_front / id_back / selfie.

    Multipart form: kind, file, optional doc_type + doc_number.
    Validates the file by content, not extension, and caps it at 5 MB.
    """
    try:
        return _upload_document(request)
    except KYCStorageUnavailable:
        logger.exception('KYC upload failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()
    except Exception:  # noqa: BLE001
        logger.exception('KYC upload failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()


def _upload_document(request):
    profile = get_or_create_profile(request.user)
    if profile.status == 'pending':
        return _err(
            'Your documents are under review and cannot be changed.',
            status.HTTP_409_CONFLICT,
            'submission_locked',
        )
    if profile.status == 'verified':
        return _err(
            'Your identity is already verified.',
            status.HTTP_409_CONFLICT,
            'already_verified',
        )

    kind = str(request.data.get('kind') or '').strip()
    if kind not in VALID_KINDS:
        return _err(
            'kind must be one of: id_front, id_back, selfie.',
            status.HTTP_400_BAD_REQUEST,
            'invalid_kind',
        )

    uploaded = request.FILES.get('file')
    try:
        content_type = sniff_content_type(uploaded)
    except FileValidationError as exc:
        return _err(str(exc), status.HTTP_400_BAD_REQUEST, 'invalid_file')

    doc_type = str(
        request.data.get('doc_type') or request.data.get('docType') or ''
    ).strip().lower()
    if doc_type and doc_type not in VALID_DOC_TYPES:
        return _err(
            'Unknown doc_type.',
            status.HTTP_400_BAD_REQUEST,
            'invalid_doc_type',
        )

    # Replace any existing document in this slot (same profile+kind).
    profile.documents.filter(kind=kind).delete()
    doc = profile.documents.create(
        kind=kind,
        doc_type=doc_type or profile.doc_type,
        file=uploaded,
        content_type=content_type,
    )

    # The doc number rides along with the first upload and is stored encrypted
    # on the profile; it is never echoed back in full. An upload never
    # clears a number that was saved in the previous step -- an empty value
    # never overwrites a stored one.
    doc_number = str(
        request.data.get('doc_number') or request.data.get('docNumber') or ''
    ).strip()
    apply_document_fields(profile, doc_type=doc_type, doc_number=doc_number)

    logger.info(
        'KYC document uploaded: user=%s kind=%s doc=%s doc_type=%s number_provided=%s',
        profile.user_id, kind, doc.pk, doc_type or profile.doc_type, bool(doc_number),
    )
    return Response(serialize_profile(profile), status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def delete_document(request, document_id):
    """Remove an uploaded document before submitting."""
    try:
        profile = get_or_create_profile(request.user)
        if profile.status in {'pending', 'verified'}:
            return _err(
                'This submission cannot be changed right now.',
                status.HTTP_409_CONFLICT,
                'submission_locked',
            )
        deleted, _ = profile.documents.filter(id=document_id).delete()
        # Idempotent by design: the browser can retry a delete (a double click,
        # a flaky connection, React re-rendering) and the second request must
        # not turn into a scary 404. The document is gone either way, which is
        # exactly what the caller asked for.
        if not deleted:
            logger.info(
                'KYC delete ignored (already gone): user=%s document=%s',
                profile.user_id, document_id,
            )
        return Response(serialize_profile(profile))
    except KYCStorageUnavailable:
        logger.exception('KYC delete failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()
    except Exception:  # noqa: BLE001
        logger.exception('KYC delete failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def submit(request):
    """Validate everything is present, then move the profile to pending."""
    try:
        return _submit(request)
    except KYCStorageUnavailable:
        logger.exception('KYC submit failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()
    except Exception:  # noqa: BLE001
        logger.exception('KYC submit failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()


def _submit(request):
    profile = get_or_create_profile(request.user)

    if profile.status == 'pending':
        return _err(
            'Your documents are already under review.',
            status.HTTP_409_CONFLICT,
            'submission_locked',
        )
    if profile.status == 'verified':
        return _err(
            'Your identity is already verified.',
            status.HTTP_409_CONFLICT,
            'already_verified',
        )

    ok, reason = can_submit(profile)
    if not ok:
        return _err(reason, status.HTTP_429_TOO_MANY_REQUESTS, 'rate_limited')

    # Everything that is still missing, reported in one go. The keys are
    # machine-readable so the frontend can send the user straight to the
    # step that needs fixing instead of making them hunt for it.
    missing = _missing_items(profile)
    if missing:
        return Response(
            {
                'detail': 'Some information is missing',
                'code': 'kyc_incomplete',
                'missing': missing,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    submit_for_review(profile)
    # Activity alert is emitted by kyc.services.submit_for_review (info level,
    # user id only, never document data) -- do not duplicate it here.
    return Response(serialize_profile(profile))


# ---------------------------------------------------------------------------
# Staff-only document access via short-lived signed URLs
# ---------------------------------------------------------------------------

def mint_signed_url(request, document_id: int) -> str:
    """Create a URL that serves one document for SIGNED_URL_MAX_AGE seconds.

    Used by the admin to preview files; the download view re-validates the
    signature and the age, so a leaked URL expires quickly. Built from the
    incoming request's host so it works in any environment.
    """
    token = signing.dumps(
        {'doc': document_id, 'exp': int(timezone.now().timestamp()) + SIGNED_URL_MAX_AGE},
        salt=SIGNING_SALT,
    )
    return f'{request.scheme}://{request.get_host()}/api/kyc/files/{token}/'


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_document(request, token):
    """Serve one private file if the signed token is valid and fresh.

    The token is minted only for authenticated staff (see the admin), so a
    regular user cannot obtain one; the signature check makes a forged or
    expired URL useless.
    """
    try:
        payload = signing.loads(token, salt=SIGNING_SALT, max_age=SIGNED_URL_MAX_AGE)
    except signing.BadSignature as exc:
        raise Http404 from exc

    if not request.user.is_staff:
        # Staff-only: even with a valid token, non-staff get nothing.
        raise Http404

    from .models import KYCDocument

    doc = KYCDocument.objects.filter(id=payload.get('doc')).select_related('profile').first()
    if doc is None or not doc.file:
        raise Http404

    response = FileResponse(doc.file.open('rb'), content_type=doc.content_type or 'application/octet-stream')
    response['Content-Disposition'] = f'inline; filename="document-{doc.id}"'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
