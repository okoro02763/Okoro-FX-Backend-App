"""Logging handler that forwards ERROR records to Discord."""

from __future__ import annotations

import logging
import os
import traceback
from typing import Any

from . import config
from .context import get_current_request
from .discord import fingerprint_for, send_discord_alert, time_labels

TRACEBACK_LINES = 15


def _safe(value: Any) -> str:
    try:
        return '' if value is None else str(value)
    except Exception:  # noqa: BLE001 - __str__ must never break alerting
        return '<unprintable>'


def _user_id(request) -> str:
    if request is None:
        return ''
    try:
        user = getattr(request, 'user', None)
        if user is None:
            return ''
        # Only the numeric id is ever sent -- never an email or a name.
        if getattr(user, 'is_authenticated', False):
            pk = getattr(user, 'pk', None)
            return str(pk) if pk is not None else ''
    except Exception:  # noqa: BLE001
        return ''
    return ''


def _status(record: logging.LogRecord, request) -> str:
    status = getattr(record, 'status_code', None)
    if status is None:
        status = getattr(request, '_monitoring_status', None) if request else None
    return str(status) if status is not None else 'unknown'


def _view_name(record: logging.LogRecord, request) -> str:
    view = getattr(record, 'view_name', '') or ''
    if view:
        return _safe(view)
    if request is not None:
        match = getattr(request, 'resolver_match', None)
        if match is not None:
            return _safe(getattr(match, 'view_name', '') or '')
    return ''


def _exception_parts(record: logging.LogRecord):
    exc_type = exc_value = exc_tb = None
    if record.exc_info:
        try:
            exc_type, exc_value, exc_tb = record.exc_info
        except (TypeError, ValueError):
            exc_type = exc_value = exc_tb = None
    return exc_type, exc_value, exc_tb


def _exception_name(exc_type) -> str:
    if exc_type is None:
        return ''
    module = getattr(exc_type, '__module__', '') or ''
    qualname = getattr(exc_type, '__qualname__', None) or getattr(exc_type, '__name__', 'Exception')
    return f'{module}.{qualname}' if module and module != 'builtins' else qualname


def _last_frame(exc_tb):
    if exc_tb is None:
        return None
    frames = traceback.extract_tb(exc_tb)
    return frames[-1] if frames else None


def _traceback_text(exc_tb) -> str:
    if exc_tb is None:
        return ''
    frames = traceback.extract_tb(exc_tb)[-TRACEBACK_LINES:]
    lines = []
    for frame in frames:
        location = f'{os.path.basename(frame.filename)}:{frame.lineno}'
        source = (frame.line or '').strip()
        lines.append(f'{location} in {frame.name}: {source}' if source else f'{location} in {frame.name}')
    return '\n'.join(lines)


class DiscordLogHandler(logging.Handler):
    """Forward every ERROR+ record as a Discord alert.

    Attach with ``level=ERROR``. Unhandled 500s arrive through the
    ``django.request`` logger; any ``logger.exception(...)`` inside a view
    arrives through the normal logger hierarchy with the thread-local request
    attached by :class:`monitoring.middleware.CurrentRequestMiddleware`.
    """

    def __init__(self, level: int = logging.ERROR):
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        # This method is on the logging hot path and must never raise: a broken
        # alert handler must not turn a handled error into a crashed request.
        try:
            if record.levelno < logging.ERROR:
                return
            if not config.alerts_enabled():
                return

            request = getattr(record, 'request', None) or get_current_request()
            exc_type, exc_value, exc_tb = _exception_parts(record)
            frame = _last_frame(exc_tb)
            filename = os.path.basename(frame.filename) if frame else os.path.basename(record.pathname or '')
            lineno = frame.lineno if frame else (record.lineno or 0)
            path = _safe(getattr(request, 'path', '')) if request is not None else ''
            exc_name = _exception_name(exc_type)

            if exc_type is not None:
                title = f'{exc_name.rsplit(".", 1)[-1] or "Error"}'
                if path:
                    title = f'{title} at {path}'
            else:
                title = f'Backend error in {record.name}'

            labels = time_labels()
            fields = [
                ('Environment', config.app_env()),
                ('Time (UTC)', labels['utc']),
                ('Time (Lagos)', labels['lagos']),
                ('Method', _safe(getattr(request, 'method', '')) or 'n/a'),
                ('Path', path or 'n/a'),
                ('Status', _status(record, request)),
                ('View', _view_name(record, request) or 'n/a'),
                ('User ID', _user_id(request) or 'n/a'),
                ('Logger', record.name),
                ('Exception', exc_name or 'n/a'),
                ('Message', _safe(exc_value) if exc_value is not None else record.getMessage()),
                ('Commit', config.commit_sha()),
            ]
            traceback_text = _traceback_text(exc_tb)
            if traceback_text:
                fields.append((f'Traceback (last {TRACEBACK_LINES} lines)', traceback_text))

            fingerprint = fingerprint_for(
                exc_name or record.name,
                filename,
                lineno,
                path,
            )
            send_discord_alert(
                title=title,
                fields=fields,
                level='error',
                fingerprint=fingerprint,
            )
        except Exception:  # noqa: BLE001
            # Deliberately silent, and deliberately not self.handleError():
            # that writes to stderr and can recurse through the logging module.
            pass
