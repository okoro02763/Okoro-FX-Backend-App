"""
WSGI config for backend project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')

application = get_wsgi_application()

# Apply any outstanding database migrations before this worker starts serving.
#
# This is deliberately here, not in a start command: Render runs
# "gunicorn backend.wsgi" (its Python fallback) because the service is managed
# by hand rather than by render.yaml, so a migrate step configured anywhere
# else was never executed. This module *is* what gunicorn imports, so the call
# below runs on every boot no matter how the service is configured.
#
# It must come after get_wsgi_application() (Django has to be set up first)
# and it never raises -- see backend/startup.py for the reasoning.
from backend.startup import enforce_kyc_encryption_key, migrate_on_startup  # noqa: E402

enforce_kyc_encryption_key()
migrate_on_startup()
