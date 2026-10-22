"""KYC business logic shared by the views and the Django admin.

The status transitions live here so the API and the admin actions cannot
drift apart: every path to 'verified'/'rejected' sets the timestamps,
the reviewer, and sends the user an email.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from .crypto import encrypt_value, mask_value
from .models import KYCProfile
from .providers import get_provider

logger = logging.getLogger('kyc')

# How many submissions (initial + resubmissions) are allowed per window.
RESUBMIT_LIMIT = 3
RESUBMIT_WINDOW = timedelta(hours=24)


class KYCStorageUnavailable(Exception):
    """Raised when the KYC tables cannot be read (e.g. missing migration).

    Distinct from a generic ``DatabaseError`` so callers can degrade to a
    retryable response instead of a raw 500. See ``get_or_create_profile``.
    """


def get_or_create_profile(user) -> KYCProfile:
    """Get or create the user's profile, or raise ``KYCStorageUnavailable``.

    The old version caught ``DatabaseError``, logged, and **re-raised**. That
    looked defensive but produced HTTP 500 anyway: a ``DatabaseError`` leaves
    the request's transaction marked "needs rollback", so even though the view
    caught the exception and returned a friendly 503 body, Django forced the
    response status to 500. The frontend then had no ``code`` to act on.

    Wrapping the query in its own ``transaction.atomic()`` gives the failure a
    savepoint to unwind, so the connection returns to a usable state and the
    view's response -- status included -- actually reaches the client. The
    original exception is logged in full and chained onto the new one, so the
    production logs still show the real cause.
    """
    try:
        with transaction.atomic():
            profile, _ = KYCProfile.objects.get_or_create(user=user)
        return profile
    except DatabaseError as exc:
        logger.exception(
            'Could not get/create the KYC profile for user %s. '
            'Is the kyc migration applied on this database?',
            getattr(user, 'pk', None),
        )
        raise KYCStorageUnavailable(
            'The KYC profile table is unavailable; has the kyc migration been applied?'
        ) from exc


def _load_profile(user):
    """Read the user's KYC profile without ever raising.

    Returns ``(profile, error)``. ``profile`` is ``None`` when the user has
    no profile yet; ``error`` is a short string when the table could not be
    read at all -- the classic "relation \\"kyc_kycprofile\\" does not exist"
    that used to turn ``GET /api/account/`` into a 500.

    The failure is logged with a full traceback so it stays visible in the
    platform logs; it is only the *response* that degrades.
    """
    try:
        return KYCProfile.objects.filter(user=user).first(), None
    except DatabaseError:
        logger.exception(
            'Could not read the KYC profile for user %s. '
            'Is the kyc migration applied on this database?',
            getattr(user, 'pk', None),
        )
        return None, 'database-error'


def _legacy_statuses(user) -> set[str]:
    """Statuses present in the legacy single-document KYC table.

    Returns an empty set when that table cannot be read; the caller treats
    "no information" the same as "not verified".
    """
    try:
        rows = user.identity_verifications.values_list('status', flat=True)
        return set(rows)
    except DatabaseError:
        logger.exception(
            'Could not read the legacy identity verifications for user %s.',
            getattr(user, 'pk', None),
        )
        return set()


def user_is_verified(user) -> bool:
    """True when the user may use live-account features.

    Never raises: if the KYC tables cannot be read the answer is ``False``
    (fail closed -- no live access without proof of verification) and the
    underlying error is logged.
    """
    profile, error = _load_profile(user)
    if error:
        return False
    if profile is not None and profile.status == 'verified':
        return True
    # Legacy single-document KYC still counts.
    return 'approved' in _legacy_statuses(user)


def current_status(user) -> str:
    """Effective KYC status for display ('none'|'pending'|'verified'|'rejected')."""
    profile, error = _load_profile(user)
    if error:
        return 'none'
    if profile is not None and profile.status != 'none':
        return profile.status
    legacy = _legacy_statuses(user)
    if 'approved' in legacy:
        return 'verified'
    if 'pending' in legacy:
        return 'pending'
    if 'rejected' in legacy:
        return 'rejected'
    return 'none'


def apply_document_fields(profile: KYCProfile, doc_type=None, doc_number=None) -> list[str]:
    """Store the document type and number on the profile.

    Returns the names of the fields that actually changed. The number is
    encrypted before it touches the database and is never echoed back.

    An empty value never overwrites a stored one. That rule is what makes the
    wizard safe: a client that sends ``docNumber: ""`` must not be able to
    erase a number that was saved earlier, and uploading a file must not
    clear the number that was entered in the previous step.
    """
    changed: list[str] = []

    if doc_type:
        doc_type = str(doc_type).strip().lower()[:30]
        if doc_type and doc_type != profile.doc_type:
            profile.doc_type = doc_type
            changed.append('doc_type')

    if doc_number:
        doc_number = str(doc_number).strip()
        if doc_number:
            profile.doc_number_encrypted = encrypt_value(doc_number)
            changed.append('doc_number_encrypted')

    if changed:
        # `updated_at` is auto_now, so it has to be listed explicitly when a
        # partial save is used.
        profile.save(update_fields=[*changed, 'updated_at'])
    return changed


def can_submit(profile: KYCProfile) -> tuple[bool, str]:
    """Rate-limit submissions: `RESUBMIT_LIMIT` per `RESUBMIT_WINDOW`."""
    now = timezone.now()
    if (
        profile.resubmit_window_start is None
        or now - profile.resubmit_window_start > RESUBMIT_WINDOW
    ):
        return True, ''
    if profile.resubmit_count >= RESUBMIT_LIMIT:
        return False, (
            'You have reached the submission limit for today. '
            'Please try again tomorrow.'
        )
    return True, ''


def _bump_resubmit_counter(profile: KYCProfile) -> None:
    now = timezone.now()
    if (
        profile.resubmit_window_start is None
        or now - profile.resubmit_window_start > RESUBMIT_WINDOW
    ):
        profile.resubmit_window_start = now
        profile.resubmit_count = 1
    else:
        profile.resubmit_count += 1


def _send(template: str, subject: str, user, **context) -> None:
    """Fire-and-forget email; delivery failure must never break the flow."""
    try:
        from forex.email_service import send_template_email  # avoid a cycle

        send_template_email(
            template,
            user.email,
            {'name': user.first_name or user.email, **context},
            subject=subject,
            defer=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception('KYC email (%s) failed for user %s', template, user.pk)


def submit_for_review(profile: KYCProfile) -> None:
    """Move a complete profile to 'pending' and notify the user.

    Caller is responsible for validating completeness (see the view).
    """
    with transaction.atomic():
        profile.status = 'pending'
        profile.submitted_at = timezone.now()
        profile.reviewed_at = None
        profile.reviewed_by = None
        profile.rejection_reason = ''
        _bump_resubmit_counter(profile)
        profile.save(
            update_fields=[
                'status', 'submitted_at', 'reviewed_at', 'reviewed_by',
                'rejection_reason', 'resubmit_count', 'resubmit_window_start',
            ]
        )
    _send(
        'kyc_received',
        'We received your documents',
        profile.user,
        doc_type=profile.doc_type,
    )
    # Hand off to the configured provider (ManualProvider = no-op).
    get_provider().submit_for_review(profile)


def approve_profile(profile: KYCProfile, reviewer=None) -> None:
    """Staff approved the documents: grant live access and email the user."""
    with transaction.atomic():
        profile.status = 'verified'
        profile.reviewed_at = timezone.now()
        profile.reviewed_by = reviewer
        profile.rejection_reason = ''
        profile.save(
            update_fields=[
                'status', 'reviewed_at', 'reviewed_by', 'rejection_reason',
            ]
        )
    _send(
        'kyc_approved',
        'Your identity is verified',
        profile.user,
        action_url=f'{settings.FRONTEND_URL}/account',
    )


def reject_profile(profile: KYCProfile, reason: str, reviewer=None) -> None:
    """Staff rejected the documents: store the reason and email the user."""
    reason = (reason or '').strip() or 'Your documents could not be accepted.'
    with transaction.atomic():
        profile.status = 'rejected'
        profile.reviewed_at = timezone.now()
        profile.reviewed_by = reviewer
        profile.rejection_reason = reason[:2000]
        profile.save(
            update_fields=[
                'status', 'reviewed_at', 'reviewed_by', 'rejection_reason',
            ]
        )
    _send(
        'kyc_rejected',
        'Action needed on your verification',
        profile.user,
        reason=profile.rejection_reason,
        action_url=f'{settings.FRONTEND_URL}/account/verification',
    )


def serialize_profile(profile: KYCProfile) -> dict:
    """Public, non-sensitive representation. Never includes the raw
    document number -- only the masked tail."""
    number = _decrypt(profile)
    has_number = bool(profile.doc_number_encrypted)
    last4 = number[-4:] if number else ''
    return {
        'status': profile.status,
        'rejectionReason': profile.rejection_reason or None,
        'submittedAt': profile.submitted_at.isoformat() if profile.submitted_at else None,
        'reviewedAt': profile.reviewed_at.isoformat() if profile.reviewed_at else None,
        'details': {
            'fullName': profile.full_name,
            'dateOfBirth': profile.date_of_birth.isoformat() if profile.date_of_birth else '',
            'country': profile.country,
            'phone': profile.phone,
            'addressLine': profile.address_line,
            'city': profile.city,
            'state': profile.state,
            'postalCode': profile.postal_code,
        },
        'docType': profile.doc_type or None,
        'doc_type': profile.doc_type or None,
        'docNumberMasked': mask_value(number) if has_number else None,
        # Whether a document number is stored, and its last four digits.
        # The review screen renders from these instead of from whatever the
        # browser happens to be holding, so it can never show a number the
        # server never received.
        'hasDocumentNumber': has_number,
        'documentNumberLast4': last4,
        # The same two values under their canonical snake_case names, so any
        # client (and the admin tooling) can read them without guessing.
        'has_document_number': has_number,
        'document_number_last4': last4,
        'documents': [
            {
                'id': doc.id,
                'kind': doc.kind,
                'docType': doc.doc_type,
                'uploadedAt': doc.uploaded_at.isoformat(),
            }
            for doc in profile.documents.all()
        ],
    }


def _decrypt(profile: KYCProfile) -> str:
    from .crypto import decrypt_value

    try:
        return decrypt_value(profile.doc_number_encrypted)
    except Exception:  # noqa: BLE001 -- key lost/rotated; show nothing, log it
        logger.exception('Could not decrypt doc number for profile %s', profile.pk)
        return ''
