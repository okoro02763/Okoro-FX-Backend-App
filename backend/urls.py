"""Project URL configuration.

Three API surfaces are mounted under ``/api/``:

* ``forex``   -- the legacy OKORO FX spot/bot platform (``/api/auth/...`` etc.)
* ``market``  -- binary-options market data (``/api/market/...``)
* ``trading`` -- binary-options wallet + trades (``/api/trading/...``)

The market and trading apps get their own prefixes because ``forex`` already
owns ``/api/trades/`` for spot trades; sharing the root would shadow it.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from .health import health

urlpatterns = [
    path('admin/', admin.site.urls),
    # Public readiness probe: DB reachable + no unapplied migrations.
    path('api/health', health, name='health'),
    path('api/', include('forex.urls')),
    path('api/market/', include('market.urls')),
    path('api/trading/', include('trading.urls')),
    path('api/kyc/', include('kyc.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
