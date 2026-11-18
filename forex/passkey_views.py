import json
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
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
        options_to_json,
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
        try:
            from webauthn.helpers import options_to_json
        except Exception:
            options_to_json = None
        WEBAUTHN_AVAILABLE = True
    except Exception:
        WEBAUTHN_AVAILABLE = False


def _rp_id():
    return getattr(settings, 'WEBAUTHN_RP_ID', None) or 'localhost'


def _rp_name():
    return getattr(settings, 'WEBAUTHN_RP_NAME', 'OKORO FX')


def _expected_origins():
    """Origins allowed to run a WebAuthn ceremony.

    A browser only accepts an RP ID that equals, or is a registrable suffix of,
    the page's own origin. The origins trusted to call this API (CORS) are
    therefore also the origins trusted for passkeys. Merging them in means a
    deployment cannot advertise ``localhost`` to a real frontend just because
    ``FRONTEND_URL`` was left unset.
    """
    origins = [
        origin.rstrip('/')
        for origin in (getattr(settings, 'WEBAUTHN_EXPECTED_ORIGINS', None) or [])
        if origin
    ]
    if not origins:
        origin = (
            getattr(settings, 'WEBAUTHN_ORIGIN', None)
            or getattr(settings, 'FRONTEND_URL', 'http://localhost:3000')
        )
        origins = [origin.rstrip('/')]
    for origin in getattr(settings, 'CORS_ALLOWED_ORIGINS', None) or []:
        origin = (origin or '').rstrip('/')
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def _request_origin(request):
    """The origin of the page that made this request, if the browser sent it."""
    origin = (request.headers.get('Origin') or '').rstrip('/')
    if origin:
        return origin
    referer = request.headers.get('Referer') or ''
    if referer:
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            return f'{parsed.scheme}://{parsed.netloc}'
    return ''


def _rp_id_for_request(request):
    """Return the RP ID matching the page, when its origin is allow-listed.

    WebAuthn requires the RP ID to be the page's host or a registrable suffix
    of it. Deriving it from the (allow-listed) Origin avoids the classic
    "rpId=localhost on a real domain" failure when a deployment forgets to set
    FRONTEND_URL. An unknown origin falls back to the configured RP ID, so the
    browser still rejects it rather than silently trusting the caller.
    """
    origin = _request_origin(request)
    if origin and origin in _expected_origins():
        return urlparse(origin).hostname or _rp_id()
    return _rp_id()


# WebAuthn state is signed and returned to the client instead of relying on a
# server-side session. The frontend and backend are on different domains in
# production, so the session cookie is a third-party cookie: browsers drop it
# (SameSite=Lax by default), and even with credentials enabled Safari/Chrome
# increasingly block it. It also breaks when more than one worker handles the
# two halves of the ceremony. A short-lived signed challenge is stateless and
# works across origins, instances and cookie policies.
_WEBAUTHN_STATE_SALT = 'forex.webauthn.state'
_WEBAUTHN_STATE_MAX_AGE = 300


def _issue_state(payload):
    return signing.dumps(payload, salt=_WEBAUTHN_STATE_SALT, compress=True)


def _read_state(token):
    if not token:
        return None
    try:
        return signing.loads(
            token, salt=_WEBAUTHN_STATE_SALT, max_age=_WEBAUTHN_STATE_MAX_AGE
        )
    except Exception:
        return None


def _options_response(options, state):
    """Serialise WebAuthn options and attach the signed challenge state."""
    try:
        if options_to_json:
            data = json.loads(options_to_json(options))
        else:
            data = json.loads(options.model_dump_json(by_alias=True))
    except Exception:
        data = json.loads(options_to_json(options)) if options_to_json else {}
    if state:
        data['state'] = state
    return Response(data)


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
        rp_id=_rp_id_for_request(request),
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
    challenge_b64 = bytes_to_base64url(options.challenge)
    state = _issue_state(
        {
            'kind': 'registration',
            'challenge': challenge_b64,
            'user_id': str(user.id),
        }
    )
    try:
        request.session['webauthn_reg_challenge'] = challenge_b64
        request.session['webauthn_reg_user_id'] = str(user.id)
        if hasattr(request.session, 'modified'):
            request.session.modified = True
    except Exception:
        pass
    return _options_response(options, state)


@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def passkey_register_finish(request):
    if not WEBAUTHN_AVAILABLE:
        return Response({'message': 'Passkeys are not available on this server.'}, status=503)
    user = request.user
    state = _read_state(request.data.get('state'))
    if state and state.get('kind') == 'registration':
        if state.get('user_id') != str(user.id):
            return Response(
                {'message': 'Invalid or expired passkey registration session.'},
                status=400,
            )
        challenge_b64 = state.get('challenge')
    else:
        challenge_b64 = request.session.get('webauthn_reg_challenge')
        if (
            not challenge_b64
            or request.session.get('webauthn_reg_user_id') != str(user.id)
        ):
            return Response(
                {'message': 'Invalid or expired passkey registration session.'},
                status=400,
            )
    try:
        body = {
            key: value
            for key, value in request.data.items()
            if key not in ('state', 'device_label')
        }
        verification = verify_registration_response(
            credential=body,
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_origin=_expected_origins(),
            expected_rp_id=_rp_id_for_request(request),
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
        rp_id=_rp_id_for_request(request),
        allow_credentials=allow_credentials or None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    challenge_b64 = bytes_to_base64url(options.challenge)
    state = _issue_state(
        {
            'kind': 'authentication',
            'challenge': challenge_b64,
            'user_id': str(user.id) if user else None,
        }
    )
    try:
        request.session['webauthn_auth_challenge'] = challenge_b64
        if user:
            request.session['webauthn_auth_user_id'] = str(user.id)
        else:
            request.session.pop('webauthn_auth_user_id', None)
        if hasattr(request.session, 'modified'):
            request.session.modified = True
    except Exception:
        pass
    return _options_response(options, state)


@api_view(['POST'])
@permission_classes([AllowAny])
def passkey_login_finish(request):
    if not WEBAUTHN_AVAILABLE:
        return Response({'message': 'Passkeys are not available on this server.'}, status=503)
    state = _read_state(request.data.get('state'))
    if state and state.get('kind') == 'authentication':
        challenge_b64 = state.get('challenge')
    else:
        challenge_b64 = request.session.get('webauthn_auth_challenge')
    if not challenge_b64:
        return Response({'message': 'Invalid or expired passkey login session.'}, status=400)
    try:
        body = {
            key: value
            for key, value in request.data.items()
            if key != 'state'
        }
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
            expected_rp_id=_rp_id_for_request(request),
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
