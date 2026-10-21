from django.apps import AppConfig
import os
import sys
import threading

# Management commands that must never open an outbound API connection: they run
# in the build, in migrations, in CI, and in one-shot admin commands, where a
# background thread would either pin the process open or race the database.
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


class ForexConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'forex'

    def ready(self):
        # Start the live Deriv connection only in a real serving process.
        #
        # The previous check looked at sys.argv[1] alone, which is a Django-ism:
        # under WSGI/ASGI servers argv is e.g. ['gunicorn', 'backend.wsgi'] or
        # ['daphne', 'backend.asgi:application'], so argv[1] is a module path,
        # never 'runserver', and the connection started on *every* production
        # boot -- including 'collectstatic' during the build, where the
        # non-daemon reconnect Timer kept the process alive until it was killed.
        #
        # Opt in explicitly instead of enumerating what to skip.
        if os.getenv('RUN_DERIV_CLIENT', 'False').strip().lower() not in {'true', '1', 'yes', 'on'}:
            return

        # Belt and braces: even with the flag set, never start it for a
        # management command (argv[1] is the command name when Django runs the
        # command directly, and the WSGI/ASGI entry point otherwise).
        command = sys.argv[1] if len(sys.argv) > 1 else ''
        if command in _NON_SERVING_COMMANDS:
            return

        # Import here to avoid loading models at module level
        from .deriv_client import start_deriv_connection

        connection_thread = threading.Thread(target=start_deriv_connection)
        connection_thread.daemon = True
        connection_thread.start()

