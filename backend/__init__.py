try:
    from .celery import app as celery_app
except ImportError:
    # Celery is an optional dependency; without it the demo settles trades
    # lazily (trade reads, the price-stream sweep, `manage.py settle_trades`).
    celery_app = None
else:
    __all__ = ('celery_app',)
