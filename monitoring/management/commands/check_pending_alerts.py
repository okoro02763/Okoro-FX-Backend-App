"""Hourly operational check: withdrawals and KYC reviews pending > 24h.

Sends exactly one activity message per run, and nothing when there is nothing
to report. Designed for a free external scheduler (cron-job.org) or a Render
cron job because the free web service cannot run an in-process timer.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from forex.models import IdentityVerification, Withdrawal
from kyc.models import KYCProfile
from monitoring import activity


class Command(BaseCommand):
    help = 'Alert once if any withdrawal or KYC review has been pending for over 24 hours.'

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(hours=24)

        withdrawals = Withdrawal.objects.filter(status='pending', created_at__lte=cutoff)
        kyc_profiles = KYCProfile.objects.filter(status='pending', submitted_at__lte=cutoff)
        legacy_kyc = IdentityVerification.objects.filter(status='pending', submitted_at__lte=cutoff)

        w_count = withdrawals.count()
        k_count = kyc_profiles.count() + legacy_kyc.count()
        if not w_count and not k_count:
            self.stdout.write('No withdrawals or KYC reviews pending over 24h.')
            return

        activity.send_activity(
            f'{w_count} withdrawals and {k_count} KYC reviews pending over 24h',
            level='warning',
            fields={
                'withdrawals_pending': w_count,
                'kyc_pending': k_count,
            },
        )
        self.stdout.write(
            f'Sent pending summary: {w_count} withdrawals, {k_count} KYC reviews.'
        )
