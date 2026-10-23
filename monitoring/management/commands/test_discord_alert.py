"""Send one test alert to Discord: ``python manage.py test_discord_alert``.

Deliberately not wired into any schedule. It is the quickest way to prove a
deploy can reach Discord after adding ``DISCORD_WEBHOOK_URL`` to the service.
"""

from django.core.management.base import BaseCommand, CommandError

from monitoring import config
from monitoring.discord import send_test_alert


class Command(BaseCommand):
    help = 'Send a test alert to the configured Discord webhook.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--level',
            choices=['error', 'warning', 'info'],
            default='info',
            help='Embed colour/level for the test message (default: info).',
        )

    def handle(self, *args, **options):
        if not config.alerts_enabled():
            raise CommandError(
                'Discord alerts are disabled. Set DISCORD_WEBHOOK_URL to a '
                'valid webhook URL and try again.'
            )

        level = options['level']
        sent = send_test_alert(level=level)
        if not sent:
            raise CommandError(
                'The test alert could not be delivered. Check that the webhook '
                'still exists in Discord and that the server can reach it.'
            )

        self.stdout.write(
            self.style.SUCCESS(f'Test alert sent to Discord ({level}).')
        )
