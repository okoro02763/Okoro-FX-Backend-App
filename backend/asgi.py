"""
ASGI config for backend project.

Exposes the ASGI callable as ``application``. HTTP is served by Django and
WebSocket connections are routed through the Channels protocol router so the
market app can stream live prices.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')

# The Django ASGI application must be built before importing anything that
# touches the ORM (consumers import models), otherwise the app registry is not
# ready yet.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.sessions import SessionMiddlewareStack  # noqa: E402

from market.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        'http': django_asgi_app,
        'websocket': SessionMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)
