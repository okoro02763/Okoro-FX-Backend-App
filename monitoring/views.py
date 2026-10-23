"""Monitoring endpoints: staff test alert + public frontend error intake."""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from . import config
from .alerts import notify_frontend_error
from .discord import send_test_alert


class ClientErrorThrottle(SimpleRateThrottle):
    """Strict per-IP limit for an endpoint that is intentionally unauthenticated.

    The rate is ``monitoring_client`` in ``DEFAULT_THROTTLE_RATES`` (10/min by
    default, overridable in the environment). It is keyed on the client IP --
    behind Render's proxy that is the first ``X-Forwarded-For`` entry, which
    DRF's ``get_ident`` already handles.
    """

    scope = 'monitoring_client'

    def get_cache_key(self, request, view):
        return self.cache_format % {
            'scope': self.scope,
            'ident': self.get_ident(request),
        }


def _allowed_origin(request) -> bool:
    origin = (request.META.get('HTTP_ORIGIN') or '').strip().rstrip('/')
    if not origin:
        # No browser Origin: a server-side call, curl, or a same-origin proxy.
        return True
    return origin in config.client_origins()


def _raw_body(request) -> bytes:
    try:
        return bytes(request._request.body or b'')
    except Exception:  # noqa: BLE001 - a malformed body is simply rejected
        return b''


def _text(data: Any, key: str, limit: int) -> str:
    value = data.get(key) if isinstance(data, dict) else None
    if value is None:
        return ''
    return str(value).strip()[:limit]


@api_view(['POST'])
@permission_classes([IsAdminUser])
def test_alert(request):
    """POST /api/monitoring/test-alert/ -- staff only.

    Sends a clearly-marked message synchronously so the caller gets an honest
    result instead of a "queued" that may never arrive.
    """
    level = _text(request.data, 'level', 16).lower() or 'info'
    if level not in {'error', 'warning', 'info'}:
        level = 'info'
    if not config.alerts_enabled():
        return Response(
            {'detail': 'Discord alerts are disabled (DISCORD_WEBHOOK_URL is not set).'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    sent = send_test_alert(level=level)
    if not sent:
        return Response(
            {'detail': 'The test alert could not be delivered.'},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    return Response({'status': 'sent', 'level': level})


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ClientErrorThrottle])
def client_error(request):
    """POST /api/monitoring/client-error/ -- frontend crash/API-failure intake.

    No auth by design (a crash can happen before/after login), but strictly
    rate limited, size limited, origin checked, and scrubbed before it reaches
    Discord. Always answers 202 for an accepted report: the browser must never
    retry a crash report in a loop.
    """
    if not _allowed_origin(request):
        return Response({'detail': 'Origin not allowed.'}, status=status.HTTP_403_FORBIDDEN)

    raw = _raw_body(request)
    if len(raw) > config.max_client_error_bytes():
        return Response(
            {'detail': 'Payload too large.'},
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    data = request.data if isinstance(request.data, dict) else {}
    message = _text(data, 'message', 2000)
    if not message:
        return Response(
            {'detail': 'message is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    notify_frontend_error(
        message=message,
        stack=_text(data, 'stack', 4000),
        route=_text(data, 'route', 300),
        user_agent=_text(data, 'userAgent', 300),
        app_version=_text(data, 'appVersion', 60),
    )
    return Response({'status': 'accepted'}, status=status.HTTP_202_ACCEPTED)
