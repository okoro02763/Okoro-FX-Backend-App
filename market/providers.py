"""Market data providers for the binary-options demo.

Providers share one small interface (``quote``/``candles``) and return plain
dicts so the views/consumers never depend on a concrete implementation:

* :class:`TwelveDataProvider` -- real quotes and OHLC candles from Twelve Data.
  Serves forex, commodities and stocks (and crypto when CoinGecko is absent).
* :class:`CoinGeckoProvider` -- crypto quotes from CoinGecko's batched
  ``/coins/markets`` feed, plus candles folded from ``/coins/{id}/market_chart``
  price points.
* :class:`SimulatedProvider` -- a deterministic, offline price generator used
  when no API key is configured (or when every upstream errors out) so the demo
  never needs a key to run.

:func:`get_provider` picks the upstream(s) for a symbol and wires the fallbacks.
Each upstream enforces its free-tier request budget per minute and re-serves the
last known good payload when that budget is spent, so a busy terminal degrades
gracefully instead of hammering (and getting blocked by) the API.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Sequence

import requests
from django.conf import settings
from django.core.cache import cache

# Timeframes the UI offers, mapped onto the provider's supported intervals.
# Sub-minute frames have no Twelve Data equivalent, so they are served by the
# simulated feed (which is also what offline demos use).
INTERVAL_SECONDS: Dict[str, int] = {
    '5s': 5,
    '15s': 15,
    '1m': 60,
    '5m': 300,
    '15m': 900,
    '1h': 3600,
    '4h': 14400,
    '1d': 86400,
}
# Our timeframe -> Twelve Data `interval` parameter. Missing keys fall back to
# the simulated feed.
Td_intervals: Dict[str, str] = {
    '1m': '1min',
    '5m': '5min',
    '15m': '15min',
    '1h': '1h',
    '4h': '4h',
    '1d': '1day',
}
# Our crypto symbols -> CoinGecko coin ids. Only these symbols are ever routed
# to CoinGecko; everything else stays on Twelve Data.
COINGECKO_IDS: Dict[str, str] = {
    'BTC/USD': 'bitcoin',
    'ETH/USD': 'ethereum',
    'BNB/USD': 'binancecoin',
    'SOL/USD': 'solana',
    'XRP/USD': 'ripple',
    'ADA/USD': 'cardano',
    'DOGE/USD': 'dogecoin',
    'LTC/USD': 'litecoin',
}


class UpstreamError(RuntimeError):
    """An upstream is unusable right now (budget spent, HTTP/parse error, ...)."""


class _MinuteBudget:
    """Thread-safe sliding-window limiter for one upstream's request quota."""

    def __init__(self, limit: int):
        self._limit = max(1, int(limit))
        self._hits: List[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._hits = [t for t in self._hits if t > now - 60.0]
            if len(self._hits) >= self._limit:
                return False
            self._hits.append(now)
            return True


# One budget per upstream name, shared across instances: get_provider() builds a
# fresh provider object per call, but the free-tier quota belongs to the key.
_BUDGETS: Dict[str, _MinuteBudget] = {}
_BUDGETS_LOCK = threading.Lock()


def _budget(name: str, limit: int) -> _MinuteBudget:
    with _BUDGETS_LOCK:
        bucket = _BUDGETS.get(name)
        if bucket is None:
            bucket = _BUDGETS[name] = _MinuteBudget(limit)
        return bucket


def reset_budgets() -> None:
    """Drop every per-minute budget (tests / the refresh-cache dev endpoint).

    Buckets are recreated on next use, which also lets tests pick a fresh limit.
    """
    with _BUDGETS_LOCK:
        _BUDGETS.clear()


def _ohlc(time_: int, o: float, h: float, l: float, c: float) -> Dict[str, Any]:
    return {'time': time_, 'open': o, 'high': h, 'low': l, 'close': c}


def _bucket_ohlc(
    points: Sequence[Any],
    step: int,
    outputsize: int,
    end_time: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fold ``[timestamp_ms, price]`` points into OHLC buckets of ``step`` secs.

    CoinGecko's ``market_chart`` returns point-in-time prices rather than OHLC,
    so the high/low of each bucket are the extremes of the samples inside it.
    """
    buckets: Dict[int, List[float]] = {}
    for item in points:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            ms = float(item[0])
            price = float(item[1])
        except (TypeError, ValueError):
            continue
        secs = int(ms // 1000)
        if end_time and secs > end_time:
            continue
        start = secs - (secs % step)
        current = buckets.get(start)
        if current is None:
            buckets[start] = [price, price, price, price]
        else:
            current[1] = max(current[1], price)
            current[2] = min(current[2], price)
            current[3] = price
    out = [_ohlc(start, o, h, l, c) for start, (o, h, l, c) in sorted(buckets.items())]
    return out[-outputsize:] if outputsize else out


class _UpstreamProvider:
    """Shared plumbing for keyed upstreams: request budget + stale fallback."""

    name = 'upstream'
    is_upstream = True
    budget_per_minute = 60
    # How long a last-known-good payload stays servable once the per-minute
    # budget is spent or the upstream errors (seconds).
    stale_ttl = 600

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: int = 10,
        budget_per_minute: Optional[int] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._budget = _budget(self.name, budget_per_minute or self.budget_per_minute)

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        raise NotImplementedError

    def _budgeted(self, fetch, stale_key: Optional[str] = None) -> Any:
        """Run ``fetch`` inside the request budget.

        When the budget is spent or ``fetch`` raises, the last payload cached
        under ``stale_key`` is returned if there is one; otherwise the error is
        re-raised so callers can degrade further (next provider / simulated).
        """
        try:
            if not self._budget.acquire():
                raise UpstreamError(f'{self.name}: per-minute request budget exhausted')
            value = fetch()
        except Exception:
            if stale_key:
                stale = cache.get(stale_key)
                if stale is not None:
                    return stale
            raise
        if stale_key:
            cache.set(stale_key, value, self.stale_ttl)
        return value


class SimulatedProvider:
    """Deterministic offline price feed.

    The price is a pure function of ``(symbol, unix timestamp)`` built from a
    handful of sine components with different periods. That makes it smooth and
    continuous (good for charting) while still moving every tick, and it needs
    no server-side state between calls.
    """

    is_upstream = False
    name = 'simulated'

    COMPONENTS = ((911.0, 0.60), (467.0, 0.25), (41.0, 0.06), (7.3, 0.03), (2.1, 0.01))
    SCALE = 40.0  # amplifies `volatility` into a visually interesting range

    @staticmethod
    def _phases(symbol: str) -> List[float]:
        seed = int(hashlib.md5(symbol.encode()).hexdigest()[:8], 16)
        return [
            (seed % 1000) / 1000.0 * math.tau,
            ((seed >> 4) % 977) / 977.0 * math.tau,
        ]

    @classmethod
    def price(
        cls,
        symbol: str,
        t: float,
        base: float,
        volatility: float,
    ) -> float:
        p0, p1 = cls._phases(symbol)
        rel = 0.0
        for i, (period, amp) in enumerate(cls.COMPONENTS):
            phase = p0 if i % 2 == 0 else p1
            rel += amp * math.sin(math.tau * (t / period) + phase)
        value = float(base) * (1.0 + float(cls.SCALE) * float(volatility) * rel)
        # Guard against absurd zero/negative prices for ill-conditioned inputs.
        return max(value, float(base) * 0.05)

    @classmethod
    def quote(
        cls,
        symbol: str,
        base: float,
        volatility: float,
        name: str = '',
        timestamp: Optional[int] = None,
    ) -> Dict[str, Any]:
        now = int(timestamp if timestamp is not None else time.time())
        price = cls.price(symbol, now, base, volatility)
        prev = cls.price(symbol, now - 86400, base, volatility)
        change = price - prev
        pct = (change / prev * 100.0) if prev else 0.0
        day_open = cls.price(symbol, now - (now % 86400), base, volatility)
        day_high = max(cls.price(symbol, now - k, base, volatility) for k in (0, 3600, 7200, 10800))
        day_low = min(cls.price(symbol, now - k, base, volatility) for k in (0, 3600, 7200, 10800))
        return {
            'symbol': symbol,
            'name': name,
            'price': round(price, 8),
            'prev_close': round(prev, 8),
            'open': round(day_open, 8),
            'high': round(day_high, 8),
            'low': round(day_low, 8),
            'change': round(change, 8),
            'percent_change': round(pct, 4),
            'timestamp': now,
            'source': 'simulated',
        }

    @classmethod
    def candles(
        cls,
        symbol: str,
        interval: str,
        outputsize: int,
        base: float,
        volatility: float,
        end_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        step = INTERVAL_SECONDS.get(interval, 60)
        now = int(end_time if end_time is not None else time.time())
        # Align the newest candle onto a bucket boundary so the series looks
        # like real exchange data.
        last_bucket = now - (now % step)
        samples = 8  # intra-candle samples for open/high/low/close
        out: List[Dict[str, Any]] = []
        for i in range(outputsize - 1, -1, -1):
            start = last_bucket - i * step
            values = [
                cls.price(symbol, start + (step * s) / samples, base, volatility)
                for s in range(samples + 1)
            ]
            out.append(_ohlc(start, values[0], max(values), min(values), values[-1]))
        return out


class TwelveDataProvider(_UpstreamProvider):
    """Thin wrapper over the Twelve Data REST API (free tier: 8 credits/min)."""

    name = 'twelvedata'
    budget_per_minute = 8

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        payload = dict(params)
        payload['apikey'] = self.api_key
        resp = requests.get(f'{self.base_url}/{path}', params=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError('Unexpected response from upstream market data API')
        if data.get('status') == 'error' or data.get('code') in (400, 401, 403, 404, 429, 500):
            raise ValueError(data.get('message') or data.get('error') or 'upstream error')
        return data

    def quote(self, symbol: str) -> Dict[str, Any]:
        data = self._budgeted(
            lambda: self._get('quote', {'symbol': symbol}),
            stale_key=f'bo:td:quote:{symbol}',
        )
        return self._parse_quote(symbol, data)

    @staticmethod
    def _parse_quote(symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        price = float(data.get('close') or data.get('price') or 0.0)
        prev = float(data.get('previous_close') or 0.0) or price
        change = price - prev
        pct = float(data.get('percent_change') or 0.0)
        return {
            'symbol': data.get('symbol', symbol),
            'name': data.get('name', ''),
            'price': price,
            'prev_close': prev,
            'open': float(data.get('open') or price),
            'high': float(data.get('high') or price),
            'low': float(data.get('low') or price),
            'change': round(change, 8),
            'percent_change': round(pct, 4),
            'timestamp': int(data.get('timestamp') or time.time()),
            'source': 'twelvedata',
        }

    def candles(
        self, symbol: str, interval: str, outputsize: int, end_time: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        td_interval = Td_intervals.get(interval)
        if td_interval is None:
            raise ValueError(f'{interval} is not available from upstream')
        data = self._budgeted(
            lambda: self._get(
                'time_series',
                {
                    'symbol': symbol,
                    'interval': td_interval,
                    'outputsize': min(max(outputsize, 1), 5000),
                    'timezone': 'UTC',
                    'format': 'JSON',
                },
            ),
        )
        out = self._parse_candles(data, end_time)
        if not out:
            raise ValueError(f'Twelve Data returned no candles for {symbol}/{interval}')
        return out

    @staticmethod
    def _parse_candles(data: Dict[str, Any], end_time: Optional[int] = None) -> List[Dict[str, Any]]:
        rows = data.get('values') or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                iso = row['datetime'].replace('T', ' ')[:19]
                # Twelve Data returns wall-clock times; treat them as UTC so
                # candle offsets are correct no matter the server timezone.
                naive = datetime.strptime(iso, '%Y-%m-%d %H:%M:%S')
                secs = int(naive.replace(tzinfo=dt_timezone.utc).timestamp())
            except (KeyError, ValueError):
                continue
            if end_time and secs > end_time:
                continue
            out.append(
                _ohlc(
                    secs,
                    float(row['open']),
                    float(row['high']),
                    float(row['low']),
                    float(row['close']),
                )
            )
        # The API returns newest-first; chart APIs expect ascending order.
        out.sort(key=lambda c: c['time'])
        return out


class CoinGeckoProvider(_UpstreamProvider):
    """Crypto quotes and candles from CoinGecko (demo tier: ~30 calls/min).

    Quotes come from the batched ``/coins/markets`` endpoint, so one request
    prices the whole crypto universe and the WebSocket tick loop stays cheap.
    Candles are folded from ``/coins/{id}/market_chart`` price points: the demo
    tier samples 1 day at 5-minute, 7-30 days at hourly and 365 days at daily
    resolution, so 5-minute frames only carry the last 24 hours and highs/lows
    are approximations of the samples inside each bar.
    """

    name = 'coingecko'
    budget_per_minute = 30

    MARKETS_CACHE_KEY = 'bo:cg:markets'
    MARKETS_STALE_KEY = 'bo:cg:markets:lastgood'
    # market_chart windows that return hourly samples.
    HOURLY_WINDOWS = (7, 14, 30)

    @property
    def _headers(self) -> Dict[str, str]:
        # Demo keys use api.coingecko.com; Pro keys use pro-api.coingecko.com
        # and are sent under a different header.
        header = 'x-cg-pro-api-key' if 'pro-api' in self.base_url else 'x-cg-demo-api-key'
        return {header: self.api_key}

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        resp = requests.get(
            f'{self.base_url}/{path}', params=params, headers=self._headers, timeout=self.timeout
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            if data.get('error'):
                raise ValueError(str(data['error']))
            status = data.get('status')
            if isinstance(status, dict) and status.get('error_code'):
                raise ValueError(status.get('error_message') or 'upstream error')
        return data

    def quote(self, symbol: str) -> Dict[str, Any]:
        coin_id = COINGECKO_IDS.get(symbol)
        if coin_id is None:
            raise ValueError(f'CoinGecko does not track {symbol}')
        row = self._markets_by_id().get(coin_id)
        if not isinstance(row, dict):
            raise ValueError(f'CoinGecko returned no market data for {symbol}')
        return self._parse_market(symbol, row)

    def _markets_by_id(self) -> Dict[str, Dict[str, Any]]:
        fresh = cache.get(self.MARKETS_CACHE_KEY)
        if fresh:
            return fresh
        rows = self._budgeted(self._fetch_markets, stale_key=self.MARKETS_STALE_KEY)
        by_id = {
            str(row.get('id')): row
            for row in rows
            if isinstance(row, dict) and row.get('id')
        }
        cache.set(
            self.MARKETS_CACHE_KEY,
            by_id,
            int(getattr(settings, 'MARKET_QUOTE_TTL', 3)),
        )
        return by_id

    def _fetch_markets(self) -> List[Dict[str, Any]]:
        rows = self._get(
            'coins/markets',
            {
                'vs_currency': 'usd',
                'ids': ','.join(COINGECKO_IDS.values()),
                'price_change_percentage': '24h',
            },
        )
        if not isinstance(rows, list):
            raise ValueError('Unexpected response from CoinGecko')
        return rows

    def _parse_market(self, symbol: str, row: Dict[str, Any]) -> Dict[str, Any]:
        price = _to_float(row.get('current_price'))
        change = _to_float(row.get('price_change_24h'))
        pct = _to_float(row.get('price_change_percentage_24h'))
        prev = price - change if change else price
        return {
            'symbol': symbol,
            'name': str(row.get('name') or ''),
            'price': price,
            'prev_close': round(prev, 8),
            'open': round(prev, 8),
            'high': _to_float(row.get('high_24h')) or price,
            'low': _to_float(row.get('low_24h')) or price,
            'change': round(change, 8),
            'percent_change': round(pct, 4),
            'timestamp': _parse_iso_timestamp(row.get('last_updated')),
            'source': 'coingecko',
        }

    def candles(
        self, symbol: str, interval: str, outputsize: int, end_time: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        coin_id = COINGECKO_IDS.get(symbol)
        if coin_id is None:
            raise ValueError(f'CoinGecko does not track {symbol}')
        step = INTERVAL_SECONDS.get(interval)
        if not step or step < 300:
            raise ValueError(f'CoinGecko has no {interval} candles')
        outputsize = min(max(int(outputsize), 1), 5000)
        days = self._chart_days(step, outputsize)
        data = self._budgeted(
            lambda: self._get(
                f'coins/{coin_id}/market_chart',
                {'vs_currency': 'usd', 'days': days},
            ),
        )
        points = data.get('prices') if isinstance(data, dict) else None
        if not points:
            raise ValueError(f'CoinGecko returned no prices for {symbol}')
        out = _bucket_ohlc(points, step, outputsize, end_time)
        if not out:
            raise ValueError(f'CoinGecko returned no usable candles for {symbol}')
        return out

    def _chart_days(self, step: int, outputsize: int) -> int:
        """Window that yields samples at (or finer than) the requested step."""
        span = step * max(1, outputsize)
        if step < 3600:
            return 1  # 5-minute samples covering the last day
        if step < 86400:
            for days in self.HOURLY_WINDOWS:
                if days * 86400 >= span:
                    return days
            return self.HOURLY_WINDOWS[-1]
        return 365  # daily samples


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso_timestamp(value: Any) -> int:
    if isinstance(value, str) and value:
        try:
            naive = datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
            return int(naive.replace(tzinfo=dt_timezone.utc).timestamp())
        except ValueError:
            pass
    return int(time.time())


class RoutedProvider:
    """Serves quotes and candles from ordered upstream chains.

    Each call tries the providers in order and returns the first usable result,
    so a rate-limited or offline upstream degrades to the next one instead of
    straight to the simulated feed.
    """

    is_upstream = True

    def __init__(
        self,
        quotes: Sequence[Any],
        candles: Sequence[Any],
        name: str = 'routed',
    ):
        self._quotes = tuple(quotes)
        self._candles = tuple(candles)
        self.name = name

    def quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._first(self._quotes, 'quote', symbol)

    def candles(
        self, symbol: str, interval: str, outputsize: int, end_time: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        return self._first(
            self._candles, 'candles', symbol, interval, outputsize, end_time=end_time
        ) or []

    @staticmethod
    def _first(chain: Sequence[Any], method: str, *args: Any, **kwargs: Any) -> Any:
        last_exc: Optional[Exception] = None
        for provider in chain:
            try:
                result = getattr(provider, method)(*args, **kwargs)
            except Exception as exc:  # try the next upstream
                last_exc = exc
                continue
            if result:
                return result
        if last_exc is not None:
            raise last_exc
        return None


def get_provider(symbol: str = '') -> Any:
    """Return the provider (or routed chain) that serves ``symbol``.

    Crypto pairs are quoted by CoinGecko when its key is set -- one batched
    request covers every crypto symbol, which leaves the tiny Twelve Data
    free-tier budget for forex, commodities and stocks. Charts still prefer
    Twelve Data (finer OHLC) and fall back to CoinGecko. Without a matching key
    the deterministic simulated feed is returned.
    """
    td_key = (getattr(settings, 'TWELVE_DATA_API_KEY', '') or '').strip()
    cg_key = (getattr(settings, 'COINGECKO_API_KEY', '') or '').strip()

    td: Optional[TwelveDataProvider] = None
    if td_key:
        td = TwelveDataProvider(
            td_key,
            getattr(settings, 'TWELVE_DATA_BASE_URL', 'https://api.twelvedata.com'),
            budget_per_minute=int(getattr(settings, 'TWELVE_DATA_REQUESTS_PER_MINUTE', 8)),
        )

    cg: Optional[CoinGeckoProvider] = None
    if cg_key and symbol in COINGECKO_IDS:
        cg = CoinGeckoProvider(
            cg_key,
            getattr(settings, 'COINGECKO_BASE_URL', 'https://api.coingecko.com/api/v3'),
            budget_per_minute=int(getattr(settings, 'COINGECKO_REQUESTS_PER_MINUTE', 30)),
        )

    if cg is not None and td is not None:
        return RoutedProvider(quotes=(cg, td), candles=(td, cg), name='coingecko+twelvedata')
    if cg is not None:
        return cg
    if td is not None:
        return td
    return SimulatedProvider()
