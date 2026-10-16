from rest_framework import serializers

from .models import Instrument


class InstrumentSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source='get_category_display', read_only=True)

    class Meta:
        model = Instrument
        fields = [
            'id',
            'symbol',
            'name',
            'category',
            'category_display',
            'payout_percent',
            'payout_percent_short',
            'payout_percent_long',
            'is_active',
            'sort_order',
        ]


class QuoteSerializer(serializers.Serializer):
    """Shape of the payloads returned by market.services.get_quote()."""

    symbol = serializers.CharField()
    name = serializers.CharField(required=False, allow_blank=True, default='')
    price = serializers.FloatField()
    prev_close = serializers.FloatField(required=False, default=0)
    open = serializers.FloatField(required=False, default=0)
    high = serializers.FloatField(required=False, default=0)
    low = serializers.FloatField(required=False, default=0)
    change = serializers.FloatField(required=False, default=0)
    percent_change = serializers.FloatField(required=False, default=0)
    timestamp = serializers.IntegerField()
    source = serializers.CharField(required=False, default='simulated')
    payout_percent = serializers.FloatField(required=False, default=0)
    payout_percent_short = serializers.FloatField(required=False, default=0)
    payout_percent_long = serializers.FloatField(required=False, default=0)
    category = serializers.CharField(required=False, default='')
