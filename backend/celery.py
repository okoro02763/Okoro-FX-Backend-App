"""Celery application for the OKORO FX backend.

Imported from ``backend/__init__.py`` so ``shared_task`` decorators bind to
this app. Celery is optional: if the package is not installed the import fails
softly and the project keeps running, settling trades lazily instead.
"""

import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')

app = Celery('backend')
# Reads CELERY_* settings (broker URL, result backend, beat schedule, ...) out
# of Django settings.
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
