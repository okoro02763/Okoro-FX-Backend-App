"""Liveness/readiness endpoint used by the deploy platform and uptime checks.

``GET /api/health/`` answers 200 only when the process can reach its database
*and* the database schema matches the deployed code (no unapplied migrations).
A bad deploy -- the classic "model added, migrations never ran" failure that
took down ``/api/trading/trades/`` with ``relation ... does not exist`` -- is
then caught by the platform's health check in seconds instead of surfacing as
500s on real user traffic.

The endpoint is intentionally public (no authentication) so a load balancer or
Render health check can probe it; it never returns configuration or secrets,
only booleans and the names of any unapplied migrations.
"""

import logging

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

logger = logging.getLogger(__name__)


def _unapplied_migrations():
    """Return the ``['app_label.migration_name', ...]`` still to be applied.

    Uses the same executor ``migrate`` does, so the answer matches reality
    (including migrations recorded in ``django_migrations``).

    ``migration_plan()`` yields ``(migration, backwards)`` pairs -- a
    ``Migration`` instance and a bool -- not ``(app_label, name)``. Unpacking
    it as the latter produced the nonsense entry
    ``"kyc.0001_initial.False"`` in a real incident; the app label and name
    come from the ``Migration`` object itself.
    """
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    return [
        f'{migration.app_label}.{migration.name}'
        for migration, _backwards in executor.migration_plan(targets)
    ]


@api_view(['GET'])
@permission_classes([AllowAny])
def health(request):
    """Report database reachability and schema/migration state."""
    payload = {
        'status': 'ok',
        'database': 'ok',
        'migrations': 'ok',
        'unapplied_migrations': [],
    }
    http_status = 200

    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - health must never raise
        logger.exception('Health check: database is unreachable')
        payload.update(status='error', database='unreachable')
        return Response(payload, status=503)

    try:
        pending = _unapplied_migrations()
    except Exception:  # noqa: BLE001 - report, never raise
        logger.exception('Health check: could not inspect migration state')
        payload.update(status='error', migrations='unknown')
        return Response(payload, status=503)

    if pending:
        payload.update(
            status='error',
            migrations='pending',
            unapplied_migrations=pending,
        )
        http_status = 503

    return Response(payload, status=http_status)
