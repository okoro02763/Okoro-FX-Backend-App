"""Environment-driven configuration for Discord alerting.

The webhook URL is treated as a password:

* it is never logged, echoed in a response, or written to a file;
* it is never hard-coded -- a missing (or obviously fake) value disables
  alerting instead of producing a stream of delivery failures.
"""

from __future__ import annotations

import os

from django.conf import settings

# Values a developer is likely to copy from docs/.env.example by accident.
_PLACEHOLDER_MARKERS = (
    'paste-your',
    'your-webhook',
    'replace-me',
    'changeme',
    'example.com',
)


def _env(name: str, default: str = '') -> str:
    return (os.getenv(name) or default).strip()


def _clean_webhook(value: str) -> str:
    """Return a usable webhook URL, or '' for a placeholder/invalid value."""
    value = (value or '').strip()
    if not value:
        return ''
    lowered = value.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return ''
    # Discord webhooks are HTTPS only. Rejecting anything else keeps a typo
    # from turning every error into a slow, pointless network call.
    if not lowered.startswith('https://'):
        return ''
    return value


def webhook_url() -> str:
    """The main webhook (errors and, unless configured otherwise, everything)."""
    return _clean_webhook(_env('DISCORD_WEBHOOK_URL'))


def activity_webhook_url() -> str:
    """Optional second channel for info/warning events.

    When set, service-start notices, KYC activity and other non-error events go
    here so the error channel stays quiet enough to keep notifications on.
    Falls back to the main webhook when unset.
    """
    return _clean_webhook(_env('DISCORD_ACTIVITY_WEBHOOK_URL'))


def alerts_enabled() -> bool:
    """True when a valid webhook exists and the kill switch is not off.

    ``DISCORD_ALERTS_ENABLED`` defaults to true once a URL is configured; an
    explicit false/0/no/off disables everything without deleting the URL.
    """
    if not webhook_url():
        return False
    flag = _env('DISCORD_ALERTS_ENABLED', 'true').lower()
    return flag not in {'false', '0', 'no', 'off', 'disabled'}


def app_env() -> str:
    """``production`` / ``staging`` / ``local`` label shown on every alert."""
    explicit = _env('APP_ENV')
    if explicit:
        return explicit
    try:
        return 'local' if settings.DEBUG else 'production'
    except Exception:  # settings may not be configured in a tiny script
        return 'unknown'


def commit_sha(short: bool = True) -> str:
    """Commit hash injected by Render (or any CI), never a secret."""
    sha = _env('RENDER_GIT_COMMIT') or _env('GIT_COMMIT') or _env('COMMIT_SHA')
    if not sha:
        return 'unknown'
    return sha[:7] if short else sha


def client_origins() -> set[str]:
    """Origins allowed to post frontend errors.

    Browsers always attach ``Origin`` to a cross-origin POST. A request with no
    Origin (curl, an uptime probe, a server-side renderer) is allowed through;
    an unexpected browser origin is rejected.
    """
    raw = _env(
        'MONITORING_CLIENT_ORIGINS',
        'https://okorofx.netlify.app,'
        'https://forex-trader-frontend.vercel.app,'
        'http://localhost:3000,'
        'http://localhost:5173',
    )
    return {origin.rstrip('/') for origin in raw.split(',') if origin.strip()}


def max_client_error_bytes() -> int:
    try:
        return max(1024, int(_env('MONITORING_CLIENT_ERROR_MAX_BYTES', '8192')))
    except ValueError:
        return 8192
