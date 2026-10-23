"""Monitoring endpoints: staff test alert + public frontend error intake."""

from __future__ import annotations

import hmac
import io
import logging
import os
from typing import Any

from django.core.management import call_command
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

logger = logging.getLogger(__name__)


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


def _cron_authorised(request) -> bool:
    """Constant-time check of ``X-Cron-Secret`` against ``CRON_SECRET``.

    A missing server secret disables the endpoint entirely (404) rather than
    accepting an empty header. The response never reveals which check failed.
    """
    secret = (os.getenv('CRON_SECRET') or '').strip()
    if not secret:
        return False
    supplied = (request.headers.get('X-Cron-Secret') or '').strip()
    return hmac.compare_digest(supplied, secret)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def run_checks(request):
    """POST /api/monitoring/run-checks/ -- secret-header scheduled checks.

    ``?task=check_pending_alerts`` (default) or ``?task=daily_summary``.
    The daily summary is only available when ``ENABLE_DAILY_SUMMARY`` is true.
    Missing or wrong secret, and a disabled task, all return 404 so the
    endpoint is indistinguishable from a non-existent route.
    """
    if not _cron_authorised(request):
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    task = (request.query_params.get('task') or 'check_pending_alerts').strip().lower()
    if task in {'check_pending_alerts', 'pending', ''}:
        command = 'check_pending_alerts'
    elif task == 'daily_summary':
        enabled = (os.getenv('ENABLE_DAILY_SUMMARY') or '').strip().lower()
        if enabled not in {'true', '1', 'yes', 'on'}:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        command = 'daily_summary'
    else:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        call_command(command, stdout=io.StringIO(), verbosity=0)
    except Exception:  # noqa: BLE001 - a scheduler call must never 500 loudly
        logger.exception('Scheduled monitoring command %s failed', command)
        return Response(
            {'detail': 'Check failed.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response({'status': 'ok', 'task': command})
