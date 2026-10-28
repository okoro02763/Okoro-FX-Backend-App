import json
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .authentication import BearerTokenAuthentication
from .models import WebAuthnCredential, SecurityEvent
from .auth_views import serialize_user, _get_client_ip

User = get_user_model()

try:
    from webauthn import (
        generate_authentication_options,
        generate_registration_options,
        verify_authentication_response,
        verify_registration_response,
    )
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
    from webauthn.helpers.structs import (
        AttestationConveyancePreference,
        AuthenticatorSelectionCriteria,
        AuthenticatorAttachment,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
    WEBAUTHN_AVAILABLE = True
except Exception:
    WEBAUTHN_AVAILABLE = False


def _rp_id():
    return getattr(settings, 'WEBAUTHN_RP_ID', None) or 'localhost'


def _rp_name():
    return getattr(settings, 'WEBAUTHN_RP_NAME', 'OKORO FX')


def _expected_origins():
    origins = getattr(settings, 'WEBAUTHN_EXPECTED_ORIGINS', None)
    if origins:
        return origins
    origin = getattr(settings, 'WEBAUTHN_ORIGIN', None) or getattr(settings, 'FRONTEND_URL', 'http://localhost:3000')
    return [origin]


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def passkey_register_begin(request):
    if not WEBAUTHN_AVAILABLE:
        return Response({'message': 'Passkeys are not available on this server.'}, status=503)
    user = request.user
    exclude = []
    for cred in WebAuthnCredential.objects.filter(user=user):
        try:
            exclude.append(PublicKeyCredentialDescriptor(id=base64url_to_bytes(cred.credential_id)))
        except Exception:
            continue
    options = generate_registration_options(
        rp_id=_rp_id(),
        rp_name=_rp_name(),
        user_id=str(user.id).encode('utf-8'),
        user_name=user.email or user.username,
        user_display_name=user.first_name or user.email or user.username,
        exclude_credentials=exclude or None,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
        ),
        attestation=AttestationConveyancePreference.NONE,
    )
    request.session['webauthn_reg_challenge'] = bytes_to_base64url(options.challenge)
    request.session['webauthn_reg_user_id'] = user.id
    request.session.modified = True
    return Response(json.loads(options.model_dump_json(by_alias=True)))


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def passkey_register_finish(request):
    if not WEBAUTHN_AVAILABLE:
        return Response({'message': 'Passkeys are not available on this server.'}, status=503)
    user = request.user
    challenge_b64 = request.session.get('webauthn_reg_challenge')
    if not challenge_b64 or request.session.get('webauthn_reg_user_id') != user.id:
        return Response({'message': 'Invalid or expired passkey registration session.'}, status=400)
    try:
        body = request.data
        verification = verify_registration_response(
            credential=body,
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_origin=_expected_origins(),
            expected_rp_id=_rp_id(),
        )
        credential_id = bytes_to_base64url(verification.credential_id)
        if WebAuthnCredential.objects.filter(credential_id=credential_id).exists():
            request.session.pop('webauthn_reg_challenge', None)
            request.session.pop('webauthn_reg_user_id', None)
            return Response({'message': 'This passkey is already registered.'}, status=400)
        device_label = request.data.get('device_label') or ''
        WebAuthnCredential.objects.create(
            user=user,
            credential_id=credential_id,
            public_key=bytes_to_base64url(verification.credential_public_key),
            sign_count=verification.sign_count or 0,
            device_label=device_label,
        )
        request.session.pop('webauthn_reg_challenge', None)
        request.session.pop('webauthn_reg_user_id', None)
        try:
            SecurityEvent.objects.create(
                user=user,
                event_type='passkey_registered',
                details={'credential_id': credential_id[:16]},
                ip_address=_get_client_ip(request),
            )
        except Exception:
            pass
        return Response({'message': 'Passkey registered successfully.'})
    except Exception as e:
        return Response({'message': f'Passkey registration failed: {e}'}, status=400)


@api_view(['POST'])
@permission_classes([AllowAny])
def passkey_login_begin(request):
    if not WEBAUTHN_AVAILABLE:
        return Response({'message': 'Passkeys are not available on this server.'}, status=503)
    email = (request.data.get('email') or '').strip().lower()
    user = None
    allow_credentials = []
    if email:
        user = User.objects.filter(email=email).first()
        if user:
            for cred in WebAuthnCredential.objects.filter(user=user):
                try:
                    allow_credentials.append(PublicKeyCredentialDescriptor(id=base64url_to_bytes(cred.credential_id)))
                except Exception:
                    continue
    options = generate_authentication_options(
        rp_id=_rp_id(),
        allow_credentials=allow_credentials or None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    request.session['webauthn_auth_challenge'] = bytes_to_base64url(options.challenge)
    if user:
        request.session['webauthn_auth_user_id'] = user.id
    else:
        request.session.pop('webauthn_auth_user_id', None)
    request.session.modified = True
    return Response(json.loads(options.model_dump_json(by_alias=True)))


@api_view(['POST'])
@permission_classes([AllowAny])
def passkey_login_finish(request):
    if not WEBAUTHN_AVAILABLE:
        return Response({'message': 'Passkeys are not available on this server.'}, status=503)
    challenge_b64 = request.session.get('webauthn_auth_challenge')
    if not challenge_b64:
        return Response({'message': 'Invalid or expired passkey login session.'}, status=400)
    try:
        body = request.data
        cred_id = body.get('id') or body.get('rawId')
        if not cred_id:
            return Response({'message': 'Invalid credential.'}, status=400)
        cred = WebAuthnCredential.objects.filter(credential_id=cred_id).first()
        if not cred:
            try:
                # try to match
                for c in WebAuthnCredential.objects.all()[:200]:
                    if c.credential_id == cred_id or cred_id in c.credential_id:
                        cred = c
                        break
            except Exception:
                pass
        if not cred:
            return Response({'message': 'Passkey not recognized.'}, status=400)
        verification = verify_authentication_response(
            credential=body,
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_origin=_expected_origins(),
            expected_rp_id=_rp_id(),
            credential_public_key=base64url_to_bytes(cred.public_key),
            credential_current_sign_count=cred.sign_count,
        )
        if verification.new_sign_count is not None:
            cred.sign_count = verification.new_sign_count
            cred.save(update_fields=['sign_count'])
        user = cred.user
        profile = getattr(user, 'profile', None)
        if profile and getattr(profile, 'status', 'active') != 'active':
            return Response({'message': f'This account is {profile.status}.'}, status=403)
        token, _ = Token.objects.get_or_create(user=user)
        request.session.pop('webauthn_auth_challenge', None)
        request.session.pop('webauthn_auth_user_id', None)
        try:
            from .auth_views import _get_client_ip as _cip

            SecurityEvent.objects.create(
                user=user,
                event_type='login_passkey',
                details={'credential_id': cred.credential_id[:16]},
                ip_address=_cip(request),
            )
        except Exception:
            pass
        return Response({'token': token.key, 'user': serialize_user(user)})
    except Exception as e:
        return Response({'message': f'Passkey login failed: {e}'}, status=400)
