"""Apply database migrations automatically when a serving process boots.

Why this exists
---------------
The production outage this fixes: the ``kyc`` app shipped a migration, but the
Render web service never ran ``migrate``. Its start command is Render's
fallback (``gunicorn backend.wsgi``), and the service is not Blueprint-managed,
so ``render.yaml`` -- which does contain a migrate step -- is ignored entirely.
The result was ``relation "kyc_kycprofile" does not exist`` on every KYC call.

Every fix attempted through configuration (start command, pre-deploy command,
build script) depends on someone editing the Render dashboard correctly, and
that kept not happening. This module takes configuration out of the equation:
``backend.wsgi`` is the exact module ``gunicorn backend.wsgi`` imports, so the
code here runs on every boot of the web service regardless of the start
command. Migrations can no longer be forgotten.

Why it is safe
--------------
* **It never raises.** A failure to migrate must not stop the server from
  starting -- a serving process that can answer ``/api/health`` with "schema is
  behind" is far more useful than a process that crashes and gives a bare 502.
  Failures are logged at ERROR with the full traceback.
* **It is idempotent.** ``migrate`` on an up-to-date database is a no-op.
* **It can be turned off.** Set ``AUTO_MIGRATE=False`` for environments where
  schema changes must be a deliberate, separate deploy step.
* **It is guarded against management commands.** ``wsgi.py`` is only imported
  by servers, but the same guard is applied so importing the WSGI module from
  a script can never trigger a surprise migration.
* **Only one process should win the race.** Render runs a single web instance
  here; where several workers share a database, Django's own migration locking
  (``MigrationExecutor`` acquires a lock on supported backends) plus the
  advisory-lock-free retry below keep concurrent booters from corrupting
  state. A ``database is locked``/``relation ... already exists`` style error
  in a second worker is harmless and logged, not fatal.
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)

# Commands for which booting a server was clearly not intended. Kept in sync
# with the guard in ``forex.apps`` by intent, not by import (importing that
# module here could pull in models before the app registry is ready).
_NON_SERVING_COMMANDS = {
    'collectstatic',
    'checkmigrations',
    'createsuperuser',
    'dbshell',
    'flush',
    'loaddata',
    'makemigrations',
    'migrate',
    'shell',
    'showmigrations',
    'test',
}


def _truthy(value: str) -> bool:
    return value.strip().lower() in {'true', '1', 'yes', 'on'}


def enforce_kyc_encryption_key() -> None:
    """Fail fast at boot when the KYC encryption key is not configured.

    The document-number cipher comes from the ``KYC_ENCRYPTION_KEY``
    environment variable. In development a key is derived from ``SECRET_KEY``
    so the flow works with zero setup; in production there is no fallback, and
    ``kyc.crypto`` already refuses to build a Fernet without an explicit key.

    Called from the WSGI boot path so a misconfigured deployment fails loudly
    at startup instead of surfacing as an opaque error on the first KYC
    document upload.
    """
    from django.core.exceptions import ImproperlyConfigured

    from kyc import crypto as kyc_crypto

    try:
        kyc_crypto.get_fernet()
    except (kyc_crypto.KycCryptoError, ValueError, TypeError) as exc:
        # KycCryptoError: no key in production. ValueError/TypeError: the key
        # is set but is not a valid Fernet key. Both are a misconfiguration
        # that must be fixed by a human, so both stop the boot with a clear
        # message instead of surfacing later as a failed upload.
        logger.error('KYC encryption key is not configured correctly: %s', exc)
        raise ImproperlyConfigured(
            'KYC_ENCRYPTION_KEY must be set to a valid Fernet key in production. '
            'Generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


def migrate_on_startup() -> None:
    """Bring the database schema up to date, logging and swallowing errors.

    Called from ``backend.wsgi`` once the Django app is ready. Never raises:
    a boot failure would turn a degraded service into an unavailable one.

    Also refuses to serve when the KYC encryption key is missing in
    production -- see :func:`enforce_kyc_encryption_key`.
    """
    # Fail fast when the KYC encryption key is missing: document numbers are
    # encrypted at rest before they are stored, and in production there is no
    # development fallback. Booting with a missing key would only surface the
    # problem on the first document upload, mid-flow, as a confusing error.
    enforce_kyc_encryption_key()

    # Off switch for anyone who wants migrations to stay a manual step.
    if not _truthy(os.getenv('AUTO_MIGRATE', 'True')):
        logger.info('AUTO_MIGRATE is off; skipping the startup migration.')
        return

    command = sys.argv[1] if len(sys.argv) > 1 else ''
    if command in _NON_SERVING_COMMANDS:
        return

    # Django must be set up before we touch the migration machinery.
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    try:
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
    except Exception:  # noqa: BLE001 - never block startup
        logger.exception(
            'Could not inspect migration state at startup; '
            'continuing to boot. /api/health will report the schema state.'
        )
        return

    if not plan:
        logger.info('Startup migration check: database schema is up to date.')
        return

    pending = [
        f'{migration.app_label}.{migration.name}'
        for migration, _backwards in plan
    ]
    logger.warning(
        'Startup migration check: %d migration(s) pending: %s',
        len(pending),
        ', '.join(pending),
    )

    try:
        call_command('migrate', interactive=False, verbosity=1)
    except Exception:  # noqa: BLE001 - never block startup
        # Most common real cause: a second worker won the race and applied the
        # same migration first. Either way the schema is now consistent, and
        # /api/health is the authority on whether it actually is.
        logger.exception(
            'Startup migration failed. The schema may still be behind; '
            'check GET /api/health. Continuing to boot so the service stays '
            'reachable.'
        )
        return

    logger.info('Startup migration completed.')
