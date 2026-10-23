"""Read helpers shared by the market REST views and the price WebSocket.

Every upstream call goes through a short-lived cache so a burst of clients
(streaming every tick plus REST chart loads) only burns one provider request
per symbol per TTL window -- important for the Twelve Data free tier.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.core.cache import cache

from .models import Instrument
from .providers import INTERVAL_SECONDS, SimulatedProvider, get_provider

logger = logging.getLogger(__name__)


def get_instrument(symbol: str) -> Optional[Instrument]:
    return Instrument.objects.filter(symbol=symbol, is_active=True).first()


def _simulated_quote(instrument: Instrument) -> Dict[str, Any]:
    return SimulatedProvider.quote(
        instrument.symbol,
        float(instrument.base_price),
        float(instrument.volatility),
        name=instrument.name,
    )


def _simulated_candles(
    instrument: Instrument, interval: str, outputsize: int, end_time: Optional[int] = None
) -> List[Dict[str, Any]]:
    return SimulatedProvider.candles(
        instrument.symbol,
        interval,
        outputsize,
        float(instrument.base_price),
        float(instrument.volatility),
        end_time=end_time,
    )


def _notify_price_fallback(symbol: str, provider: str, interval: str = '') -> None:
    """Tell a human once per symbol per 30 minutes that we served simulated prices."""
    try:
        from monitoring.activity import send_activity

        detail = f'provider: {provider}'
        if interval:
            detail += f' | interval: {interval}'
        send_activity(
            f'Price feed fallback to simulated prices | symbol: {symbol} | {detail}',
            level='warning',
            dedupe_key=f'price-fallback:{symbol}',
            cooldown_seconds=1800,
            fields={'symbol': symbol, 'provider': provider, **({'interval': interval} if interval else {})},
        )
    except Exception:  # noqa: BLE001 - alerting must never affect market data
        pass


def get_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Latest price for an instrument, or ``None`` if it is not tradable."""
    instrument = get_instrument(symbol)
    if instrument is None:
        return None

    ttl = int(getattr(settings, 'MARKET_QUOTE_TTL', 3))
    cache_key = f'bo:quote:{symbol}'
    cached = cache.get(cache_key)
    if cached:
        return cached

    provider = get_provider(symbol)
    if getattr(provider, 'is_upstream', False):
        try:
            quote = provider.quote(symbol)
        except Exception as exc:  # rate limits, offline, bad symbol ...
            logger.warning(
                '%s quote failed for %s (%s); using simulated feed',
                provider.name,
                symbol,
                exc,
            )
            quote = _simulated_quote(instrument)
            _notify_price_fallback(symbol, str(getattr(provider, 'name', 'upstream')))
    else:
        # Simulated mode is a supported demo configuration, not a degraded
        # upstream feed; only warn when an upstream was expected and failed.
        quote = _simulated_quote(instrument)

    quote.setdefault('name', instrument.name)
    quote['payout_percent'] = float(instrument.payout_percent)
    quote['payout_percent_short'] = float(instrument.payout_percent_short)
    quote['payout_percent_long'] = float(instrument.payout_percent_long)
    quote['category'] = instrument.category
    cache.set(cache_key, quote, ttl)
    return quote


def get_candles(
    symbol: str, interval: str = '1m', outputsize: int = 200, end_time: Optional[int] = None
) -> Optional[List[Dict[str, Any]]]:
    """OHLC history for an instrument, oldest first (lightweight-charts order)."""
    instrument = get_instrument(symbol)
    if instrument is None:
        return None
    if interval not in INTERVAL_SECONDS:
        return None

    outputsize = max(1, min(int(outputsize), 5000))
    # end_time is bucketed so cache keys stay stable while the feed advances.
    bucket = 30 if end_time is None else end_time
    cache_key = f'bo:candles:{symbol}:{interval}:{outputsize}:{bucket}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    provider = get_provider(symbol)
    candles: Optional[List[Dict[str, Any]]] = None
    fallback_provider: Optional[str] = None
    if getattr(provider, 'is_upstream', False):
        fallback_provider = str(getattr(provider, 'name', 'upstream'))
        try:
            candles = provider.candles(symbol, interval, outputsize, end_time=end_time)
        except Exception as exc:
            logger.warning(
                '%s candles failed for %s/%s (%s); using simulated feed',
                provider.name,
                symbol,
                interval,
                exc,
            )

    if not candles:
        candles = _simulated_candles(instrument, interval, outputsize, end_time=end_time)
        if fallback_provider:
            _notify_price_fallback(symbol, fallback_provider, interval)

    cache.set(cache_key, candles, 30 if interval in ('1h', '4h', '1d') else 5)
    return candles
