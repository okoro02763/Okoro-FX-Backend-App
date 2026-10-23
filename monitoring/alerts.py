"""Business-event alerts.

These are the non-exception signals worth a human's attention: a KYC document
was submitted, a user is being brute-forced, a third-party provider is failing,
Celery is broken, or the deployed schema is behind. Each one is a single short
message and is rate-limited by the standard dedupe in
:mod:`monitoring.discord`.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque
from typing import Any, Iterable, Optional

from . import config
from .discord import claim_persistent_slot, send_discord_alert, time_labels

LOGIN_FAILURE_THRESHOLD = 5
LOGIN_FAILURE_WINDOW_SECONDS = 10 * 60
SERVICE_START_WINDOW_SECONDS = 10 * 60

_login_lock = threading.Lock()
_login_failures: dict[str, deque[float]] = defaultdict(deque)


def _digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:length]


def _safe(call) -> bool:
    try:
        return call()
    except Exception:  # noqa: BLE001 - none of these may break the caller
        return False


# ---------------------------------------------------------------------------
# Startup / deploy
# ---------------------------------------------------------------------------

def notify_service_started() -> bool:
    """Announce a boot with the deployed commit (restart loops are suppressed)."""
    if not config.alerts_enabled():
        return False
    if not claim_persistent_slot('service-started', SERVICE_START_WINDOW_SECONDS):
        return False
    labels = time_labels()
    return _safe(
        lambda: send_discord_alert(
            title='Service started',
            fields=[
                ('Environment', config.app_env()),
                ('Commit', config.commit_sha()),
                ('Time (UTC)', labels['utc']),
                ('Time (Lagos)', labels['lagos']),
            ],
            level='info',
            description='The backend process is up.',
            fingerprint='service-started',
        )
    )


def notify_pending_migrations(pending: Iterable[str]) -> bool:
    """Warn that the deployed code expects migrations the DB does not have."""
    names = [str(item) for item in pending if item]
    if not names:
        return False
    fingerprint = 'pending-migrations:' + _digest(','.join(sorted(names)), 12)
    return _safe(
        lambda: send_discord_alert(
            title='Migrations pending at startup',
            fields=[
                ('Environment', config.app_env()),
                ('Commit', config.commit_sha()),
                ('Count', len(names)),
                ('Pending', ', '.join(names)),
            ],
            level='warning',
            description='The service booted with a schema that is behind the code.',
            fingerprint=fingerprint,
        )
    )


# ---------------------------------------------------------------------------
# KYC / account activity
# ---------------------------------------------------------------------------

def notify_kyc_submitted(user_id: Any) -> bool:
    """A customer submitted identity documents. User id only -- no KYC data."""
    if user_id in (None, ''):
        return False
    return _safe(
        lambda: send_discord_alert(
            title='KYC submission received',
            fields=[('Event', 'KYC submitted for review'), ('User ID', user_id)],
            level='warning',
            fingerprint=f'kyc-submitted:{user_id}',
        )
    )


def notify_login_failure(user_id: Any) -> bool:
    """Alert after 5 failed logins for one user inside 10 minutes."""
    if user_id in (None, ''):
        return False
    key = str(user_id)
    now = time.monotonic()
    with _login_lock:
        failures = _login_failures[key]
        failures.append(now)
        while failures and now - failures[0] > LOGIN_FAILURE_WINDOW_SECONDS:
            failures.popleft()
        if len(failures) < LOGIN_FAILURE_THRESHOLD:
            return False
        count = len(failures)
        failures.clear()

    return _safe(
        lambda: send_discord_alert(
            title='Repeated login failures',
            fields=[
                ('Event', 'Possible credential stuffing'),
                ('User ID', user_id),
                ('Failures', f'{count} in {LOGIN_FAILURE_WINDOW_SECONDS // 60} minutes'),
            ],
            level='warning',
            fingerprint=f'login-failures:{user_id}',
        )
    )


# ---------------------------------------------------------------------------
# Third-party providers / background jobs
# ---------------------------------------------------------------------------

def notify_third_party_failure(
    provider: str,
    detail: str = '',
    status_code: Optional[int] = None,
) -> bool:
    """A provider failed after the app's own fallbacks were exhausted."""
    provider = (provider or 'unknown').strip()
    fields = [
        ('Provider', provider),
        ('Environment', config.app_env()),
    ]
    if status_code is not None:
        fields.append(('HTTP status', status_code))
    if detail:
        fields.append(('Detail', detail))
    return _safe(
        lambda: send_discord_alert(
            title=f'Third-party failure: {provider}',
            fields=fields,
            level='warning',
            fingerprint=f'third-party:{_digest(provider, 12)}',
        )
    )


def notify_celery_failure(task_name: str, exception: Any = None) -> bool:
    """A Celery task raised (the worker logged it, but nobody reads workers)."""
    task_name = (task_name or 'unknown task').strip()
    fields = [
        ('Task', task_name),
        ('Environment', config.app_env()),
        ('Commit', config.commit_sha()),
    ]
    if exception is not None:
        fields.append(('Exception', type(exception).__name__))
        fields.append(('Message', str(exception)))
    return _safe(
        lambda: send_discord_alert(
            title='Celery task failed',
            fields=fields,
            level='warning',
            fingerprint=f'celery:{_digest(task_name, 12)}',
        )
    )


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

def notify_frontend_error(
    message: str,
    stack: str = '',
    route: str = '',
    user_agent: str = '',
    app_version: str = '',
) -> bool:
    """Forward a browser crash / failed API call. Scrubbed again on the way out."""
    message = (message or '').strip()
    if not message:
        return False
    stack_lines = [line for line in (stack or '').splitlines() if line.strip()][:10]
    fields = [
        ('Route', route or 'unknown'),
        ('Message', message),
        ('App version', app_version or 'unknown'),
        ('User agent', user_agent or 'unknown'),
        ('Commit', config.commit_sha()),
        ('Environment', config.app_env()),
    ]
    if stack_lines:
        fields.append(('Stack (first 10 lines)', '\n'.join(stack_lines)))
    fingerprint = 'frontend:' + _digest(f'{route}|{message[:160]}', 16)
    return _safe(
        lambda: send_discord_alert(
            title='Frontend error',
            fields=fields,
            level='error',
            fingerprint=fingerprint,
        )
    )
