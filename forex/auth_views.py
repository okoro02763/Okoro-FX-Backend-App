import secrets
import string
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from .authentication import BearerTokenAuthentication
from .permissions import IsAdminUser
from .email_service import send_template_email
from .models import AuditLog, Invite, PasswordResetToken, Profile
from .referral import ensure_referral_code, process_referral

User = get_user_model()

# Base URL of the frontend, used when constructing password-reset links.
FRONTEND_URL = settings.FRONTEND_URL


def _with_profile(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def _send_email(
    subject,
    body,
    to_email,
    action_url=None,
    action_label=None,
    name=None,
    html_template=None,
):
    """Legacy free-form helper kept for backward compatibility.

    Prefer ``email_service.send_template_email`` for transactional emails. This
    thin shim renders through the ``welcome`` template (the codebase's generic
    layout) so callers that only have subject + body keep working.
    """
    return send_template_email(
        'welcome',
        to_email,
        {
            'name': name or 'there',
            'action_url': action_url,
            'action_label': action_label or 'Continue',
            'message': body,
        },
    )


def serialize_user(user):
    profile = _with_profile(user)
    return {
        'id': user.id,
        'name': user.first_name or user.username,
        'email': user.email or user.username,
        'role': profile.role,
        'status': profile.status,
        'referralCode': profile.referral_code,
        'rewardBalance': float(profile.reward_balance),
        'mustChangePassword': profile.must_change_password,
    }


@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    """Sign in with email/password -> { token, user }"""
    email = (request.data.get('email') or '').strip().lower()
    password = request.data.get('password') or ''

    if not email or not password:
        return Response(
            {'message': 'Email and password are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = User.objects.filter(email=email).first()
    if not user or not user.check_password(password):
        return Response(
            {'message': 'Invalid email or password.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    profile = _with_profile(user)
    if profile.status != 'active':
        return Response(
            {'message': f'This account is {profile.status}.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    token, _ = Token.objects.get_or_create(user=user)

    # Login alert -- fire-and-forget, never blocks the sign-in response.
    ip_address = (
        (request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
        or request.META.get('REMOTE_ADDR', '')
    )
    send_template_email(
        'login_alert',
        user.email,
        {
            'name': user.first_name or 'there',
            'ip_address': ip_address or 'Unknown',
            'user_agent': request.META.get('HTTP_USER_AGENT', '') or 'Unknown',
            'login_time': timezone.localtime(),
        },
    )

    return Response({'token': token.key, 'user': serialize_user(user)})


@api_view(['POST'])
@permission_classes([AllowAny])
def signup(request):
    """Create an account -> { token, user } (auto-login) or { message }"""
    name = (request.data.get('name') or '').strip()
    email = (request.data.get('email') or '').strip().lower()
    password = request.data.get('password') or ''

    if not name or not email or not password:
        return Response(
            {'message': 'Name, email and password are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if User.objects.filter(email=email).exists():
        return Response(
            {'message': 'An account with this email already exists.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = User.objects.create_user(
        username=email,
        email=email,
        first_name=name,
        password=password,
    )

    # First registered account becomes admin so the platform can be managed.
    is_first = not User.objects.exclude(id=user.id).exists()
    profile = _with_profile(user)
    if is_first:
        profile.role = 'admin'
        profile.save()

    ensure_referral_code(profile)

    # Apply an optional referral code supplied at signup.
    referral_code = (request.data.get('referralCode') or request.data.get('referral_code') or '').strip()
    if referral_code and not is_first:
        process_referral(user, referral_code)

    token = Token.objects.create(user=user)

    send_template_email(
        'welcome',
        user.email,
        {
            'name': name,
            'action_url': FRONTEND_URL,
            'action_label': 'Open OKORO FX',
        },
        defer=True,
    )

    return Response(
        {'token': token.key, 'user': serialize_user(user)},
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def forgot_password(request):
    """Request a password reset email -> { message }"""
    email = (request.data.get('email') or '').strip().lower()
    if not email:
        return Response(
            {'message': 'Email is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = User.objects.filter(email=email).first()
    if user:
        # Generate a one-time reset token and email a link to the frontend.
        token = PasswordResetToken.objects.create(
            user=user,
            key=secrets.token_urlsafe(32),
        )
        reset_url = f"{FRONTEND_URL}/reset-password/{token.key}"
        send_template_email(
            'password_reset',
            user.email,
            {
                'name': user.first_name or 'there',
                'action_url': reset_url,
                'action_label': 'Reset Password',
            },
            defer=True,
        )

    return Response(
        {'message': 'If that email is registered, a reset link has been sent.'}
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def reset_password(request):
    """Consume a reset token and set a new password -> { message }"""
    key = (request.data.get('token') or '').strip()
    password = request.data.get('password') or ''

    if not key or not password:
        return Response(
            {'message': 'Token and new password are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    reset = PasswordResetToken.objects.filter(key=key, used=False).first()
    if not reset:
        return Response(
            {'message': 'Invalid or expired reset token.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    reset.user.set_password(password)
    reset.user.save()
    reset.used = True
    reset.save()

    send_template_email(
        'password_changed',
        reset.user.email,
        {'name': reset.user.first_name or 'there'},
        defer=True,
    )

    return Response({'message': 'Your password has been reset.'})


@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def me(request):
    """Return the currently authenticated user (with role/status)."""
    return Response({'user': serialize_user(request.user)})


def _random_password(length=12):
    """Generate a strong temporary password."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _log(actor, action, target='', details=None):
    AuditLog.objects.create(
        actor=actor,
        action=action,
        target=target,
        details=details or {},
    )


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def invite_user(request):
    """Admin invites an email to become an admin. Sends /signup?invite=<token>."""
    email = (request.data.get('email') or '').strip().lower()
    if not email:
        return Response(
            {'message': 'Email is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if User.objects.filter(email=email).exists():
        return Response(
            {'message': 'A user with this email already exists.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    invite, created = Invite.objects.get_or_create(
        email=email,
        defaults={'token': secrets.token_urlsafe(32), 'invited_by': request.user},
    )
    if not created:
        return Response(
            {'message': 'An invitation for this email is already pending.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    invite_url = f"{FRONTEND_URL}/signup?invite={invite.token}"
    send_template_email(
        'admin_invite',
        email,
        {
            'name': 'there',
            'inviter': request.user.first_name or request.user.email or 'Admin',
            'action_url': invite_url,
            'action_label': 'Accept Invitation',
        },
        defer=True,
    )

    _log(request.user, 'admin.invited', email)

    return Response(
        {
            'message': f'Invitation sent to {email}.',
            'inviteUrl': invite_url,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def accept_invite(request):
    """Burn an invite token, create the admin account with a temp password."""
    token = (request.data.get('invite') or '').strip()
    name = (request.data.get('name') or '').strip()
    email = (request.data.get('email') or '').strip().lower()
    password = request.data.get('password') or ''

    try:
        invite = Invite.objects.get(token=token, accepted=False)
    except Invite.DoesNotExist:
        return Response(
            {'message': 'Invalid or already-used invitation.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if email != invite.email:
        return Response(
            {'message': 'This invitation is for a different email address.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not name or not password:
        return Response(
            {'message': 'Name and password are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if User.objects.filter(email=email).exists():
        return Response(
            {'message': 'An account with this email already exists.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = User.objects.create_user(
        username=email,
        email=email,
        first_name=name,
        password=password,
    )

    profile = _with_profile(user)
    profile.role = 'admin'
    # The invited admin sets their own password here, so no forced change is
    # required. (An existing admin can still reset it via a separate action.)
    profile.must_change_password = False
    profile.save()

    ensure_referral_code(profile)

    invite.accepted = True
    invite.save()

    _log(request.user, 'admin.invite_accepted', email)

    token_obj = Token.objects.create(user=user)
    return Response(
        {'token': token_obj.key, 'user': serialize_user(user)},
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def change_password(request):
    """Set a new password and clear the must-change flag. Body: { password }"""
    password = request.data.get('password') or ''
    if len(password) < 6:
        return Response(
            {'message': 'Password must be at least 6 characters.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    request.user.set_password(password)
    request.user.save()

    profile = _with_profile(request.user)
    profile.must_change_password = False
    profile.save()

    send_template_email(
        'password_changed',
        request.user.email,
        {'name': request.user.first_name or 'there'},
        defer=True,
    )

    return Response({'message': 'Password updated.'})
