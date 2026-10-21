"""Fail the build when a model change has no committed migration.

This is the check that would have caught the outage that took the account
endpoint down with ``relation "kyc_kycprofile" does not exist``: the ``kyc``
app shipped without a ``migrations/`` package, so Django never created the
table and every request to ``/api/account/`` answered 500.

``python manage.py makemigrations --check --dry-run`` answers the same
question, but it also opens a database connection (``check_consistent_history``)
before it looks at anything.  On the deploy platform the build phase may have
no reachable database, and psycopg2 has no connect timeout by default, so the
check can sit there for minutes and stall -- or kill -- the build.

This command performs exactly the same comparison as Django's own command but
with the connection replaced by ``None``:

* ``MigrationLoader(None, ...)`` builds the migration graph from the files on
  disk only and never touches the database,
* ``MigrationAutodetector`` compares that graph against the current models,
* the command exits non-zero when the comparison finds a change that has no
  migration file.

Because it never opens a socket, it cannot hang, and it gives the same
verdict as ``makemigrations --check --dry-run`` for the failure this is meant
to catch.
"""

import sys

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState


class Command(BaseCommand):
    help = (
        'Fail (exit code 1) when a model change has no committed migration. '
        'Runs without touching the database.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'app_label',
            nargs='*',
            help='Limit the check to these apps (default: every app).',
        )

    def handle(self, *app_labels, **options):
        # `MigrationLoader(None)` is exactly what `makemigrations` itself uses
        # to build the graph; passing `None` for the connection is what keeps
        # this command database-free.
        loader = MigrationLoader(None, ignore_no_migrations=True)

        conflicts = loader.detect_conflicts()
        if conflicts:
            names = "; ".join(
                "%s in %s" % (", ".join(apps_for_app), app)
                for app, apps_for_app in conflicts.items()
            )
            raise CommandError(
                "Conflicting migrations detected; multiple leaf nodes in the "
                "migration graph: (%s).\nTo fix them run "
                "'python manage.py makemigrations --merge'" % names
            )

        questioner = NonInteractiveMigrationQuestioner(
            specified_apps=app_labels,
            dry_run=True,
            verbosity=int(options.get('verbosity', 1)),
            log=self.stdout.write,
        )
        autodetector = MigrationAutodetector(
            loader.project_state(),
            ProjectState.from_apps(apps),
            questioner,
        )
        changes = autodetector.changes(
            graph=loader.graph,
            trim_to_apps=app_labels or None,
            convert_apps=app_labels or None,
            migration_name=None,
        )

        if not changes:
            self.stdout.write(self.style.SUCCESS('No changes detected'))
            return

        self.stderr.write('')
        self.stderr.write(
            self.style.ERROR(
                'ERROR: models have changes without a committed migration.'
            )
        )
        for app_label, app_changes in sorted(changes.items()):
            for migration in app_changes:
                self.stderr.write('  %s: %s' % (app_label, migration.name))
                for operation in migration.operations:
                    self.stderr.write('      %s' % operation.describe())
        self.stderr.write('')
        self.stderr.write(
            "       Run 'python manage.py makemigrations <app>' and commit the "
            "result."
        )
        sys.exit(1)
