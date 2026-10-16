"""Settle every open trade whose expiry has passed.

Normally settlement runs automatically (Celery beat, or the price-stream
consumer's sweep). This command exists for cron-based deployments and for
manual/ops use: ``python manage.py settle_trades``.
"""

from django.core.management.base import BaseCommand

from trading.services import settle_expired_trades


class Command(BaseCommand):
    help = 'Settle expired binary options and credit winning wallets.'

    def handle(self, *args, **options):
        settled = settle_expired_trades()
        self.stdout.write(self.style.SUCCESS(f'Settled {settled} expired trade(s).'))
