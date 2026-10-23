import hmac
import hashlib
import logging
import os
from decimal import ROUND_HALF_UP, Decimal
import requests

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    permission_classes,
    authentication_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .authentication import BearerTokenAuthentication
from .permissions import IsAdminUser
from .models import AuditLog, IdentityVerification, Payment, Profile, Withdrawal

from kyc.permissions import IsKYCVerified
from kyc.services import current_status as kyc_current_status
from kyc.services import user_is_verified as kyc_user_is_verified

User = get_user_model()

logger = logging.getLogger(__name__)

# Every money movement in this app (Paystack funding, withdrawals) is in
# naira, so the account balance is reported in the same unit.
ACCOUNT_CURRENCY = 'NGN'


def _log(actor, action, target='', details=None):
    AuditLog.objects.create(
        actor=actor,
        action=action,
        target=target,
        details=details or {},
    )


def _with_profile(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def _kyc_serialize(v):
    return {
        'id': v.id,
        'docType': v.doc_type,
        'docNumber': v.doc_number,
        'docImage': v.doc_image.url if v.doc_image else None,
        'status': v.status,
        'reviewNote': v.review_note,
        'submittedAt': v.submitted_at.isoformat(),
        'reviewedAt': v.reviewed_at.isoformat() if v.reviewed_at else None,
    }


def _legacy_kyc_records(user):
    """Legacy single-document verifications, newest first.

    Never raises: a missing table must not turn ``GET /api/account/`` into a
    500. The error is logged so it stays diagnosable.
    """
    try:
        return list(user.identity_verifications.all())
    except DatabaseError:
        logger.exception(
            'Could not read the legacy identity verifications for user %s.',
            getattr(user, 'pk', None),
        )
        return []


def _balance_number(value):
    """Balance as a plain number, never ``None``.

    The frontend renders this straight into a currency string, so a null
    here used to render as a broken value. Anything unusable becomes 0.
    """
    try:
        amount = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        logger.exception('Unusable balance value on profile: %r', value)
        return 0.0
    if not amount.is_finite():
        return 0.0
    return float(amount)


def _account_status(user):
    """The account payload shared by ``GET /api/account/`` and the type switch.

    Both spellings of every field are returned and are always equal, so the
    deployed frontend (camelCase) and any new client (snake_case) cannot
    drift apart:

        account_type / accountType          'demo' | 'live'
        balance / availableBalance          number, never null
        currency                            ISO code of the wallet ('NGN')
        verification_status / kycStatus     'none'|'pending'|'verified'|'rejected'
        can_trade_live / liveVerified       whether live trading is unlocked
    """
    profile = _with_profile(user)
    kyc = _legacy_kyc_records(user)
    approved = kyc_user_is_verified(user)
    pending = any(k.status == 'pending' for k in kyc)
    # Unified status across the legacy document flow and the new kyc app.
    verification_status = kyc_current_status(user)
    balance = _balance_number(profile.available_balance)
    account_type = profile.account_type or 'demo'
    return {
        'accountType': account_type,
        'availableBalance': balance,
        'liveVerified': approved,
        'kycPending': pending,
        'kycStatus': verification_status,
        'kyc': [_kyc_serialize(k) for k in kyc],
        'fees': {
            'fundingPercent': settings.FUNDING_FEE_PERCENT,
            'withdrawalPercent': settings.WITHDRAWAL_FEE_PERCENT,
            'minWithdrawal': settings.MIN_WITHDRAWAL,
        },
        # Flat aliases with the documented names. The camelCase keys above
        # are the contract the deployed frontend already uses; these are the
        # same values under their canonical names so both shapes stay in
        # sync and neither can drift.
        'account_type': account_type,
        'balance': balance,
        'currency': ACCOUNT_CURRENCY,
        'verification_status': verification_status,
        'can_trade_live': approved,
    }


CENT = Decimal('0.01')


def compute_fee(amount, percent):
    """Return the fee for a given amount at a percentage rate.

    Uses Decimal with ROUND_HALF_UP; float arithmetic and Python's banker's
    rounding both lose or gain kobo unpredictably on a money path.
    """
    amount = Decimal(str(amount))
    percent = Decimal(str(percent))
    return (amount * percent / Decimal('100')).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_amount(raw, *, field='Amount'):
    """Parse a client-supplied money value, or return None if unusable.

    Rejects NaN/Infinity and out-of-range values, which otherwise raise
    deep inside decimal comparisons and surface as a 500.
    """
    try:
        value = Decimal(str(raw))
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not value.is_finite():
        return None
    if value <= 0 or value > Decimal('100000000'):
        return None
    return value


@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def account_status(request):
    """Return the user's account type and KYC verification state."""
    return Response(_account_status(request.user))


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def set_account_type(request):
    """Switch between demo and live account. Body: { accountType: demo|live }"""
    account_type = (request.data.get('accountType') or '').strip().lower()
    if account_type not in {'demo', 'live'}:
        return Response(
            {'message': 'accountType must be one of demo, live.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    profile = _with_profile(request.user)

    # Switching to live requires an already-approved KYC document.
    if account_type == 'live':
        approved = user_has_approved_kyc(request.user)
        if not approved:
            return Response(
                {
                    'message': 'You must complete identity verification before using a live account.',
                    'code': 'KYC_REQUIRED',
                },
                status=status.HTTP_403_FORBIDDEN,
            )

    profile.account_type = account_type
    profile.save()
    return Response(_account_status(request.user))


def user_has_approved_kyc(user):
    """Unified: new kyc app ('verified') or legacy approved document."""
    return kyc_user_is_verified(user)


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def submit_kyc(request):
    """Upload a government document for verification.
    Multipart form: doc_type, doc_number, doc_image."""
    doc_type = (request.data.get('doc_type') or '').strip().lower()
    doc_number = (request.data.get('doc_number') or '').strip()
    doc_image = request.FILES.get('doc_image')

    valid_types = {t for t, _ in IdentityVerification.DOC_TYPES}
    if doc_type not in valid_types:
        return Response(
            {'message': 'doc_type must be one of: nin, national_id, voters_card, other.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not doc_number or not doc_image:
        return Response(
            {'message': 'doc_number and doc_image are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # A user may only have one submission pending at a time.
    if request.user.identity_verifications.filter(status='pending').exists():
        return Response(
            {'message': 'You already have a verification in review.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    verification = IdentityVerification.objects.create(
        user=request.user,
        doc_type=doc_type,
        doc_number=doc_number,
        doc_image=doc_image,
        status='pending',
    )

    try:
        from monitoring.activity import send_activity_on_commit

        user_id = request.user.pk
        prior_rejections = request.user.identity_verifications.filter(status='rejected').count()
        if prior_rejections >= 3:
            send_activity_on_commit(
                f'KYC resubmitted after 3+ rejections | user_id: {user_id} | prior_rejections: {prior_rejections}',
                level='warning',
                user_id=user_id,
                dedupe_key=f'kyc-resubmit-3:{user_id}',
                cooldown_seconds=3600,
            )
        else:
            send_activity_on_commit(
                f'KYC submitted, waiting for approval | user_id: {user_id}',
                level='info',
                user_id=user_id,
            )
    except Exception:  # noqa: BLE001 - alerting must never fail a submission
        pass

    return Response(
        {'message': 'Document submitted for verification.', 'kyc': _kyc_serialize(verification)},
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsKYCVerified])
def live_fund(request):
    """Initialize a live-account funding payment via Paystack.
    Body: { amount } in naira (NGN). Identity verification is required
    (enforced by IsKYCVerified)."""

    secret = settings.PAYSTACK_SECRET_KEY
    if not secret:
        return Response(
            {'message': 'PAYSTACK_SECRET_KEY is not configured on the server.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        amount_decimal = parse_amount(request.data.get('amount'))
    except Exception:  # noqa: BLE001
        amount_decimal = None
    if amount_decimal is None:
        return Response(
            {'message': 'Amount is invalid.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    amount_decimal = amount_decimal.quantize(CENT, rounding=ROUND_HALF_UP)
    if amount_decimal < 100:
        return Response(
            {'message': 'Minimum deposit is 100 NGN.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Apply the funding service charge: the user pays amount + fee, and their
    # wallet is credited with the requested amount (net of the charge).
    fee = compute_fee(amount_decimal, settings.FUNDING_FEE_PERCENT)
    charge_total = amount_decimal + fee

    headers = {'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'}
    payload = {
        'email': request.user.email,
        'amount': int(charge_total * 100),  # Paystack uses kobo (amount * 100)
        'currency': 'NGN',
        'metadata': {
            'user_id': request.user.id,
            'purpose': 'live_account_funding',
        },
    }
    try:
        resp = requests.post(
            f"{settings.PAYSTACK_BASE_URL}/transaction/initialize",
            json=payload,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        logger.exception('Paystack initialise failed')
        return Response(
            {'message': 'Could not reach the payment provider. Please try again.'},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    if not data.get('status'):
        return Response(
            {'message': data.get('message', 'Paystack could not initialise payment.')},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    reference = data['data']['reference']
    # Persist what this reference is worth. The webhook credits from this row,
    # so the amount cannot be influenced by anything in the callback payload.
    Payment.objects.create(
        user=request.user,
        reference=reference,
        amount_charged=charge_total,
        fee=fee,
        credit_amount=amount_decimal,
        currency='NGN',
        status='pending',
    )

    try:
        from monitoring.activity import is_large_deposit, send_activity_on_commit

        large = is_large_deposit(amount_decimal)
        title = 'Large deposit request' if large else 'Deposit request created'
        send_activity_on_commit(
            f'{title} | user_id: {request.user.pk} | amount: {amount_decimal} | currency: NGN',
            level='warning' if large else 'info',
            user_id=request.user.pk,
            dedupe_key=f'deposit-request:{reference}',
            cooldown_seconds=3600,
            fields={'amount': amount_decimal, 'currency': 'NGN'},
        )
    except Exception:  # noqa: BLE001 - alerting must never fail funding
        pass

    return Response(
        {
            'authorizationUrl': data['data']['authorization_url'],
            'reference': reference,
            'breakdown': {
                'amount': str(amount_decimal),
                'fee': str(fee),
                'total': str(charge_total),
            },
        }
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def paystack_webhook(request):
    """Credit a user's wallet for a confirmed Paystack payment.

    Three independent gates must all pass before a single naira moves:
      1. The request carries a valid HMAC over the *raw* body.
      2. Paystack's verify API confirms the reference is genuinely `success`
         for the amount we originally charged.
      3. A Payment row for that reference exists and is not already credited.
    """
    secret = settings.PAYSTACK_SECRET_KEY
    if not secret:
        # Fail closed: an unconfigured secret must never mean "trust everyone".
        logger.error('Paystack webhook hit with no PAYSTACK_SECRET_KEY configured')
        return Response(
            {'message': 'Webhook is not configured.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Paystack signs the exact bytes it sent. Re-serialising with json.dumps()
    # produces a different byte stream, so the digest would never match.
    signature = request.headers.get('x-paystack-signature', '')
    expected = hmac.new(
        secret.encode('utf-8'), request.body, hashlib.sha512
    ).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        logger.warning('Rejected Paystack webhook with an invalid signature')
        return Response(
            {'message': 'Invalid signature'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    event = request.data.get('event', '')
    if event != 'charge.success':
        return Response({'message': 'Received'})

    data = request.data.get('data', {}) or {}
    reference = data.get('reference') or ''
    if not reference:
        return Response({'message': 'Missing reference'}, status=status.HTTP_400_BAD_REQUEST)

    payment = Payment.objects.filter(reference=reference).first()
    if payment is None:
        # A reference we never issued, or one whose row was rolled back.
        logger.warning('Rejected webhook for unknown reference %s', reference)
        try:
            from monitoring.activity import send_activity

            send_activity(
                'Payment webhook did not match an order | status: unmatched',
                level='warning',
                dedupe_key=f'unmatched-payment:{str(reference)[-8:]}',
                cooldown_seconds=3600,
                fields={'status': 'unmatched'},
            )
        except Exception:  # noqa: BLE001
            pass
        return Response({'message': 'Unknown reference'}, status=status.HTTP_400_BAD_REQUEST)

    if payment.status == 'success':
        # Idempotent redelivery -- acknowledge without crediting twice.
        return Response({'message': 'Already processed'})

    # Ask Paystack what actually happened, rather than trusting the callback.
    try:
        verify_resp = requests.get(
            f"{settings.PAYSTACK_BASE_URL}/transaction/verify/{reference}",
            headers={'Authorization': f'Bearer {secret}'},
            timeout=30,
        )
        verify_resp.raise_for_status()
        verified = verify_resp.json()
    except Exception:  # noqa: BLE001
        logger.exception('Paystack verification call failed for %s', reference)
        return Response(
            {'message': 'Could not verify payment.'},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    if not verified.get('status') or not verified.get('data', {}).get('status') == 'success':
        Payment.objects.filter(pk=payment.pk).update(status='failed')
        try:
            from monitoring.activity import send_activity

            send_activity(
                'Deposit failed | status: failed',
                level='warning',
                dedupe_key=f'deposit-failed:{str(reference)[-8:]}',
                cooldown_seconds=3600,
                fields={'status': 'failed'},
            )
        except Exception:  # noqa: BLE001
            pass
        return Response({'message': 'Payment not successful'}, status=status.HTTP_400_BAD_REQUEST)

    # The provider reports kobo; Payment stores naira. Confirm the amount matches
    # what we actually charged so a swapped/underpaid reference is rejected.
    verified_kobo = verified['data'].get('amount')
    expected_kobo = int(payment.amount_charged * 100)
    if verified_kobo != expected_kobo:
        logger.error(
            'Amount mismatch for %s: paystack=%s expected=%s',
            reference, verified_kobo, expected_kobo,
        )
        try:
            from monitoring.activity import send_activity

            send_activity(
                'Deposit amount mismatch | status: mismatch',
                level='warning',
                dedupe_key=f'deposit-mismatch:{str(reference)[-8:]}',
                cooldown_seconds=3600,
                fields={'status': 'mismatch'},
            )
        except Exception:  # noqa: BLE001
            pass
        return Response(
            {'message': 'Amount mismatch'}, status=status.HTTP_400_BAD_REQUEST
        )

    with transaction.atomic():
        # Re-read under lock so two concurrent deliveries cannot both credit.
        locked = Payment.objects.select_for_update().filter(
            reference=reference
        ).first()
        if locked is None or locked.status == 'success':
            return Response({'message': 'Already processed'})

        profile, _ = Profile.objects.select_for_update().get_or_create(user_id=locked.user_id)
        profile.available_balance = Decimal(profile.available_balance) + locked.credit_amount
        profile.save(update_fields=['available_balance'])
        if Decimal(profile.available_balance) < 0:
            try:
                from monitoring.activity import send_activity_on_commit

                send_activity_on_commit(
                    f'Wallet balance went negative | user_id: {locked.user_id} | balance: {profile.available_balance}',
                    level='warning',
                    user_id=locked.user_id,
                    dedupe_key=f'negative-balance:{locked.user_id}',
                    cooldown_seconds=3600,
                    fields={'balance': profile.available_balance},
                )
            except Exception:  # noqa: BLE001
                pass

        locked.status = 'success'
        locked.verified_at = timezone.now()
        locked.paystack_response = verified.get('data', {})
        locked.save(update_fields=['status', 'verified_at', 'paystack_response'])

        _log(
            locked.user,
            'wallet.credited',
            locked.user.email,
            {
                'amount': str(locked.credit_amount),
                'fee': str(locked.fee),
                'reference': reference,
            },
        )

    from .email_service import send_template_email  # local import to avoid cycle

    send_template_email(
        'deposit_confirmation',
        locked.user.email,
        {
            'name': locked.user.first_name or 'there',
            'amount': f'{locked.credit_amount}',
            'currency': 'NGN',
            'reference': reference,
            'status': 'success',
        },
        defer=True,
    )

    return Response({'message': 'Received'})

def _withdrawal_serialize(w):
    return {
        'id': w.id,
        'amount': str(w.amount),
        'fee': str(w.fee),
        'netAmount': str(w.net_amount),
        'bankName': w.bank_name,
        'accountNumber': w.account_number,
        'accountName': w.account_name,
        'status': w.status,
        'reviewNote': w.review_note,
        'createdAt': w.created_at.isoformat(),
        'reviewedAt': w.reviewed_at.isoformat() if w.reviewed_at else None,
    }


@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def list_withdrawals(request):
    """Return the authenticated user's withdrawal requests."""
    items = request.user.withdrawals.all()
    return Response([_withdrawal_serialize(w) for w in items])


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsKYCVerified])
def create_withdrawal(request):
    """Request a withdrawal. The percentage service charge is deducted from
    the requested amount; the user must have enough in their wallet.
    Identity verification is required (enforced by IsKYCVerified).
    Body: { amount, bankName, accountNumber, accountName }"""
    amount = parse_amount(request.data.get('amount'))
    if amount is None:
        return Response(
            {'message': 'Amount is invalid.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)

    min_amount = Decimal(str(settings.MIN_WITHDRAWAL))
    if amount < min_amount:
        return Response(
            {'message': f'Minimum withdrawal is {min_amount} NGN.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    bank_name = (request.data.get('bankName') or '').strip()
    account_number = (request.data.get('accountNumber') or '').strip()
    account_name = (request.data.get('accountName') or '').strip()
    if not bank_name or not account_number or not account_name:
        return Response(
            {'message': 'Bank name, account number and account name are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # The balance check and the deduction must be one indivisible step, or two
    # concurrent requests each pass the check and drive the balance negative.
    with transaction.atomic():
        profile, _ = Profile.objects.select_for_update().get_or_create(
            user=request.user
        )
        available = Decimal(profile.available_balance)
        if amount > available:
            return Response(
                {'message': 'Insufficient balance for this withdrawal.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        fee = compute_fee(amount, settings.WITHDRAWAL_FEE_PERCENT)
        net_amount = amount - fee

        # Hold the funds immediately (deduct from available balance).
        profile.available_balance = available - amount
        profile.save(update_fields=['available_balance'])

        withdrawal = Withdrawal.objects.create(
            user=request.user,
            amount=amount,
            fee=fee,
            net_amount=net_amount,
            bank_name=bank_name[:120],
            account_number=account_number[:20],
            account_name=account_name[:120],
            status='pending',
        )
        remaining = profile.available_balance
        try:
            from monitoring.activity import is_large_withdrawal, send_activity_on_commit

            large = is_large_withdrawal(amount)
            title = 'Large withdrawal request' if large else 'Withdrawal request created'
            send_activity_on_commit(
                f'{title} | user_id: {request.user.pk} | amount: {amount} | currency: NGN',
                level='warning' if large else 'info',
                user_id=request.user.pk,
                dedupe_key=f'withdrawal-request:{withdrawal.pk}',
                cooldown_seconds=3600,
                fields={'amount': amount, 'currency': 'NGN'},
            )
            if Decimal(profile.available_balance) < 0:
                send_activity_on_commit(
                    f'Wallet balance went negative | user_id: {request.user.pk} | balance: {profile.available_balance}',
                    level='warning',
                    user_id=request.user.pk,
                    dedupe_key=f'negative-balance:{request.user.pk}',
                    cooldown_seconds=3600,
                    fields={'balance': profile.available_balance},
                )
        except Exception:  # noqa: BLE001 - alerting must never fail a withdrawal
            pass

    return Response(
        {
            'message': 'Withdrawal request submitted.',
            'withdrawal': _withdrawal_serialize(withdrawal),
            'availableBalance': str(remaining),
        },
        status=status.HTTP_201_CREATED,
    )
