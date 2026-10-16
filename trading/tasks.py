"""Celery tasks for the binary-options demo.

Settlement also runs lazily (trade list reads, the price-stream sweep, and the
``settle_trades`` management command), so the demo works without a worker.
This task exists for the docker-compose/production path where Celery beat
triggers it every few seconds.
"""

from celery import shared_task


@shared_task(name='trading.settle_expired_trades')
def settle_expired_trades_task() -> int:
    """Settle every expired open trade. Returns the number settled."""
    from .services import settle_expired_trades

    return settle_expired_trades()


@shared_task(name='trading.process_pending_orders')
def process_pending_orders_task() -> int:
    """Open any pending order whose time/price trigger has been reached."""
    from .services import process_pending_orders

    return process_pending_orders()
