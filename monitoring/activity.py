"""Activity alerts (Discord #activity-alerts) built on top of monitoring.discord.

Rules:
- Never block/raise; all calls wrapped in _safe.
- Scrub all strings via monitoring.scrubbing (applied by send_discord_alert).
- User id, amounts, currency, counts, status names, symbol, tournament id, event name only.
- IPs shown only as first 8 chars of sha256.
- Dedupe/cooldown per dedupe_key.
- Call send_activity AFTER action succeeded and DB transaction committed (use transaction.on_commit).
"""

from __future__ import annotations

import hashlib
import os
import time
from decimal import Decimal
from typing import Any, Optional

from django.core.cache import cache
from django.db import transaction

from . import config
from .discord import send_discord_alert
from .scrubbing import scrub_text

_LEVEL_MAP = {
    'info': 'info',
    'success': 'success',
    'warning': 'warning',
}

# Fallback in-process cooldown registry: dedupe_key -> last_sent_epoch.
# Django's cache is the primary store so the window is shared by every worker.
_cooldowns = {}


def _safe(call):
    try:
        return call()
    except Exception:
        return False


def _now():
    # Wall-clock time so the value is meaningful across worker processes when
    # the cache backend is shared (Redis, Memcached).
    return time.time()


def _hash_ip(ip: str) -> str:
    ip = (ip or '').strip()
    if not ip:
        return ''
    return hashlib.sha256(ip.encode('utf-8')).hexdigest()[:8]


def _fmt_amount(amount, currency: str = '') -> str:
    try:
        if isinstance(amount, Decimal):
            s = f'{amount}'
        else:
            s = str(amount)
    except Exception:
        s = str(amount)
    cur = (currency or '').strip()
    return f'{s} {cur}'.strip()


def _threshold(name: str, default: str) -> Decimal:
    raw = (os.getenv(name) or '').strip()
    try:
        return Decimal(raw or default)
    except Exception:
        return Decimal(default)


def is_large_deposit(amount) -> bool:
    try:
        return Decimal(str(amount)) >= _threshold('ALERT_LARGE_DEPOSIT', '500')
    except Exception:
        return False


def is_large_withdrawal(amount) -> bool:
    try:
        return Decimal(str(amount)) >= _threshold('ALERT_LARGE_WITHDRAWAL', '500')
    except Exception:
        return False


def is_large_trade(amount) -> bool:
    try:
        return Decimal(str(amount)) >= _threshold('ALERT_LARGE_TRADE', '1000')
    except Exception:
        return False


def format_activity(title: str, **fields) -> str:
    """Consistent format: "<icon> <Title> | key: value | key: value".
    """
    icon = 'ℹ️'
    if 'warning' in (title or '').lower():
        icon = '⚠️'
    elif 'success' in (title or '').lower():
        icon = '✅'
    parts = [f'{icon} {title}'.strip()]
    for k, v in fields.items():
        if v is None or v == '':
            continue
        parts.append(f'{scrub_text(k)}: {scrub_text(v)}')
    return ' | '.join(parts)


def _check_cooldown(dedupe_key: Optional[str], cooldown_seconds: int) -> bool:
    if not dedupe_key or cooldown_seconds <= 0:
        return True
    now = _now()
    cache_key = 'monitoring:activity-cooldown:' + hashlib.sha256(
        dedupe_key.encode('utf-8')
    ).hexdigest()
    try:
        last = cache.get(cache_key)
        if last is not None and now - float(last) < cooldown_seconds:
            return False
        cache.set(cache_key, now, timeout=max(int(cooldown_seconds), 60))
        return True
    except Exception:  # noqa: BLE001 - a cache outage must not silence alerts
        last = _cooldowns.get(dedupe_key)
        if last is not None and now - last < cooldown_seconds:
            return False
        _cooldowns[dedupe_key] = now
        return True


def send_activity(
    message: str,
    level: str = 'info',
    dedupe_key: Optional[str] = None,
    cooldown_seconds: int = 0,
    user_id: Any = None,
    fields: Optional[dict] = None,
    async_send: bool = True,
) -> bool:
    """Send an activity alert; never raises and returns False on any failure."""
    try:
        return _send_activity_impl(
            message,
            level=level,
            dedupe_key=dedupe_key,
            cooldown_seconds=cooldown_seconds,
            user_id=user_id,
            fields=fields,
            async_send=async_send,
        )
    except Exception:  # noqa: BLE001 - alerting must never break a caller
        return False


def _send_activity_impl(
    message: str,
    level: str = 'info',
    dedupe_key: Optional[str] = None,
    cooldown_seconds: int = 0,
    user_id: Any = None,
    fields: Optional[dict] = None,
    async_send: bool = True,
) -> bool:
    """Send an activity alert to DISCORD_ACTIVITY_WEBHOOK_URL."""
    if not config.alerts_enabled():
        return False
    level = (level or 'info').lower()
    if level not in _LEVEL_MAP:
        level = 'info'
    if not _check_cooldown(dedupe_key, cooldown_seconds or 0):
        return False
    message = scrub_text(message)
    if not message:
        return False
    field_list = []
    if fields:
        for k, v in fields.items():
            if v is None or v == '':
                continue
            field_list.append((str(k), str(v)))
    return _safe(
        lambda: send_discord_alert(
            title=message,
            fields=field_list or None,
            level=_LEVEL_MAP[level],
            description='',
            fingerprint=None,
            async_send=async_send,
        )
    )


def send_activity_on_commit(
    message: str,
    level: str = 'info',
    dedupe_key: Optional[str] = None,
    cooldown_seconds: int = 0,
    user_id: Any = None,
    fields: Optional[dict] = None,
) -> bool:
    def _do():
        return send_activity(message, level=level, dedupe_key=dedupe_key, cooldown_seconds=cooldown_seconds, user_id=user_id, fields=fields, async_send=True)

    try:
        transaction.on_commit(_do)
        return True
    except Exception:
        # Never send early: a failed on_commit registration could otherwise
        # alert for a transaction that is about to roll back.
        return False
