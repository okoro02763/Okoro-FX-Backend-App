from decimal import Decimal

from django.db import models


class Instrument(models.Model):
    """A tradable instrument for the binary-options demo.

    Symbols follow the upstream data provider's convention (Twelve Data):
    forex uses ``EUR/USD``, crypto ``BTC/USD``, commodities ``XAU/USD`` and
    stocks plain tickers such as ``AAPL``.
    """

    CATEGORY_CHOICES = [
        ('currencies', 'Currencies'),
        ('crypto', 'Crypto'),
        ('commodities', 'Commodities'),
        ('stocks', 'Stocks'),
    ]

    symbol = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    payout_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('80.00')
    )
    # Payout offered for a short expiry (1+ minute) and a longer one (5+ min).
    # The terminal resolves the applicable value from the chosen expiry; a
    # Celery beat task drifts both within a configurable band every few minutes.
    payout_percent_short = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('80.00')
    )
    payout_percent_long = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('85.00')
    )
    # Anchor price used by the simulated feed and as a fallback when the
    # upstream provider is unavailable.
    base_price = models.DecimalField(
        max_digits=18, decimal_places=8, default=Decimal('1.00000000')
    )
    # Per-tick volatility for the simulated feed (fraction of price).
    volatility = models.DecimalField(
        max_digits=8, decimal_places=6, default=Decimal('0.000500')
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category', 'sort_order', 'symbol']

    def __str__(self):
        return f"{self.symbol} ({self.get_category_display()})"

    # Expiries of 5 minutes or more use the long-term payout column.
    LONG_EXPIRY_THRESHOLD_SECONDS = 300

    def payout_for_duration(self, seconds):
        """Payout percent that applies to a trade of ``seconds`` duration."""
        if seconds is not None and seconds >= self.LONG_EXPIRY_THRESHOLD_SECONDS:
            return self.payout_percent_long
        return self.payout_percent_short
