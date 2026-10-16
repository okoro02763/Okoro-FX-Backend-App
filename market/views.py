from django.core.cache import cache
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import services
from .models import Instrument
from .serializers import InstrumentSerializer

MAX_QUOTES_PER_REQUEST = 25


def _parse_symbols(raw: str):
    symbols = [s.strip() for s in (raw or '').split(',') if s.strip()]
    return symbols[:MAX_QUOTES_PER_REQUEST]


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([])
def list_instruments(request):
    """All tradable instruments, optionally filtered by category.

    Pass ``include_quote=1`` to attach the latest price to every row. That
    costs one cached provider call per instrument, so callers should keep the
    filtered set small (or use ``/api/market/quotes/`` for batch lookups).
    """
    instruments = Instrument.objects.filter(is_active=True)
    category = (request.query_params.get('category') or '').strip()
    if category:
        instruments = instruments.filter(category=category)

    payload = InstrumentSerializer(instruments, many=True).data

    if request.query_params.get('include_quote') in ('1', 'true', 'yes'):
        for row in payload:
            quote = services.get_quote(row['symbol'])
            if quote:
                row['price'] = quote['price']
                row['percent_change'] = quote['percent_change']
                row['source'] = quote['source']

    return Response({'count': len(payload), 'results': payload})


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([])
def quotes(request):
    """Batch quotes: ``/api/market/quotes/?symbols=EUR/USD,BTC/USD``."""
    symbols = _parse_symbols(request.query_params.get('symbols'))
    if not symbols:
        return Response(
            {'detail': 'symbols query parameter is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    results = [q for s in symbols if (q := services.get_quote(s))]
    return Response({'count': len(results), 'results': results})


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([])
def quote(request):
    """Latest price for one symbol: ``/api/market/quote/?symbol=EUR/USD``."""
    symbol = (request.query_params.get('symbol') or '').strip()
    quote_payload = services.get_quote(symbol) if symbol else None
    if not quote_payload:
        return Response(
            {'detail': f'Unknown or inactive symbol: {symbol}'},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(quote_payload)


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([])
def candles(request):
    """OHLC history: ``/api/market/candles/?symbol=EUR/USD&interval=1m&outputsize=200``."""
    symbol = (request.query_params.get('symbol') or '').strip()
    interval = (request.query_params.get('interval') or '1m').strip()
    outputsize_raw = request.query_params.get('outputsize') or '200'

    try:
        outputsize = int(outputsize_raw)
    except (TypeError, ValueError):
        outputsize = 200

    end_time_raw = request.query_params.get('end_time')
    try:
        end_time = int(end_time_raw) if end_time_raw else None
    except (TypeError, ValueError):
        end_time = None

    payload = services.get_candles(symbol, interval, outputsize, end_time=end_time)
    if payload is None:
        return Response(
            {'detail': f'Unknown symbol or unsupported interval: {symbol}/{interval}'},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response({'symbol': symbol, 'interval': interval, 'results': payload})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([])
def refresh_cache(request):
    """Debug/dev helper: drop cached market data."""
    cache.clear()
    return Response({'status': 'ok'})
