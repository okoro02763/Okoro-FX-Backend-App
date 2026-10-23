"""App configuration: boot-time wiring for Discord alerting."""

from __future__ import annotations

import logging
import os
import sys

from django.apps import AppConfig

logger = logging.getLogger(__name__)

_NON_SERVING_MANAGE_COMMANDS = frozenset(
    {
        'check',
        'checkmigrations',
        'collectstatic',
        'createsuperuser',
        'dbshell',
        'flush',
        'loaddata',
        'makemigrations',
        'migrate',
        'shell',
        'showmigrations',
        'test',
        'test_discord_alert',
    }
)


def is_serving_process() -> bool:
    """True only for a process that actually serves traffic.

    ``ready()`` also runs for every management command, the test suite, and the
    Celery worker. Sending a "Service started" notice from those would be noise
    at best and a spurious alert during a build at worst.
    """
    program = os.path.basename(sys.argv[0] if sys.argv else '').lower()
    command = (sys.argv[1] if len(sys.argv) > 1 else '').lower()

    if program in {'pytest', 'py.test'}:
        return False
    if program == 'celery' or 'celery' in program:
        return False
    if program in {'manage.py', 'django-admin', 'django-admin.py'}:
        return command == 'runserver'
    # gunicorn / daphne / uwsgi / hypercorn / a plain Python WSGI wrapper.
    return bool(program)


def _connect_celery_signals() -> None:
    try:
        from celery.signals import task_failure
    except Exception:  # noqa: BLE001 - Celery is optional
        return

    def _on_task_failure(sender=None, exception=None, **kwargs):  # noqa: ANN001
        try:
            from .alerts import notify_celery_failure

            task_name = getattr(sender, 'name', None) or str(sender or 'unknown task')
            notify_celery_failure(task_name, exception)
        except Exception:  # noqa: BLE001 - never let a signal handler raise
            pass

    try:
        task_failure.connect(
            _on_task_failure,
            dispatch_uid='monitoring.celery.task_failure',
            weak=False,
        )
    except Exception:  # noqa: BLE001
        pass


class MonitoringConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'monitoring'
    verbose_name = 'Monitoring'

    def ready(self) -> None:
        from . import config

        _connect_celery_signals()

        serving = is_serving_process()
        if not config.alerts_enabled():
            if serving and not getattr(self, '_warned_missing_webhook', False):
                self._warned_missing_webhook = True
                # One line, once per process, and never the URL itself.
                logger.warning(
                    'Discord alerts are disabled: DISCORD_WEBHOOK_URL is not '
                    'set to a valid webhook.'
                )
            return

        if serving:
            try:
                from .alerts import notify_service_started

                notify_service_started()
            except Exception:  # noqa: BLE001 - startup must never be blocked
                pass
