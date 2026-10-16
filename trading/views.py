"""REST endpoints for the binary-options demo (paper trading only).

All views here require authentication (the project-wide DRF default); every
query is scoped to ``request.user``. Money movement lives in
``trading.services`` so the views stay thin and the invariants (stake locked
on open, idempotent settlement, wallet credited exactly once) are enforced in
one place.
"""

from datetime import datetime
from datetime import timezone as dt_timezone

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Trade
from .serializers import TradeSerializer, WalletSerializer
from .services import (
    TradeError,
    get_wallet,
    open_trade,
    reset_wallet,
    settle_expired_trades,
    settle_trade,
)

MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 50


def _parse_expiry(raw):
    """Accept an ISO-8601 string (or None) and return an aware datetime."""
    if raw in (None, ''):
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        text = str(raw).replace('Z', '+00:00')
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise TradeError('expiry_time must be an ISO-8601 datetime') from exc


@api_view(['GET', 'POST'])
def trades(request):
    """List the caller's trades, or open a new one.

    GET  ``/api/trading/trades/?status=open|closed|won|lost|refund&limit=50``
         Lazy-settles anything past its expiry first, so open positions and
         the wallet are always current even without a Celery worker.
    POST ``/api/trading/trades/`` with
         ``{symbol, direction, stake, duration_seconds? 60, expiry_time?}``
         Locks the stake and opens a binary option at the current market price.
    """
    if request.method == 'POST':
        data = request.data or {}
        try:
            expiry_time = _parse_expiry(data.get('expiry_time'))
            trade = open_trade(
                request.user,
                symbol=data.get('symbol'),
                direction=data.get('direction'),
                stake=data.get('stake'),
                duration_seconds=data.get('duration_seconds'),
                expiry_time=expiry_time,
            )
        except TradeError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        wallet = get_wallet(request.user)
        return Response(
            {
                'trade': TradeSerializer(trade).data,
                'wallet': WalletSerializer(wallet).data,
            },
            status=status.HTTP_201_CREATED,
        )

    # Lazy settlement: keep state correct without depending on a worker.
    settle_expired_trades()

    qs = Trade.objects.filter(user=request.user).select_related('instrument')
    status_filter = (request.query_params.get('status') or '').strip().lower()
    if status_filter == 'open':
        qs = qs.filter(status='open')
    elif status_filter == 'closed':
        qs = qs.exclude(status='open')
    elif status_filter in ('won', 'lost', 'refund'):
        qs = qs.filter(status=status_filter)

    try:
        limit = int(request.query_params.get('limit') or DEFAULT_LIST_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIST_LIMIT
    limit = max(1, min(limit, MAX_LIST_LIMIT))

    wallet = get_wallet(request.user)
    return Response(
        {
            'count': qs.count(),
            'results': TradeSerializer(qs[:limit], many=True).data,
            'wallet': WalletSerializer(wallet).data,
        }
    )


@api_view(['POST'])
def settle_view(request):
    """Force settlement of expired trades (demo helper).

    POST with ``{"trade_id": 12}`` settles that one trade if it is the
    caller's and already expired; without a body it sweeps every expired open
    trade. Settlement is idempotent, so calling this is always safe.
    """
    trade_id = (request.data or {}).get('trade_id') or request.query_params.get('trade_id')

    if trade_id:
        trade = Trade.objects.filter(pk=trade_id, user=request.user).first()
        if trade is None:
            return Response(
                {'detail': 'Trade not found.'}, status=status.HTTP_404_NOT_FOUND
            )
        if trade.status != 'open':
            return Response(
                {'detail': 'Trade is already settled.', 'trade': TradeSerializer(trade).data}
            )
        if trade.expiry_time > timezone.now():
            return Response(
                {'detail': 'Trade has not expired yet.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        trade = settle_trade(trade.pk)
        return Response({'trade': TradeSerializer(trade).data, 'wallet': WalletSerializer(get_wallet(request.user)).data})

    count = settle_expired_trades()
    return Response({'settled': count})


@api_view(['GET'])
def get_wallet_view(request):
    """The caller's demo wallet (created on first access)."""
    wallet = get_wallet(request.user)
    return Response(WalletSerializer(wallet).data)


@api_view(['POST'])
def reset_wallet_view(request):
    """Reset the demo balance to the configured starting amount."""
    wallet = reset_wallet(request.user)
    return Response(
        {'detail': 'Demo balance reset.', 'wallet': WalletSerializer(wallet).data}
    )
