from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone
from datetime import timedelta


class Wallet(models.Model):
    """Demo balance for the binary-options terminal.

    Paper trading only: no real money is ever held here.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='binary_wallet',
    )
    demo_balance = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal('10000.00')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user} -> {self.demo_balance}'


class Trade(models.Model):
    """A single binary option: direction, stake, fixed payout, expiry."""

    DIRECTION_CHOICES = [
        ('buy', 'Buy'),
        ('sell', 'Sell'),
    ]
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('won', 'Won'),
        ('lost', 'Lost'),
        # A refund happens only when the close price exactly equals the open
        # price; the stake is returned with no profit.
        ('refund', 'Refund'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='binary_trades',
    )
    instrument = models.ForeignKey(
        'market.Instrument',
        on_delete=models.PROTECT,
        related_name='binary_trades',
    )
    direction = models.CharField(max_length=4, choices=DIRECTION_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='open')

    stake = models.DecimalField(max_digits=15, decimal_places=2)
    # Snapshot of the instrument's payout at open time.
    payout_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('80.00'))
    # Gross payout credited on a win: stake + stake * payout_percent / 100.
    gross_payout = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    profit = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    open_price = models.DecimalField(max_digits=18, decimal_places=8)
    close_price = models.DecimalField(max_digits=18, decimal_places=8, null=True, blank=True)

    expiry_time = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['status', 'expiry_time']),
        ]

    def __str__(self):
        return f'{self.get_direction_display()} {self.instrument.symbol} {self.status} ({self.stake})'

    @property
    def is_settled(self) -> bool:
        return self.status != 'open'

    @property
    def is_expired(self) -> bool:
        return self.expiry_time <= timezone.now()


def default_expiry(seconds: int = 60):
    return timezone.now() + timedelta(seconds=seconds)
