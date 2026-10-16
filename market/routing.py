from django.urls import re_path

from .consumers import PriceConsumer

# Symbols contain a slash (``EUR/USD``), so the capture must allow ``/`` --
# a plain ``[^/]+`` would never match and every forex socket would 404.
# The trailing slash is optional because the client may omit it.
websocket_urlpatterns = [
    re_path(r'^ws/prices/(?P<symbol>.+?)/?$', PriceConsumer.as_asgi()),
]
