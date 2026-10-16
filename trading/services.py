"""Wallet + trade lifecycle for the binary-options demo (paper trading only).

Open a trade -> deduct the stake, lock it in the row, then settle at expiry by
comparing the close price with the open price. Settlement is idempotent: it is
only ever applied to a trade whose status is still ``open`` and runs inside a
row lock so a double-settlement cannot pay twice.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from datetime import timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from django.db import transaction
from django.utils import timezone

from market.services import get_instrument, get_quote

from .models import Trade, Wallet

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal('0.01')
MIN_STAKE = Decimal('1')
MAX_STAKE = Decimal('100000')
MIN_DURATION_SECONDS = 5
MAX_DURATION_SECONDS = 86400


class TradeError(Exception):
    """Raised for any user-facing validation failure (maps to HTTP 400)."""


def money(value) -> Decimal:
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def get_wallet(user) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


def reset_wallet(user) -> Wallet:
    from django.conf import settings

    starting = Decimal(str(getattr(settings, 'DEMO_STARTING_BALANCE', '10000')))
    with transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
        wallet.demo_balance = money(starting)
        wallet.save(update_fields=['demo_balance', 'updated_at'])
    return wallet


def open_trade(user, symbol, direction, stake, duration_seconds=None, expiry_time=None) -> Trade:
    """Lock the stake and open a new binary option."""
    instrument = get_instrument(symbol)
    if instrument is None:
        raise TradeError(f'Unknown or inactive symbol: {symbol}')

    direction = (direction or '').strip().lower()
    if direction not in ('buy', 'sell'):
        raise TradeError('direction must be "buy" or "sell"')

    try:
        stake = money(stake)
    except Exception as exc:
        raise TradeError('stake must be a number') from exc

    if stake < MIN_STAKE:
        raise TradeError(f'Minimum stake is {MIN_STAKE}')
    if stake > MAX_STAKE:
        raise TradeError(f'Maximum stake is {MAX_STAKE}')

    if duration_seconds is None and expiry_time is None:
        raise TradeError('duration_seconds or expiry_time is required')

    now = timezone.now()
    if expiry_time is not None:
        try:
            if timezone.is_naive(expiry_time):
                expiry_time = timezone.make_aware(expiry_time, timezone=dt_timezone.utc)
            expiry_time = expiry_time.astimezone(dt_timezone.utc)
        except Exception as exc:
            raise TradeError('expiry_time must be an ISO-8601 datetime') from exc
        if expiry_time <= now:
            raise TradeError('expiry_time must be in the future')
    else:
        try:
            duration_seconds = int(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise TradeError('duration_seconds must be an integer') from exc
        if not (MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS):
            raise TradeError(
                f'duration_seconds must be between {MIN_DURATION_SECONDS} '
                f'and {MAX_DURATION_SECONDS}'
            )
        expiry_time = now + timedelta(seconds=duration_seconds)

    if (expiry_time - now).total_seconds() < MIN_DURATION_SECONDS:
        raise TradeError(f'Trade must run for at least {MIN_DURATION_SECONDS} seconds')

    quote = get_quote(symbol)
    if not quote:
        raise TradeError(f'No market price available for {symbol}')

    open_price = Decimal(str(quote['price']))
    if open_price <= 0:
        raise TradeError('Market price is not usable right now, try again')

    payout_percent = instrument.payout_percent
    # Potential gross payout on a win: the stake comes back plus the profit.
    gross_payout = money(stake * (Decimal('1') + payout_percent / Decimal('100')))

    with transaction.atomic():
        wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
        if wallet.demo_balance < stake:
            raise TradeError('Insufficient demo balance')
        wallet.demo_balance = money(wallet.demo_balance - stake)
        wallet.save(update_fields=['demo_balance', 'updated_at'])

        trade = Trade.objects.create(
            user=user,
            instrument=instrument,
            direction=direction,
            stake=stake,
            payout_percent=payout_percent,
            gross_payout=gross_payout,
            open_price=open_price,
            expiry_time=expiry_time,
        )
    return trade


def settle_trade(trade_pk, close_price=None) -> Optional[Trade]:
    """Apply the result to one trade. Returns ``None`` if it must be retried."""
    with transaction.atomic():
        trade = Trade.objects.select_for_update().select_related('instrument').filter(pk=trade_pk).first()
        if trade is None or trade.status != 'open':
            return trade

        if close_price is None:
            quote = get_quote(trade.instrument.symbol)
            if not quote:
                logger.warning('cannot settle trade %s: no price', trade_pk)
                return None
            close_price = quote['price']

        close_price = Decimal(str(close_price))
        trade.close_price = close_price

        if close_price == trade.open_price:
            result, credit = 'refund', trade.stake
            profit = Decimal('0.00')
        elif trade.direction == 'buy':
            won = close_price > trade.open_price
            result = 'won' if won else 'lost'
            credit = trade.gross_payout if won else Decimal('0.00')
            profit = (trade.gross_payout - trade.stake) if won else -trade.stake
        else:
            won = close_price < trade.open_price
            result = 'won' if won else 'lost'
            credit = trade.gross_payout if won else Decimal('0.00')
            profit = (trade.gross_payout - trade.stake) if won else -trade.stake

        trade.status = result
        trade.profit = money(profit)
        trade.settled_at = timezone.now()
        trade.save(update_fields=['status', 'profit', 'close_price', 'settled_at'])

        if credit > 0:
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=trade.user)
            wallet.demo_balance = money(wallet.demo_balance + credit)
            wallet.save(update_fields=['demo_balance', 'updated_at'])

    return trade


def settle_expired_trades() -> int:
    """Settle every open trade whose expiry has passed. Returns the count."""
    now = timezone.now()
    pks = list(
        Trade.objects.filter(status='open', expiry_time__lte=now).values_list('pk', flat=True)
    )
    settled = 0
    for pk in pks:
        if settle_trade(pk) is not None:
            settled += 1
    return settled
