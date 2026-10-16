import asyncio
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

logger = logging.getLogger(__name__)

# How often each connected client gets a price tick. Kept short enough to feel
# live; the services layer caches provider calls so upstream traffic stays
# well below the Twelve Data free-tier budget.
TICK_SECONDS = float(getattr(settings, 'MARKET_TICK_SECONDS', 2))
# Every Nth tick also sweeps expired trades so settlement happens in near
# real time even with no Celery worker running.
SETTLE_EVERY_N_TICKS = max(1, int(getattr(settings, 'MARKET_SETTLE_EVERY_N_TICKS', 2)))


class PriceConsumer(AsyncJsonWebsocketConsumer):
    """Streams price ticks for one instrument to a single client."""

    async def connect(self):
        self.symbol = self.scope['url_route']['kwargs'].get('symbol', '')
        self._stop = False
        self._task = None
        self._tick_count = 0

        instrument = await database_sync_to_async(self._load)(self.symbol)
        if instrument is None:
            await self.accept()
            await self.send_json(
                {'type': 'error', 'symbol': self.symbol, 'detail': 'unknown symbol'}
            )
            await self.close(code=4404)
            return

        await self.accept()
        snapshot = await database_sync_to_async(self._quote)()
        if snapshot:
            await self.send_json({'type': 'snapshot', **snapshot})
        self._task = asyncio.create_task(self._stream())

    async def _stream(self):
        while not self._stop:
            try:
                quote = await database_sync_to_async(self._quote)()
                if quote:
                    await self.send_json({'type': 'tick', **quote})
                self._tick_count += 1
                if self._tick_count % SETTLE_EVERY_N_TICKS == 0:
                    # Settlement is idempotent and cheap (indexed query), so
                    # running it from every Nth tick is safe and keeps results
                    # timely without a Celery worker.
                    await database_sync_to_async(self._settle)()
            except Exception as exc:  # keep the socket alive across blips
                logger.warning('price tick failed for %s: %s', self.symbol, exc)
            await asyncio.sleep(TICK_SECONDS)

    async def disconnect(self, code):
        self._stop = True
        task = getattr(self, '_task', None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        # The stream is push-only; ignore anything the client sends.
        return

    # --- helpers (run in a worker thread via database_sync_to_async) ---

    def _load(self, symbol):
        from .services import get_instrument

        return get_instrument(symbol)

    def _quote(self):
        from . import services

        return services.get_quote(self.symbol)

    def _settle(self):
        from trading.services import settle_expired_trades

        settle_expired_trades()
