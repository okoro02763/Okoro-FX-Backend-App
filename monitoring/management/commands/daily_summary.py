"""Daily activity summary for the last 24 hours.

One message: new users, KYC submissions, deposits (count + total), withdrawals
(count + total) and trades (count). Triggered by the protected
``POST /api/monitoring/run-checks/?task=daily_summary`` endpoint when
``ENABLE_DAILY_SUMMARY=true``.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone

from forex.models import IdentityVerification, Payment, Trade as SpotTrade, Withdrawal
from kyc.models import KYCProfile
from monitoring import activity
from trading.models import Trade as BinaryTrade

User = get_user_model()


class Command(BaseCommand):
    help = 'Send a single 24-hour activity summary to the Discord activity channel.'

    def handle(self, *args, **options):
        since = timezone.now() - timedelta(hours=24)

        new_users = User.objects.filter(date_joined__gte=since).count()
        kyc_submitted = (
            KYCProfile.objects.filter(submitted_at__gte=since).count()
            + IdentityVerification.objects.filter(submitted_at__gte=since).count()
        )

        deposits = Payment.objects.filter(status='success', verified_at__gte=since)
        deposit_count = deposits.count()
        deposit_total = deposits.aggregate(total=Sum('credit_amount'))['total'] or 0

        withdrawals = Withdrawal.objects.filter(created_at__gte=since)
        withdrawal_count = withdrawals.count()
        withdrawal_total = withdrawals.aggregate(total=Sum('amount'))['total'] or 0

        binary_trades = BinaryTrade.objects.filter(created_at__gte=since).count()
        spot_trades = SpotTrade.objects.filter(opened_at__gte=since).count()

        activity.send_activity(
            'Daily summary (last 24h)',
            level='info',
            fields={
                'new_users': new_users,
                'kyc_submitted': kyc_submitted,
                'deposits': f'{deposit_count} / {deposit_total} NGN',
                'withdrawals': f'{withdrawal_count} / {withdrawal_total} NGN',
                'trades': binary_trades + spot_trades,
            },
        )
        self.stdout.write(
            f'Daily summary sent: {new_users} users, {kyc_submitted} KYC, '
            f'{deposit_count} deposits, {withdrawal_count} withdrawals, '
            f'{binary_trades + spot_trades} trades.'
        )
