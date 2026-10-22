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

# Apply outstanding migrations before serving, for the same reason as
# backend/wsgi.py: the deploy platform may boot either entry point and neither
# the start command nor render.yaml is guaranteed to run migrate.
from backend.startup import enforce_kyc_encryption_key, migrate_on_startup  # noqa: E402

# Fail loudly when the document-number cipher has no key, exactly as the
# WSGI entry point does.
enforce_kyc_encryption_key()
migrate_on_startup()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.sessions import SessionMiddlewareStack  # noqa: E402

from market.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        'http': django_asgi_app,
        'websocket': SessionMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)
