"""The startup migration must run for servers and stay out of the way otherwise.

``backend/wsgi.py`` and ``backend/asgi.py`` call
``backend.startup.migrate_on_startup``. That is the mechanism that finally
applied the ``kyc`` migration in production, where the deploy platform ran
``gunicorn backend.wsgi`` and ignored every migrate step configured elsewhere.

These tests pin the two properties that matter: it must attempt a migration
when the schema is behind, and it must never run for management commands or
when explicitly disabled.
"""

from unittest import mock

from django.test import SimpleTestCase

from backend import startup


class StartupMigrationTests(SimpleTestCase):
    def test_disabled_by_env_flag(self):
        with mock.patch.dict('os.environ', {'AUTO_MIGRATE': 'False'}):
            with mock.patch('django.core.management.call_command') as migrate:
                startup.migrate_on_startup()
            migrate.assert_not_called()

    def test_skipped_for_management_commands(self):
        """argv[1] naming a management command means no server is booting."""
        for command in ('test', 'migrate', 'collectstatic', 'shell'):
            with mock.patch.dict('os.environ', {'AUTO_MIGRATE': 'True'}):
                with mock.patch('sys.argv', ['manage.py', command]):
                    with mock.patch('django.core.management.call_command') as migrate:
                        startup.migrate_on_startup()
                    migrate.assert_not_called(), command

    def test_runs_migrate_when_schema_is_behind(self):
        """A pending migration must trigger ``migrate`` from the startup hook."""
        with mock.patch.dict('os.environ', {'AUTO_MIGRATE': 'True'}):
            with mock.patch('sys.argv', ['gunicorn', 'backend.wsgi']):
                with mock.patch(
                    'django.db.migrations.executor.MigrationExecutor'
                ) as executor_cls:
                    plan = [(mock.Mock(app_label='kyc', name='0001_initial'), False)]
                    executor_cls.return_value.migration_plan.return_value = plan
                    with mock.patch(
                        'django.core.management.call_command'
                    ) as migrate:
                        startup.migrate_on_startup()
                    migrate.assert_called_once()
                    self.assertEqual(migrate.call_args[0][0], 'migrate')

    def test_no_migrate_call_when_schema_is_current(self):
        with mock.patch.dict('os.environ', {'AUTO_MIGRATE': 'True'}):
            with mock.patch('sys.argv', ['gunicorn', 'backend.wsgi']):
                with mock.patch(
                    'django.db.migrations.executor.MigrationExecutor'
                ) as executor_cls:
                    executor_cls.return_value.migration_plan.return_value = []
                    with mock.patch(
                        'django.core.management.call_command'
                    ) as migrate:
                        startup.migrate_on_startup()
                    migrate.assert_not_called()

    def test_never_raises_when_inspecting_fails(self):
        """A broken database must not stop the server from booting."""
        with mock.patch.dict('os.environ', {'AUTO_MIGRATE': 'True'}):
            with mock.patch('sys.argv', ['gunicorn', 'backend.wsgi']):
                with mock.patch(
                    'django.db.migrations.executor.MigrationExecutor',
                    side_effect=RuntimeError('db down'),
                ):
                    startup.migrate_on_startup()  # must not raise

    def test_never_raises_when_migrate_fails(self):
        """A failed migrate must not stop the server from booting."""
        with mock.patch.dict('os.environ', {'AUTO_MIGRATE': 'True'}):
            with mock.patch('sys.argv', ['gunicorn', 'backend.wsgi']):
                with mock.patch(
                    'django.db.migrations.executor.MigrationExecutor'
                ) as executor_cls:
                    executor_cls.return_value.migration_plan.return_value = [
                        (mock.Mock(app_label='kyc', name='0001_initial'), False)
                    ]
                    with mock.patch(
                        'django.core.management.call_command',
                        side_effect=RuntimeError('concurrent migration'),
                    ):
                        startup.migrate_on_startup()  # must not raise
