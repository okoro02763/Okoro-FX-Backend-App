"""Discord webhook transport, deduplication and throttling.

Design rules (all enforced here, not by the callers):

* ``send_discord_alert`` never raises and never blocks the caller -- delivery
  happens on a single background daemon worker.
* The same fingerprint is sent at most once per :data:`DEDUPE_WINDOW_SECONDS`;
  suppressed occurrences are counted and reported in the next message.
* A global cap of :data:`GLOBAL_MAX_ALERTS` alerts per minute is enforced; the
  excess collapses into one "Too many errors, alerts throttled" message.
* Every string is scrubbed (:mod:`monitoring.scrubbing`) and truncated to
  Discord's embed limits.
* The webhook URL is read from the environment and is never logged.
"""

from __future__ import annotations

import hashlib
import os
import queue
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

import requests

from . import config
from .scrubbing import scrub_payload, scrub_text

LEVEL_COLORS = {
    'error': 0xE74C3C,    # red
    'warning': 0xE67E22,  # orange
    'info': 0x3498DB,     # blue
}
VALID_LEVELS = frozenset(LEVEL_COLORS)

DEDUPE_WINDOW_SECONDS = 10 * 60
GLOBAL_WINDOW_SECONDS = 60
GLOBAL_MAX_ALERTS = 20
REQUEST_TIMEOUT_SECONDS = 3
QUEUE_MAXSIZE = 200

EMBED_DESCRIPTION_LIMIT = 4000
FIELD_VALUE_LIMIT = 1000
FIELD_NAME_LIMIT = 256
TITLE_LIMIT = 256
MAX_FIELDS = 25
# Discord rejects an embed whose combined text exceeds 6000 characters; keep a
# safety margin so a long traceback never causes a 400 instead of an alert.
TOTAL_EMBED_BUDGET = 5500

# Lagos has no daylight saving time, so a fixed +01:00 fallback is exact and
# works on hosts whose tz database is missing (notably Windows without tzdata).
_LAGOS_FALLBACK = timezone(timedelta(hours=1), name='Africa/Lagos')


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now() -> float:
    """Monotonic clock, isolated so tests can advance it deterministically."""
    return time.monotonic()


def _lagos_timezone():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo('Africa/Lagos')
    except Exception:  # noqa: BLE001 - a missing tzdata must not break alerts
        return _LAGOS_FALLBACK


def time_labels(moment: Optional[datetime] = None) -> dict[str, str]:
    """Return UTC and Africa/Lagos renderings of ``moment`` (now by default)."""
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    utc = moment.astimezone(timezone.utc)
    lagos = moment.astimezone(_lagos_timezone())
    return {
        'utc': utc.strftime('%Y-%m-%d %H:%M:%S UTC'),
        'lagos': lagos.strftime('%Y-%m-%d %H:%M:%S Africa/Lagos'),
    }


# ---------------------------------------------------------------------------
# Truncation / embed construction
# ---------------------------------------------------------------------------

def _truncate(value: Any, limit: int) -> str:
    text = '' if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + '\u2026'


def _normalise_fields(fields: Any) -> list[tuple[str, Any]]:
    if not fields:
        return []
    if isinstance(fields, Mapping):
        return [(str(k), v) for k, v in fields.items()]
    result: list[tuple[str, Any]] = []
    for item in fields:
        try:
            name, value = item
        except (TypeError, ValueError):
            continue
        result.append((str(name), value))
    return result


def build_embed(
    title: str,
    fields: Any = None,
    level: str = 'error',
    description: str = '',
) -> dict[str, Any]:
    """Build one Discord embed within the documented size limits."""
    level = level if level in VALID_LEVELS else 'error'
    moment = datetime.now(timezone.utc)
    embed: dict[str, Any] = {
        'title': _truncate(scrub_text(title), TITLE_LIMIT),
        'color': LEVEL_COLORS[level],
        'timestamp': moment.isoformat(),
        'footer': {
            'text': _truncate(
                f'{level.upper()} | {config.app_env()} | {config.commit_sha()}',
                2048,
            )
        },
    }
    if description:
        embed['description'] = _truncate(scrub_text(description), EMBED_DESCRIPTION_LIMIT)

    cleaned: list[dict[str, Any]] = []
    # Leave room for the title, description and footer before distributing the
    # remaining budget across fields.
    remaining = (
        TOTAL_EMBED_BUDGET
        - len(embed['title'])
        - len(embed.get('description', ''))
        - len(embed['footer']['text'])
    )
    for name, value in _normalise_fields(fields):
        if value is None or value == '':
            continue
        safe_name = _truncate(scrub_text(name), FIELD_NAME_LIMIT) or 'Field'
        text = _truncate(scrub_text(value), FIELD_VALUE_LIMIT)
        if not text or remaining <= 0:
            break
        if len(text) + len(safe_name) > remaining:
            text = _truncate(text, max(1, remaining - len(safe_name)))
        cleaned.append(
            {
                'name': safe_name,
                'value': text or '-',
                'inline': len(text) < 80,
            }
        )
        remaining -= len(safe_name) + len(text)
        if len(cleaned) >= MAX_FIELDS:
            break
    if cleaned:
        embed['fields'] = cleaned
    return embed


def _payload(embed: dict[str, Any]) -> dict[str, Any]:
    return {
        'username': 'KoroFX Alerts',
        # Never @everyone / role pings from an automated alert.
        'allowed_mentions': {'parse': []},
        'embeds': [embed],
    }


# ---------------------------------------------------------------------------
# Dedupe / throttle state (process-local, thread-safe)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_dedupe: dict[str, dict[str, Any]] = {}
_global_times: deque[float] = deque()
_throttled_since_message = 0
_last_throttle_message: Optional[float] = None


def reset_state() -> None:
    """Clear in-memory rate limiting. Used by tests and after a fork."""
    global _throttled_since_message, _last_throttle_message
    with _lock:
        _dedupe.clear()
        _global_times.clear()
        _throttled_since_message = 0
        _last_throttle_message = None


def _reserve(fingerprint: Optional[str], now: float) -> tuple[str, int, int]:
    """Decide what to do with one alert.

    Returns ``(action, suppressed_count, dropped_count)`` where action is
    ``send``, ``suppress`` or ``throttle``.
    """
    global _throttled_since_message, _last_throttle_message

    with _lock:
        while _global_times and now - _global_times[0] >= GLOBAL_WINDOW_SECONDS:
            _global_times.popleft()

        suppressed = 0
        if fingerprint:
            state = _dedupe.get(fingerprint)
            if state and now - state['last_sent'] < DEDUPE_WINDOW_SECONDS:
                state['suppressed'] += 1
                return 'suppress', 0, 0
            if state:
                suppressed = int(state.get('suppressed') or 0)

        if len(_global_times) >= GLOBAL_MAX_ALERTS:
            _throttled_since_message += 1
            if (
                _last_throttle_message is not None
                and now - _last_throttle_message < GLOBAL_WINDOW_SECONDS
            ):
                return 'suppress', 0, 0
            _last_throttle_message = now
            dropped = _throttled_since_message
            _throttled_since_message = 0
            return 'throttle', 0, dropped

        _global_times.append(now)
        if fingerprint:
            _dedupe[fingerprint] = {'last_sent': now, 'suppressed': 0}

        # Keep the registry from growing without bound in a long-lived process.
        if len(_dedupe) > 2000:
            for key, state in list(_dedupe.items()):
                if now - state['last_sent'] >= DEDUPE_WINDOW_SECONDS:
                    _dedupe.pop(key, None)

        return 'send', suppressed, 0


def _throttle_embed(dropped: int) -> dict[str, Any]:
    labels = time_labels()
    description = (
        f'Too many errors, alerts throttled. {dropped} alert'
        f'{"s" if dropped != 1 else ""} dropped in the last minute.'
    )
    return build_embed(
        'Too many errors, alerts throttled',
        fields=[
            ('Environment', config.app_env()),
            ('Commit', config.commit_sha()),
            ('Time (UTC)', labels['utc']),
            ('Time (Lagos)', labels['lagos']),
        ],
        level='warning',
        description=description,
    )


# ---------------------------------------------------------------------------
# Persistent marker (survives a process restart in the same container)
# ---------------------------------------------------------------------------

def claim_persistent_slot(key: str, ttl_seconds: int) -> bool:
    """Reserve ``key`` for ``ttl_seconds`` across process restarts.

    Used for the "Service started" notice: an in-memory dedupe cannot stop a
    restart loop, because every new process starts with empty state. A marker
    file in the temp directory survives a process restart on the same
    container. Best-effort only -- any filesystem problem returns True so the
    alert is still delivered.
    """
    try:
        state_dir = (os.getenv('DISCORD_STATE_DIR') or '').strip() or tempfile.gettempdir()
        digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]
        path = os.path.join(state_dir, f'okorofx-alert-{digest}.marker')
        now = time.time()
        try:
            age = now - os.path.getmtime(path)
            if age < ttl_seconds:
                return False
        except OSError:
            pass
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(str(now))
        return True
    except OSError:
        return True


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

_queue: 'queue.Queue[tuple[dict[str, Any], str]]' = queue.Queue(maxsize=QUEUE_MAXSIZE)
_worker_lock = threading.Lock()
_worker_started = False


def _webhook_for(level: str) -> str:
    if level in {'info', 'warning'}:
        return config.activity_webhook_url() or config.webhook_url()
    return config.webhook_url()


def _post(payload: dict[str, Any], url: str) -> bool:
    """POST one payload. Never raises and never logs the URL."""
    if not url:
        return False
    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        return 200 <= response.status_code < 300
    except Exception:  # noqa: BLE001 - transport errors are never fatal
        return False


def _worker_loop() -> None:
    while True:
        payload, url = _queue.get()
        try:
            _post(payload, url)
        except Exception:  # noqa: BLE001 - a worker must never die
            pass
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(
            target=_worker_loop,
            name='koro-discord-alerts',
            daemon=True,
        )
        thread.start()
        _worker_started = True


def _enqueue(payload: dict[str, Any], url: str) -> bool:
    try:
        _ensure_worker()
        _queue.put_nowait((payload, url))
        return True
    except Exception:  # noqa: BLE001 - a full queue is a dropped alert, not an error
        return False


def send_discord_alert(
    title: str,
    fields: Any = None,
    level: str = 'error',
    description: str = '',
    fingerprint: Optional[str] = None,
    async_send: bool = True,
) -> bool:
    """Queue (or synchronously send) one alert.

    Returns True when the alert was accepted for delivery. A False return means
    alerting is disabled, the alert was deduplicated/throttled, or the
    synchronous post failed -- never that an exception escaped.
    """
    try:
        if not config.alerts_enabled():
            return False

        level = (level or 'error').lower()
        if level not in VALID_LEVELS:
            level = 'error'

        action, suppressed, dropped = _reserve(fingerprint, _now())
        if action == 'suppress':
            return False

        if action == 'throttle':
            level = 'warning'
            embed = _throttle_embed(dropped)
        else:
            if suppressed:
                note = (
                    f'This error occurred {suppressed} more '
                    f'time{"s" if suppressed != 1 else ""} since the last alert.'
                )
                description = f'{description}\n\n{note}'.strip()
            embed = build_embed(
                title=title,
                fields=fields,
                level=level,
                description=description,
            )

        payload = scrub_payload(_payload(embed))
        url = _webhook_for(level)
        if async_send:
            return _enqueue(payload, url)
        return _post(payload, url)
    except Exception:  # noqa: BLE001 - alerting must never break a caller
        return False


def send_test_alert(level: str = 'info') -> bool:
    """Send a clearly-marked test message synchronously (command/endpoint)."""
    labels = time_labels()
    return send_discord_alert(
        title='Test alert from KoroFX',
        fields=[
            ('Environment', config.app_env()),
            ('Commit', config.commit_sha()),
            ('Time (UTC)', labels['utc']),
            ('Time (Lagos)', labels['lagos']),
            ('Sent by', 'manual test'),
        ],
        level=level,
        description='If you can read this in Discord, alerting is wired up correctly.',
        fingerprint=None,
        async_send=False,
    )


def fingerprint_for(
    exception_name: str,
    filename: str,
    lineno: int,
    path: str = '',
) -> str:
    """Stable fingerprint: exception type + file + line + request path."""
    raw = f'{exception_name}|{filename}|{lineno}|{path}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]
