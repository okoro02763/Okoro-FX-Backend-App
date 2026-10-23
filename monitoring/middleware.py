"""Middleware that exposes the active request to the alerting log handler."""

from __future__ import annotations

from .context import clear_current_request, set_current_request


class CurrentRequestMiddleware:
    """Record the request for the lifetime of the response.

    Sync only: the project's views are synchronous, and Django runs this around
    them. The request object is what lets a ``logger.exception()`` anywhere in
    a view produce an alert with method, path, view name and user id.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        set_current_request(request)
        try:
            response = self.get_response(request)
            # Available to the handler if it runs after the response exists.
            try:
                request._monitoring_status = getattr(response, 'status_code', None)
            except Exception:  # noqa: BLE001 - never interfere with the response
                pass
            return response
        finally:
            clear_current_request()
