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

from .models import PendingOrder, Trade, Wallet

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

    # The payout depends on how long the trade runs (short vs long column).
    duration_for_payout = (expiry_time - now).total_seconds()
    payout_percent = instrument.payout_for_duration(duration_for_payout)
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
        try:
            from monitoring.activity import is_large_trade, send_activity_on_commit

            if is_large_trade(stake):
                send_activity_on_commit(
                    f'Large single trade | user_id: {user.pk} | symbol: {instrument.symbol} | stake: {stake} | direction: {direction}',
                    level='warning',
                    user_id=user.pk,
                    dedupe_key=f'large-trade:{trade.pk}',
                    cooldown_seconds=3600,
                    fields={'symbol': instrument.symbol, 'stake': stake, 'direction': direction},
                )
            if Decimal(wallet.demo_balance) < 0:
                send_activity_on_commit(
                    f'Wallet balance went negative | user_id: {user.pk} | balance: {wallet.demo_balance}',
                    level='warning',
                    user_id=user.pk,
                    dedupe_key=f'negative-balance:{user.pk}',
                    cooldown_seconds=3600,
                    fields={'balance': wallet.demo_balance},
                )
        except Exception:  # noqa: BLE001 - alerting must never fail a trade
            pass
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
            if Decimal(wallet.demo_balance) < 0:
                try:
                    from monitoring.activity import send_activity_on_commit

                    send_activity_on_commit(
                        f'Wallet balance went negative | user_id: {trade.user_id} | balance: {wallet.demo_balance}',
                        level='warning',
                        user_id=trade.user_id,
                        dedupe_key=f'negative-balance:{trade.user_id}',
                        cooldown_seconds=3600,
                        fields={'balance': wallet.demo_balance},
                    )
                except Exception:  # noqa: BLE001
                    pass

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


# --- Pending (scheduled) orders ------------------------------------------------

MIN_PENDING_TRIGGER_SECONDS = 5
MAX_PENDING_TRIGGER_SECONDS = 86400


def create_pending_order(
    user,
    symbol,
    direction,
    stake,
    expiry_seconds=60,
    trigger_time=None,
    trigger_price=None,
) -> PendingOrder:
    """Schedule a trade for a future time or price.

    Exactly one trigger is required. The stake is *not* reserved here; it is
    only checked and locked when the order actually fires.
    """
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

    try:
        expiry_seconds = int(expiry_seconds or 60)
    except (TypeError, ValueError) as exc:
        raise TradeError('expiry_seconds must be an integer') from exc
    if not (MIN_DURATION_SECONDS <= expiry_seconds <= MAX_DURATION_SECONDS):
        raise TradeError(
            f'expiry_seconds must be between {MIN_DURATION_SECONDS} '
            f'and {MAX_DURATION_SECONDS}'
        )

    has_time = trigger_time is not None
    has_price = trigger_price is not None
    if has_time == has_price:
        raise TradeError('Provide exactly one of trigger_time or trigger_price')

    now = timezone.now()
    if has_time:
        try:
            if timezone.is_naive(trigger_time):
                trigger_time = timezone.make_aware(trigger_time, timezone=dt_timezone.utc)
            trigger_time = trigger_time.astimezone(dt_timezone.utc)
        except Exception as exc:
            raise TradeError('trigger_time must be an ISO-8601 datetime') from exc
        gap = (trigger_time - now).total_seconds()
        if not (MIN_PENDING_TRIGGER_SECONDS <= gap <= MAX_PENDING_TRIGGER_SECONDS):
            raise TradeError(
                f'trigger_time must be between {MIN_PENDING_TRIGGER_SECONDS}s and '
                f'{MAX_PENDING_TRIGGER_SECONDS}s in the future'
            )
    else:
        try:
            trigger_price = Decimal(str(trigger_price))
        except Exception as exc:
            raise TradeError('trigger_price must be a number') from exc
        if trigger_price <= 0:
            raise TradeError('trigger_price must be greater than zero')

    return PendingOrder.objects.create(
        user=user,
        instrument=instrument,
        direction=direction,
        stake=stake,
        expiry_seconds=expiry_seconds,
        trigger_time=trigger_time if has_time else None,
        trigger_price=trigger_price if has_price else None,
    )


def cancel_pending_order(user, order_pk) -> PendingOrder:
    with transaction.atomic():
        order = (
            PendingOrder.objects.select_for_update()
            .filter(pk=order_pk, user=user)
            .first()
        )
        if order is None:
            raise TradeError('Pending order not found')
        if order.status != 'pending':
            raise TradeError('Only pending orders can be cancelled')
        order.status = 'cancelled'
        order.save(update_fields=['status'])
    return order


def _trigger_ready(order: PendingOrder, now) -> bool:
    if order.trigger_time is not None:
        return now >= order.trigger_time
    if order.trigger_price is not None:
        quote = get_quote(order.instrument.symbol)
        if not quote:
            return False
        price = Decimal(str(quote['price']))
        if order.direction == 'buy':
            return price <= order.trigger_price
        return price >= order.trigger_price
    return False


def process_pending_orders() -> int:
    """Fire every pending order whose trigger has been reached.

    Returns the number of orders successfully placed. Idempotent: each order is
    locked and only transitioned while still ``pending``, so a double run never
    opens two trades.
    """
    now = timezone.now()
    candidate_pks = list(
        PendingOrder.objects.filter(status='pending').values_list('pk', flat=True)
    )
    placed = 0
    for pk in candidate_pks:
        with transaction.atomic():
            order = (
                PendingOrder.objects.select_for_update()
                .select_related('instrument')
                .filter(pk=pk, status='pending')
                .first()
            )
            if order is None:
                continue
            if not _trigger_ready(order, now):
                continue
            try:
                trade = open_trade(
                    order.user,
                    order.instrument.symbol,
                    order.direction,
                    order.stake,
                    duration_seconds=order.expiry_seconds,
                )
            except TradeError as exc:
                order.status = 'failed'
                order.note = str(exc)[:200]
                order.save(update_fields=['status', 'note'])
                continue
            order.status = 'placed'
            order.trade = trade
            order.placed_at = timezone.now()
            order.save(update_fields=['status', 'trade', 'placed_at'])
            placed += 1
    return placed
