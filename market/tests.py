"""Tests for the market data providers and their routing/fallback logic.

Upstream HTTP calls are mocked throughout: the suite must never depend on
Twelve Data or CoinGecko being reachable (or on their free-tier quotas).
"""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from . import providers, services
from .models import Instrument

MARKETS = [
    {
        'id': 'bitcoin',
        'name': 'Bitcoin',
        'current_price': 82000.0,
        'high_24h': 83000.0,
        'low_24h': 80000.0,
        'price_change_24h': 2000.0,
        'price_change_percentage_24h': 2.5,
        'last_updated': '2026-10-19T12:00:00.000Z',
    },
    {
        'id': 'ethereum',
        'name': 'Ethereum',
        'current_price': 2400.0,
        'high_24h': 2450.0,
        'low_24h': 2350.0,
        'price_change_24h': -25.0,
        'price_change_percentage_24h': -1.0,
        'last_updated': '2026-10-19T12:00:00.000Z',
    },
]

TD_QUOTE = {
    'symbol': 'EUR/USD',
    'name': 'Euro / US Dollar',
    'close': '1.1234',
    'previous_close': '1.1200',
    'open': '1.1210',
    'high': '1.1250',
    'low': '1.1190',
    'percent_change': '0.30',
    'timestamp': 1700000000,
}


class FakeProvider:
    """Minimal stand-in used to exercise the routed chain."""

    def __init__(self, name, quote=None, candles=None, error=None):
        self.name = name
        self._quote = quote
        self._candles = candles
        self._error = error

    def quote(self, symbol):
        if self._error:
            raise self._error
        return self._quote

    def candles(self, symbol, interval, outputsize, end_time=None):
        if self._error:
            raise self._error
        return self._candles


class BucketOhlcTests(SimpleTestCase):
    def test_folds_points_into_bars(self):
        # 1699999800 and 1700000100 are 5-minute bucket boundaries.
        points = [
            [1699999800000, 100.0],
            [1700000040000, 120.0],
            [1700000080000, 90.0],
            [1700000120000, 95.0],  # next 5-minute bucket
        ]
        rows = providers._bucket_ohlc(points, 300, 10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['time'], 1699999800)
        self.assertEqual(rows[0]['open'], 100.0)
        self.assertEqual(rows[0]['high'], 120.0)
        self.assertEqual(rows[0]['low'], 90.0)
        self.assertEqual(rows[0]['close'], 90.0)
        self.assertEqual(rows[1]['open'], 95.0)

    def test_end_time_filter_and_outputsize(self):
        points = [[(1000 + i * 300) * 1000, float(i)] for i in range(10)]
        rows = providers._bucket_ohlc(points, 300, 3, end_time=1000 + 5 * 300)
        self.assertEqual(len(rows), 3)
        self.assertLessEqual(rows[-1]['time'], 1000 + 5 * 300)


class TwelveDataParseTests(SimpleTestCase):
    def test_quote_mapping(self):
        quote = providers.TwelveDataProvider._parse_quote('EUR/USD', TD_QUOTE)
        self.assertEqual(quote['symbol'], 'EUR/USD')
        self.assertEqual(quote['price'], 1.1234)
        self.assertEqual(quote['prev_close'], 1.12)
        self.assertAlmostEqual(quote['change'], 0.0034)
        self.assertEqual(quote['percent_change'], 0.30)
        self.assertEqual(quote['timestamp'], 1700000000)
        self.assertEqual(quote['source'], 'twelvedata')

    def test_candles_are_ascending_and_utc(self):
        payload = {
            'values': [
                {'datetime': '2026-10-19 15:40:00', 'open': '2', 'high': '3', 'low': '1', 'close': '2.5'},
                {'datetime': '2026-10-19 15:39:00', 'open': '1', 'high': '2', 'low': '0.5', 'close': '2'},
            ]
        }
        rows = providers.TwelveDataProvider._parse_candles(payload)
        self.assertEqual(len(rows), 2)
        self.assertLess(rows[0]['time'], rows[1]['time'])
        self.assertEqual(rows[1]['time'] - rows[0]['time'], 60)
        self.assertEqual(rows[1]['close'], 2.5)

    def test_candles_respect_end_time(self):
        payload = {
            'values': [
                {'datetime': '2026-10-19 15:40:00', 'open': '2', 'high': '3', 'low': '1', 'close': '2.5'},
                {'datetime': '2026-10-19 15:39:00', 'open': '1', 'high': '2', 'low': '0.5', 'close': '2'},
            ]
        }
        rows = providers.TwelveDataProvider._parse_candles(payload)
        rows = providers.TwelveDataProvider._parse_candles(payload, end_time=rows[0]['time'])
        self.assertEqual(len(rows), 1)


class CoinGeckoProviderTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        providers.reset_budgets()
        self.provider = providers.CoinGeckoProvider(
            'CG-test', 'https://api.coingecko.com/api/v3'
        )

    def test_quote_maps_market_row(self):
        with mock.patch.object(self.provider, '_get', return_value=MARKETS) as get:
            quote = self.provider.quote('BTC/USD')
        self.assertEqual(get.call_count, 1)
        self.assertEqual(quote['symbol'], 'BTC/USD')
        self.assertEqual(quote['name'], 'Bitcoin')
        self.assertEqual(quote['price'], 82000.0)
        self.assertEqual(quote['prev_close'], 80000.0)
        self.assertEqual(quote['high'], 83000.0)
        self.assertEqual(quote['low'], 80000.0)
        self.assertEqual(quote['change'], 2000.0)
        self.assertEqual(quote['percent_change'], 2.5)
        self.assertEqual(quote['source'], 'coingecko')
        self.assertGreater(quote['timestamp'], 0)

    def test_batch_is_cached_across_symbols(self):
        with mock.patch.object(self.provider, '_get', return_value=MARKETS) as get:
            btc = self.provider.quote('BTC/USD')
            eth = self.provider.quote('ETH/USD')
        self.assertEqual(get.call_count, 1)
        self.assertEqual(btc['price'], 82000.0)
        self.assertEqual(eth['price'], 2400.0)

    def test_unknown_symbol_raises(self):
        with self.assertRaises(ValueError):
            self.provider.quote('EUR/USD')

    def test_candles_fold_market_chart_points(self):
        prices = {'prices': [[1700000000000, 100.0], [1700000060000, 120.0]]}
        with mock.patch.object(self.provider, '_get', return_value=prices) as get:
            rows = self.provider.candles('BTC/USD', '5m', 10)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['high'], 120.0)
        self.assertEqual(rows[0]['close'], 120.0)

    def test_sub_five_minute_intervals_are_unsupported(self):
        with mock.patch.object(self.provider, '_get') as get:
            for interval in ('5s', '15s', '1m'):
                with self.assertRaises(ValueError):
                    self.provider.candles('BTC/USD', interval, 100)
        get.assert_not_called()

    def test_chart_window_matches_sampling_tier(self):
        self.assertEqual(self.provider._chart_days(300, 100), 1)
        self.assertEqual(self.provider._chart_days(900, 100), 1)
        self.assertEqual(self.provider._chart_days(3600, 100), 7)
        self.assertEqual(self.provider._chart_days(3600, 500), 30)
        self.assertEqual(self.provider._chart_days(14400, 500), 30)
        self.assertEqual(self.provider._chart_days(86400, 100), 365)

    def test_auth_header_depends_on_host(self):
        demo = providers.CoinGeckoProvider('CG-demo', 'https://api.coingecko.com/api/v3')
        pro = providers.CoinGeckoProvider('CG-pro', 'https://pro-api.coingecko.com/api/v3')
        self.assertIn('x-cg-demo-api-key', demo._headers)
        self.assertIn('x-cg-pro-api-key', pro._headers)


class BudgetAndStaleTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        providers.reset_budgets()

    def test_spent_budget_serves_last_known_quote(self):
        provider = providers.TwelveDataProvider(
            'td-test', 'https://example.test', budget_per_minute=1
        )
        with mock.patch.object(provider, '_get', return_value=TD_QUOTE):
            first = provider.quote('EUR/USD')
        self.assertEqual(first['price'], 1.1234)

        # The minute budget is spent: the upstream must not be called again and
        # the last known good quote is re-served instead.
        with mock.patch.object(provider, '_get', side_effect=AssertionError('called')) as get:
            second = provider.quote('EUR/USD')
        self.assertEqual(get.call_count, 0)
        self.assertEqual(second['price'], 1.1234)
        self.assertEqual(second['source'], 'twelvedata')

    def test_budget_exhaustion_without_cache_raises(self):
        provider = providers.TwelveDataProvider(
            'td-test', 'https://example.test', budget_per_minute=1
        )
        with mock.patch.object(provider, '_get', side_effect=ValueError('boom')):
            with self.assertRaises(ValueError):
                provider.quote('EUR/USD')
        with self.assertRaises(providers.UpstreamError):
            provider.quote('EUR/USD')


class RoutedProviderTests(SimpleTestCase):
    def test_first_working_provider_wins(self):
        failing = FakeProvider('failing', error=ValueError('nope'))
        working = FakeProvider(
            'working',
            quote={'symbol': 'BTC/USD', 'price': 1.0, 'source': 'working'},
            candles=[{'time': 1, 'open': 1, 'high': 1, 'low': 1, 'close': 1}],
        )
        routed = providers.RoutedProvider(
            quotes=(failing, working), candles=(failing, working), name='test'
        )
        self.assertEqual(routed.quote('BTC/USD')['source'], 'working')
        self.assertEqual(len(routed.candles('BTC/USD', '1m', 1)), 1)
        self.assertTrue(routed.is_upstream)

    def test_all_providers_failing_raises_last_error(self):
        routed = providers.RoutedProvider(
            quotes=(FakeProvider('a', error=ValueError('a')),
                    FakeProvider('b', error=ValueError('b'))),
            candles=(FakeProvider('a', error=ValueError('a')),
                     FakeProvider('b', error=ValueError('b'))),
        )
        with self.assertRaises(ValueError):
            routed.quote('BTC/USD')
        with self.assertRaises(ValueError):
            routed.candles('BTC/USD', '1m', 1)


class ProviderRoutingTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        providers.reset_budgets()

    @override_settings(TWELVE_DATA_API_KEY='td', COINGECKO_API_KEY='cg')
    def test_crypto_is_routed_between_both_upstreams(self):
        provider = providers.get_provider('BTC/USD')
        self.assertIsInstance(provider, providers.RoutedProvider)
        self.assertEqual(provider.name, 'coingecko+twelvedata')

    @override_settings(TWELVE_DATA_API_KEY='td', COINGECKO_API_KEY='cg')
    def test_forex_stays_on_twelvedata(self):
        self.assertIsInstance(providers.get_provider('EUR/USD'), providers.TwelveDataProvider)

    @override_settings(TWELVE_DATA_API_KEY='', COINGECKO_API_KEY='cg')
    def test_crypto_without_twelvedata_uses_coingecko_only(self):
        self.assertIsInstance(providers.get_provider('BTC/USD'), providers.CoinGeckoProvider)

    @override_settings(TWELVE_DATA_API_KEY='td', COINGECKO_API_KEY='')
    def test_crypto_without_coingecko_uses_twelvedata(self):
        self.assertIsInstance(providers.get_provider('BTC/USD'), providers.TwelveDataProvider)

    @override_settings(TWELVE_DATA_API_KEY='', COINGECKO_API_KEY='')
    def test_without_keys_the_simulated_feed_is_used(self):
        self.assertIsInstance(providers.get_provider('BTC/USD'), providers.SimulatedProvider)


class MarketServicesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Instrument.objects.create(
            symbol='BTC/USD',
            name='Bitcoin / US Dollar',
            category='crypto',
            base_price=Decimal('67000'),
            volatility=Decimal('0.004'),
        )
        Instrument.objects.create(
            symbol='EUR/USD',
            name='Euro / US Dollar',
            category='currencies',
            base_price=Decimal('1.085'),
            volatility=Decimal('0.0006'),
        )

    def setUp(self):
        cache.clear()
        providers.reset_budgets()

    @override_settings(TWELVE_DATA_API_KEY='', COINGECKO_API_KEY='CG-test')
    def test_crypto_quote_comes_from_coingecko(self):
        with mock.patch('market.providers.CoinGeckoProvider._get', return_value=MARKETS):
            quote = services.get_quote('BTC/USD')
        self.assertEqual(quote['source'], 'coingecko')
        self.assertEqual(quote['category'], 'crypto')
        self.assertIn('payout_percent_short', quote)

    @override_settings(TWELVE_DATA_API_KEY='td-test', COINGECKO_API_KEY='')
    def test_quote_degrades_to_simulated_when_upstream_fails(self):
        with mock.patch('market.providers.TwelveDataProvider._get', side_effect=ValueError('down')):
            quote = services.get_quote('EUR/USD')
        self.assertEqual(quote['source'], 'simulated')

    @override_settings(TWELVE_DATA_API_KEY='td-test', COINGECKO_API_KEY='CG-test')
    def test_crypto_candles_fall_back_to_coingecko(self):
        prices = {'prices': [[1700000000000, 100.0], [1700000060000, 110.0]]}
        with mock.patch(
            'market.providers.TwelveDataProvider._get', side_effect=ValueError('rate limited')
        ), mock.patch('market.providers.CoinGeckoProvider._get', return_value=prices):
            rows = services.get_candles('BTC/USD', '5m', 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['open'], 100.0)
        self.assertEqual(rows[0]['close'], 110.0)

    @override_settings(TWELVE_DATA_API_KEY='td-test', COINGECKO_API_KEY='CG-test')
    def test_crypto_candles_prefer_twelvedata(self):
        payload = {
            'values': [
                {'datetime': '2026-10-19 15:40:00', 'open': '2', 'high': '3', 'low': '1', 'close': '2.5'},
            ]
        }
        with mock.patch(
            'market.providers.TwelveDataProvider._get', return_value=payload
        ), mock.patch('market.providers.CoinGeckoProvider._get') as cg_get:
            rows = services.get_candles('BTC/USD', '1m', 10)
        cg_get.assert_not_called()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['close'], 2.5)
