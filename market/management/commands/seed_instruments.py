"""Seed the Instrument table with the demo asset universe.

Symbols follow Twelve Data conventions so the same rows work against the real
provider (``TWELVE_DATA_API_KEY`` set) and the built-in simulated feed.
``update_or_create`` makes the command idempotent: re-running it refreshes
payouts and base prices without duplicating rows.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

from market.models import Instrument

# (symbol, name, category, payout_percent, base_price, volatility)
INSTRUMENTS = [
    # --- Currencies -------------------------------------------------------
    ('EUR/USD', 'Euro / US Dollar', 'currencies', '87.00', '1.08500000', '0.000600'),
    ('GBP/USD', 'British Pound / US Dollar', 'currencies', '86.00', '1.27400000', '0.000700'),
    ('USD/JPY', 'US Dollar / Japanese Yen', 'currencies', '86.00', '149.50000000', '0.000700'),
    ('AUD/USD', 'Australian Dollar / US Dollar', 'currencies', '85.00', '0.66800000', '0.000700'),
    ('USD/CAD', 'US Dollar / Canadian Dollar', 'currencies', '85.00', '1.36200000', '0.000600'),
    ('USD/CHF', 'US Dollar / Swiss Franc', 'currencies', '85.00', '0.88800000', '0.000600'),
    ('NZD/USD', 'New Zealand Dollar / US Dollar', 'currencies', '84.00', '0.60900000', '0.000800'),
    ('EUR/GBP', 'Euro / British Pound', 'currencies', '84.00', '0.85200000', '0.000500'),
    ('EUR/JPY', 'Euro / Japanese Yen', 'currencies', '85.00', '162.30000000', '0.000800'),
    ('GBP/JPY', 'British Pound / Japanese Yen', 'currencies', '83.00', '190.50000000', '0.000900'),
    # --- Crypto -----------------------------------------------------------
    ('BTC/USD', 'Bitcoin / US Dollar', 'crypto', '85.00', '67500.00000000', '0.004000'),
    ('ETH/USD', 'Ethereum / US Dollar', 'crypto', '84.00', '2650.00000000', '0.005000'),
    ('BNB/USD', 'BNB / US Dollar', 'crypto', '82.00', '575.00000000', '0.005000'),
    ('SOL/USD', 'Solana / US Dollar', 'crypto', '82.00', '158.00000000', '0.007000'),
    ('XRP/USD', 'Ripple / US Dollar', 'crypto', '80.00', '0.52000000', '0.007000'),
    ('ADA/USD', 'Cardano / US Dollar', 'crypto', '80.00', '0.36000000', '0.007000'),
    ('DOGE/USD', 'Dogecoin / US Dollar', 'crypto', '80.00', '0.11000000', '0.009000'),
    ('LTC/USD', 'Litecoin / US Dollar', 'crypto', '81.00', '72.00000000', '0.006000'),
    # --- Commodities -----------------------------------------------------
    ('XAU/USD', 'Gold / US Dollar', 'commodities', '85.00', '2650.00000000', '0.001200'),
    ('XAG/USD', 'Silver / US Dollar', 'commodities', '83.00', '31.20000000', '0.002000'),
    ('XPT/USD', 'Platinum / US Dollar', 'commodities', '80.00', '990.00000000', '0.001800'),
    ('XPD/USD', 'Palladium / US Dollar', 'commodities', '78.00', '1040.00000000', '0.002200'),
    ('UKBrent/USD', 'Brent Crude Oil', 'commodities', '82.00', '74.50000000', '0.001800'),
    ('USCrude/USD', 'WTI Crude Oil', 'commodities', '82.00', '71.00000000', '0.001800'),
    ('NatGas/USD', 'Natural Gas', 'commodities', '78.00', '2.95000000', '0.003000'),
    ('XCU/USD', 'Copper / US Dollar', 'commodities', '78.00', '4.35000000', '0.001600'),
    # --- Stocks ----------------------------------------------------------
    ('AAPL', 'Apple Inc.', 'stocks', '80.00', '232.00000000', '0.002000'),
    ('MSFT', 'Microsoft Corp.', 'stocks', '80.00', '418.00000000', '0.001800'),
    ('GOOGL', 'Alphabet Inc.', 'stocks', '79.00', '165.00000000', '0.002000'),
    ('AMZN', 'Amazon.com Inc.', 'stocks', '79.00', '186.00000000', '0.002200'),
    ('TSLA', 'Tesla Inc.', 'stocks', '78.00', '248.00000000', '0.003000'),
    ('META', 'Meta Platforms Inc.', 'stocks', '79.00', '575.00000000', '0.002200'),
    ('NVDA', 'NVIDIA Corp.', 'stocks', '80.00', '132.00000000', '0.002800'),
    ('NFLX', 'Netflix Inc.', 'stocks', '78.00', '705.00000000', '0.002400'),
]


class Command(BaseCommand):
    help = 'Seed (or refresh) the binary-options Instrument table.'

    def handle(self, *args, **options):
        created_count = 0
        for order, (symbol, name, category, payout, base_price, volatility) in enumerate(INSTRUMENTS):
            _, created = Instrument.objects.update_or_create(
                symbol=symbol,
                defaults={
                    'name': name,
                    'category': category,
                    'payout_percent': Decimal(payout),
                    'base_price': Decimal(base_price),
                    'volatility': Decimal(volatility),
                    'is_active': True,
                    'sort_order': order,
                },
            )
            if created:
                created_count += 1
        self.stdout.write(
            self.style.SUCCESS(
                f'Seeded {len(INSTRUMENTS)} instruments ({created_count} new).'
            )
        )
