from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    permission_classes,
    authentication_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .authentication import BearerTokenAuthentication
from .permissions import IsAdminUser
from .models import AuditLog, FraudResult, Profile, Trade, IdentityVerification, Withdrawal
from .fraud_engine import recompute_for_user

User = get_user_model()


def _with_profile(user):
    """Ensure a Profile row exists for a user."""
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def _log(actor, action, target='', details=None):
    AuditLog.objects.create(
        actor=actor,
        action=action,
        target=target,
        details=details or {},
    )


def _fraud_for(user):
    try:
        fraud = user.fraud
        return {
            'score': fraud.score,
            'level': fraud.level,
            'reasons': fraud.reasons or [],
        }
    except FraudResult.DoesNotExist:
        return {'score': 0, 'level': 'low', 'reasons': []}


def _serialize_user(user, stats=None):
    profile = _with_profile(user)
    fraud = _fraud_for(user)
    if stats is None:
        stats = {
            'tradeCount': Trade.objects.filter(
                currency_pair__isnull=False
            ).count(),
        }
    return {
        'id': user.id,
        'name': user.first_name or user.username,
        'email': user.email or user.username,
        'role': profile.role,
        'status': profile.status,
        'createdAt': user.date_joined.isoformat(),
        'tradeCount': stats['tradeCount'],
        'fraud': fraud,
    }


@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def list_users(request):
    """Return all users with status, role and fraud scores."""
    users = User.objects.all().order_by('-date_joined')

    # Overall trade count is platform-global today (trades are not user-bound).
    total_trades = Trade.objects.count()

    data = [
        _serialize_user(
            user,
            stats={'tradeCount': total_trades},
        )
        for user in users
    ]

    data.sort(key=lambda u: u['fraud']['score'], reverse=True)
    return Response(data)


@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def list_audit_logs(request):
    """Return recent admin audit log entries."""
    logs = AuditLog.objects.all()[:100]
    data = [
        {
            'id': log.id,
            'actor': log.actor.email if log.actor else 'system',
            'action': log.action,
            'target': log.target,
            'details': log.details,
            'createdAt': log.created_at.isoformat(),
        }
        for log in logs
    ]
    return Response(data)


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def change_user_status(request, user_id):
    """Suspend, activate or ban a user. Body: { status: active|suspended|banned }"""
    status_val = (request.data.get('status') or '').strip().lower()
    valid = {'active', 'suspended', 'banned'}
    if status_val not in valid:
        return Response(
            {'message': 'status must be one of active, suspended, banned.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response(
            {'message': 'User not found.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    profile = _with_profile(user)
    profile.status = status_val
    profile.save()

    _log(request.user, 'user.status_changed', user.email, {'status': status_val})

    return Response({'id': user.id, 'status': profile.status})


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def change_user_role(request, user_id):
    """Promote/demote a user. Body: { role: user|admin }"""
    role = (request.data.get('role') or '').strip().lower()
    if role not in {'user', 'admin'}:
        return Response(
            {'message': 'role must be one of user, admin.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response(
            {'message': 'User not found.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    profile = _with_profile(user)
    profile.role = role
    profile.save()

    _log(request.user, 'user.role_changed', user.email, {'role': role})

    return Response({'id': user.id, 'role': profile.role})


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def recompute_fraud(request, user_id):
    """Re-run the fraud engine for a user and persist the result."""
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return Response(
            {'message': 'User not found.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    result = recompute_for_user(user)
    _log(request.user, 'fraud.recomputed', user.email, {'score': result.score})

    return Response(
        {
            'id': user.id,
            'score': result.score,
            'level': result.level,
            'reasons': result.reasons,
        }
    )


@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def fraud_scores(request):
    """Return per-user fraud scores (admin only)."""
    users = User.objects.all()
    data = []
    frauds = FraudResult.objects.select_related('user')
    fraud_map = {f.user_id: f for f in frauds}
    for user in users:
        fraud = fraud_map.get(user.id)
        data.append(
            {
                'id': user.id,
                'name': user.first_name or user.username,
                'email': user.email or user.username,
                'score': fraud.score if fraud else 0,
                'level': fraud.level if fraud else 'low',
                'reasons': fraud.reasons if fraud else [],
                'updatedAt': fraud.updated_at.isoformat() if fraud else None,
            }
        )
    data.sort(key=lambda u: u['score'], reverse=True)
    return Response(data)


def _kyc_serialize(v):
    return {
        'id': v.id,
        'userId': v.user_id,
        'name': v.user.first_name or v.user.username,
        'email': v.user.email or v.user.username,
        'docType': v.doc_type,
        'docNumber': v.doc_number,
        'docImage': v.doc_image.url if v.doc_image else None,
        'status': v.status,
        'reviewNote': v.review_note,
        'submittedAt': v.submitted_at.isoformat(),
        'reviewedAt': v.reviewed_at.isoformat() if v.reviewed_at else None,
    }


@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def list_kyc(request):
    """Return all identity-verification submissions (pending first)."""
    items = IdentityVerification.objects.select_related('user').order_by(
        '-submitted_at'
    )
    data = [_kyc_serialize(v) for v in items]
    data.sort(key=lambda v: 0 if v['status'] == 'pending' else 1)
    return Response(data)


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def review_kyc(request, verification_id):
    """Approve or reject a document. Body: { status: approved|rejected, note? }"""
    new_status = (request.data.get('status') or '').strip().lower()
    note = (request.data.get('note') or '').strip()

    if new_status not in {'approved', 'rejected'}:
        return Response(
            {'message': 'status must be one of approved, rejected.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        verification = IdentityVerification.objects.get(id=verification_id)
    except IdentityVerification.DoesNotExist:
        return Response(
            {'message': 'Verification not found.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    verification.status = new_status
    verification.review_note = note
    verification.reviewed_by = request.user
    verification.reviewed_at = timezone.now()
    verification.save()

    _log(
        request.user,
        f'kyc.{new_status}',
        verification.user.email,
        {'doc_type': verification.doc_type, 'doc_number': verification.doc_number},
    )

    return Response(_kyc_serialize(verification))


def _withdrawal_serialize(w):
    return {
        'id': w.id,
        'userId': w.user_id,
        'name': w.user.first_name or w.user.username,
        'email': w.user.email or w.user.username,
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
@permission_classes([IsAuthenticated, IsAdminUser])
def list_withdrawals_admin(request):
    """Return all withdrawal requests (pending first)."""
    items = Withdrawal.objects.select_related('user').order_by('-created_at')
    data = [_withdrawal_serialize(w) for w in items]
    data.sort(key=lambda w: 0 if w['status'] == 'pending' else 1)
    return Response(data)


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def review_withdrawal(request, withdrawal_id):
    """Approve, reject or mark a withdrawal as paid.
    Body: { status: approved|rejected|paid, note? }"""
    new_status = (request.data.get('status') or '').strip().lower()
    note = (request.data.get('note') or '').strip()

    if new_status not in {'approved', 'rejected', 'paid'}:
        return Response(
            {'message': 'status must be one of approved, rejected, paid.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Valid transitions only. Without this a rejected (already refunded)
    # withdrawal can be re-approved and paid out with no balance hold.
    ALLOWED_TRANSITIONS = {
        'pending': {'approved', 'rejected'},
        'approved': {'paid', 'rejected'},
        'rejected': set(),
        'paid': set(),
    }

    try:
        withdrawal = Withdrawal.objects.get(id=withdrawal_id)
    except Withdrawal.DoesNotExist:
        return Response(
            {'message': 'Withdrawal not found.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    if new_status not in ALLOWED_TRANSITIONS[withdrawal.status]:
        return Response(
            {
                'message': (
                    f'Cannot move a {withdrawal.status} withdrawal to {new_status}.'
                ),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Lock the row so two concurrent reviews cannot both observe `pending`
    # and both issue a refund.
    with transaction.atomic():
        withdrawal = (
            Withdrawal.objects.select_for_update()
            .select_related('user')
            .get(id=withdrawal_id)
        )
        if new_status not in ALLOWED_TRANSITIONS[withdrawal.status]:
            return Response(
                {'message': 'Withdrawal was already reviewed.'},
                status=status.HTTP_409_CONFLICT,
            )

        # Only a request that still holds funds can refund them.
        if new_status == 'rejected' and withdrawal.status == 'pending':
            profile = Profile.objects.select_for_update().get(
                user_id=withdrawal.user_id
            )
            profile.available_balance = (
                Decimal(profile.available_balance) + Decimal(withdrawal.amount)
            )
            profile.save(update_fields=['available_balance'])

        withdrawal.status = new_status
        withdrawal.review_note = note
        withdrawal.reviewed_by = request.user
        withdrawal.reviewed_at = timezone.now()
        withdrawal.save()

    from .email_service import send_template_email

    send_template_email(
        'withdrawal_update',
        withdrawal.user.email,
        {
            'name': withdrawal.user.first_name or 'there',
            'amount': f'{withdrawal.amount}',
            'net_amount': f'{withdrawal.net_amount}',
            'reference': str(withdrawal.id),
            'status': new_status,
            'status_note': note,
        },
        defer=True,
    )

    _log(
        request.user,
        f'withdrawal.{new_status}',
        withdrawal.user.email,
        {'amount': str(withdrawal.amount), 'reference': withdrawal.id},
    )

    return Response(_withdrawal_serialize(withdrawal))


# Local (non-API) helper reused elsewhere.
def user_has_approved_kyc(user):
    from kyc.services import user_is_verified

    return user_is_verified(user)
