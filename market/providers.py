"""Market data providers for the binary-options demo.

Two providers are exposed behind the same interface:

* :class:`TwelveDataProvider` -- real quotes and OHLC candles from Twelve Data.
* :class:`SimulatedProvider` -- a deterministic, offline price generator used
  when no ``TWELVE_DATA_API_KEY`` is configured (or when the upstream API
  errors out) so the demo never needs a key to run.

Both return plain dicts so the views/consumers never depend on a concrete
provider.
"""

from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, List, Optional

import requests
from django.conf import settings

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


def _ohlc(time_: int, o: float, h: float, l: float, c: float) -> Dict[str, Any]:
    return {'time': time_, 'open': o, 'high': h, 'low': l, 'close': c}


class SimulatedProvider:
    """Deterministic offline price feed.

    The price is a pure function of ``(symbol, unix timestamp)`` built from a
    handful of sine components with different periods. That makes it smooth and
    continuous (good for charting) while still moving every tick, and it needs
    no server-side state between calls.
    """

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


class TwelveDataProvider:
    """Thin wrapper over the Twelve Data REST API."""

    def __init__(self, api_key: str, base_url: str, timeout: int = 10):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

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
        data = self._get('quote', {'symbol': symbol})
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
        data = self._get(
            'time_series',
            {
                'symbol': symbol,
                'interval': td_interval,
                'outputsize': min(max(outputsize, 1), 5000),
                'timezone': 'UTC',
                'format': 'JSON',
            },
        )
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


def get_provider():
    """Return the active provider, degrading to simulated when unusable."""
    key = getattr(settings, 'TWELVE_DATA_API_KEY', '')
    if key:
        return TwelveDataProvider(key, getattr(settings, 'TWELVE_DATA_BASE_URL', 'https://api.twelvedata.com'))
    return SimulatedProvider()
