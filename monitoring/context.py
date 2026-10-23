"""Thread-local reference to the request currently being handled.

``django.request`` supplies the request in the log record's ``extra`` for
unhandled 500s, but a plain ``logger.exception(...)`` inside a view does not.
Keeping the active request in a thread-local lets :class:`DiscordLogHandler`
attach method/path/user to *every* error logged during a request, without
asking every call site to pass it along.
"""

from __future__ import annotations

import threading
from typing import Optional

_state = threading.local()


def set_current_request(request) -> None:
    _state.request = request


def clear_current_request() -> None:
    _state.request = None


def get_current_request() -> Optional[object]:
    return getattr(_state, 'request', None)
