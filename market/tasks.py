"""Celery tasks for the market app.

Prices come from the provider/simulated feed on demand, but payouts drift on a
schedule so the terminal's payout percentages move like a real broker's. The
task is safe to run at any cadence and is idempotent in effect (it only ever
rewrites the two payout columns within the configured band).
"""

from __future__ import annotations

import logging
import random
from decimal import Decimal, ROUND_HALF_UP

from celery import shared_task
from django.conf import settings

from .models import Instrument

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal('0.01')


def _band():
    low = Decimal(str(getattr(settings, 'PAYOUT_DRIFT_MIN', '60')))
    high = Decimal(str(getattr(settings, 'PAYOUT_DRIFT_MAX', '95')))
    if low > high:
        low, high = high, low
    return low, high


def _drift(value: Decimal, low: Decimal, high: Decimal, step: Decimal) -> Decimal:
    """Nudge ``value`` by at most ``step`` and clamp it into the band."""
    delta = Decimal(str(random.uniform(-float(step), float(step))))
    drifted = (value + delta).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return min(high, max(low, drifted))


@shared_task(name='market.drift_payouts')
def drift_payouts_task() -> int:
    """Randomly drift every active instrument's short/long payouts."""
    low, high = _band()
    step = Decimal(str(getattr(settings, 'PAYOUT_DRIFT_STEP', '2')))
    updated = 0
    for instrument in Instrument.objects.filter(is_active=True):
        short = _drift(instrument.payout_percent_short, low, high, step)
        long = _drift(instrument.payout_percent_long, low, high, step)
        # Keep the long column at least as generous as the short one.
        if long < short:
            long = short
        if short != instrument.payout_percent_short or long != instrument.payout_percent_long:
            instrument.payout_percent_short = short
            instrument.payout_percent_long = long
            instrument.save(update_fields=['payout_percent_short', 'payout_percent_long'])
            updated += 1
    logger.info('drifted payouts for %s instruments', updated)
    return updated
