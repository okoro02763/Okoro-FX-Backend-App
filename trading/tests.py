"""Regression tests for the binary-options trading API.

The production incident these guard against: ``GET /api/trading/trades/``
returned 500 with ``relation "trading_pendingorder" does not exist`` because
the pending-order table was missing while the view called
``process_pending_orders()`` inline. A trade list must still be served when a
lazy housekeeping job fails.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from market.models import Instrument
from trading.models import PendingOrder

User = get_user_model()


class TradesEndpointTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='trader', email='trader@example.com', password='pw-123456'
        )
        self.client.force_login(self.user)

    def test_trades_returns_200_with_no_trades(self):
        response = self.client.get('/api/trading/trades/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(response.data['results'], [])

    def test_trades_survives_missing_pending_order_table(self):
        """The exact production failure must no longer break the trade list."""
        with mock.patch(
            'trading.views.process_pending_orders',
            side_effect=Exception('relation "trading_pendingorder" does not exist'),
        ):
            response = self.client.get('/api/trading/trades/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 0)

    def test_trades_returns_200_with_an_open_pending_order(self):
        instrument = Instrument.objects.create(
            symbol='EUR/USD', name='Euro / US Dollar', category='currencies'
        )
        PendingOrder.objects.create(
            user=self.user,
            instrument=instrument,
            direction='buy',
            stake=Decimal('10.00'),
            trigger_price=Decimal('1.00000000'),
            status='pending',
        )
        response = self.client.get('/api/trading/trades/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 0)


class HealthEndpointTest(TestCase):
    def test_health_reports_ok_when_schema_is_current(self):
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'ok')
        self.assertEqual(response.data['database'], 'ok')
        self.assertEqual(response.data['migrations'], 'ok')
        self.assertEqual(response.data['unapplied_migrations'], [])

    def test_health_is_public(self):
        """No auth header/session -- the platform probes this unauthenticated."""
        self.client.logout()
        self.assertEqual(self.client.get('/api/health').status_code, 200)
