from django.utils import timezone
from rest_framework import serializers

from .models import Trade, Wallet


class TradeSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source='instrument.symbol', read_only=True)
    instrument_name = serializers.CharField(source='instrument.name', read_only=True)
    category = serializers.CharField(source='instrument.category', read_only=True)
    direction_display = serializers.CharField(source='get_direction_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    seconds_remaining = serializers.SerializerMethodField()
    potential_profit = serializers.SerializerMethodField()

    class Meta:
        model = Trade
        fields = [
            'id',
            'symbol',
            'instrument_name',
            'category',
            'direction',
            'direction_display',
            'status',
            'status_display',
            'stake',
            'payout_percent',
            'gross_payout',
            'potential_profit',
            'profit',
            'open_price',
            'close_price',
            'expiry_time',
            'created_at',
            'settled_at',
            'seconds_remaining',
        ]

    def get_seconds_remaining(self, obj) -> int:
        if obj.status != 'open':
            return 0
        delta = obj.expiry_time - timezone.now()
        return max(0, int(delta.total_seconds()))

    def get_potential_profit(self, obj) -> float:
        # What the trade pays out in profit if it wins.
        if obj.status != 'open':
            return float(obj.profit)
        return float(obj.gross_payout - obj.stake)


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ['demo_balance', 'updated_at']
