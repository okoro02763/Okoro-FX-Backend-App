"""KYC API views.

All endpoints require authentication. Document numbers are encrypted at rest
and never returned in full; files live in private storage and are served only
through short-lived signed URLs minted for staff.
"""
import logging

from django.conf import settings
from django.core import signing
from django.db import DatabaseError
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
    """Return a friendly 500 without leaking internals."""
    return Response(
        {'detail': 'Something went wrong, please try again', 'code': 'server_error'},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )

VALID_DOC_TYPES = {
    'national_id', 'passport', 'drivers_license', 'voters_card', 'nin', 'bvn',
}
VALID_KINDS = {'id_front', 'id_back', 'selfie'}

# Signed URLs for uploaded files are valid for this many seconds.
SIGNED_URL_MAX_AGE = 300
SIGNING_SALT = 'kyc.document'


def _err(message: str, code: int = status.HTTP_400_BAD_REQUEST):
    return Response({'message': message}, status=code)


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
    except DatabaseError:
        logger.exception('KYC status failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()
    except Exception:  # noqa: BLE001
        logger.exception('KYC status failed for user %s', getattr(request.user, 'pk', None))
        return _server_error_response()


@api_view(['PUT'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def save_details(request):
    """Save step 1 (personal details) as a draft."""
    try:
        profile = get_or_create_profile(request.user)
        if profile.status == 'pending':
            return _err('Your documents are under review and cannot be edited.', status.HTTP_409_CONFLICT)
        if profile.status == 'verified':
            return _err('Your identity is already verified.', status.HTTP_409_CONFLICT)

        data = request.data or {}
        full_name = str(data.get('fullName') or '').strip()
        dob_raw = str(data.get('dateOfBirth') or '').strip()
        country = str(data.get('country') or '').strip()
        phone = str(data.get('phone') or '').strip()
        address_line = str(data.get('addressLine') or '').strip()
        city = str(data.get('city') or '').strip()
        state_name = str(data.get('state') or '').strip()
        postal_code = str(data.get('postalCode') or '').strip()

        date_of_birth = None
        if dob_raw:
            try:
                date_of_birth = timezone.datetime.strptime(dob_raw, '%Y-%m-%d').date()
            except ValueError:
                return _err('dateOfBirth must be YYYY-MM-DD.')

        # Server-side 18+ check (the client checks too, but never trust it).
        if date_of_birth is not None:
            today = timezone.now().date()
            age = today.year - date_of_birth.year - (
                (today.month, today.day) < (date_of_birth.month, date_of_birth.day)
            )
            if age < 18:
                return _err('You must be at least 18 years old to verify.')

        profile.full_name = full_name[:150]
        profile.date_of_birth = date_of_birth
        profile.country = country[:80]
        profile.phone = phone[:40]
        profile.address_line = address_line[:255]
        profile.city = city[:100]
        profile.state = state_name[:100]
        profile.postal_code = postal_code[:20]
        profile.save()
        return Response(serialize_profile(profile))
    except DatabaseError:
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
    profile = get_or_create_profile(request.user)
    if profile.status == 'pending':
        return _err('Your documents are under review and cannot be changed.', status.HTTP_409_CONFLICT)
    if profile.status == 'verified':
        return _err('Your identity is already verified.', status.HTTP_409_CONFLICT)

    kind = str(request.data.get('kind') or '').strip()
    if kind not in VALID_KINDS:
        return _err('kind must be one of: id_front, id_back, selfie.')

    uploaded = request.FILES.get('file')
    try:
        content_type = sniff_content_type(uploaded)
    except FileValidationError as exc:
        return _err(str(exc))

    doc_type = str(request.data.get('doc_type') or '').strip().lower()
    if doc_type and doc_type not in VALID_DOC_TYPES:
        return _err('Unknown doc_type.')

    # Replace any existing document in this slot (same profile+kind).
    profile.documents.filter(kind=kind).delete()
    doc = profile.documents.create(
        kind=kind,
        doc_type=doc_type or profile.doc_type,
        file=uploaded,
        content_type=content_type,
    )

    # The doc number rides along with the first upload and is stored encrypted
    # on the profile; it is never echoed back in full.
    doc_number = str(request.data.get('doc_number') or '').strip()
    if doc_number:
        profile.doc_number_encrypted = encrypt_value(doc_number)
        profile.doc_type = doc_type or profile.doc_type
        profile.save(update_fields=['doc_number_encrypted', 'doc_type'])
    elif doc_type:
        profile.doc_type = doc_type
        profile.save(update_fields=['doc_type'])

    logger.info(
        'KYC document uploaded: user=%s kind=%s doc=%s',
        profile.user_id, kind, doc.pk,
    )
    return Response(serialize_profile(profile), status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def delete_document(request, document_id):
    """Remove an uploaded document before submitting."""
    profile = get_or_create_profile(request.user)
    if profile.status in {'pending', 'verified'}:
        return _err('This submission cannot be changed right now.', status.HTTP_409_CONFLICT)
    deleted, _ = profile.documents.filter(id=document_id).delete()
    if not deleted:
        raise Http404
    return Response(serialize_profile(profile))


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def submit(request):
    """Validate everything is present, then move the profile to pending."""
    profile = get_or_create_profile(request.user)

    if profile.status == 'pending':
        return _err('Your documents are already under review.', status.HTTP_409_CONFLICT)
    if profile.status == 'verified':
        return _err('Your identity is already verified.', status.HTTP_409_CONFLICT)

    ok, reason = can_submit(profile)
    if not ok:
        return _err(reason, status.HTTP_429_TOO_MANY_REQUESTS)

    # 1. Personal details complete.
    missing = [
        label
        for field, label in (
            ('full_name', 'full name'),
            ('date_of_birth', 'date of birth'),
            ('country', 'country'),
            ('phone', 'phone number'),
            ('address_line', 'address'),
            ('city', 'city'),
            ('state', 'state'),
            ('postal_code', 'postal code'),
        )
        if not getattr(profile, field)
    ]
    if missing:
        return _err(f'Please complete your personal details first: {", ".join(missing)}.')

    # 2. A document type and number.
    if not profile.doc_type:
        return _err('Please choose an identity document type first.')
    if not profile.doc_number_encrypted:
        return _err('Please enter your document number first.')

    # 3. Files: selfie always; ID front/back except for number-only types.
    kinds = set(profile.documents.values_list('kind', flat=True))
    number_only = profile.doc_type in {'nin', 'bvn'}
    if 'selfie' not in kinds:
        return _err('Please upload a selfie holding your document.')
    if not number_only and 'id_front' not in kinds:
        return _err('Please upload the front of your identity document.')

    submit_for_review(profile)
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
