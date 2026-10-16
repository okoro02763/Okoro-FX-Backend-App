from django.urls import re_path

from . import views


def route(route, view, name):
    """Match both ``/instruments`` and ``/instruments/`` without a redirect.

    APPEND_SLASH is disabled in settings, so the trailing-slash form would
    otherwise 404.
    """
    return re_path(rf'^{route}/?$', view, name=name)


urlpatterns = [
    route('quotes', views.quotes, name='bo-quotes'),
    route('quote', views.quote, name='bo-quote'),
    route('candles', views.candles, name='bo-candles'),
    route('instruments', views.list_instruments, name='bo-instruments'),
    route('cache/clear', views.refresh_cache, name='bo-cache-clear'),
]
