
import json
import pandas as pd
import requests
from decimal import Decimal
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from .authentication import BearerTokenAuthentication
from .models import CurrencyPair, PriceData, Trade, DerivAccount, Profile
from .permissions import IsAdminUser
from .deriv_client import deriv_client
from kyc.services import user_is_verified as kyc_user_is_verified
import talib
import numpy as np
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)

# Upper bound on a single order's size, in lots. Anything larger is rejected
# outright rather than merely flagged by the fraud engine after the fact.
MAX_TRADE_QUANTITY = Decimal('100')

@api_view(['GET'])
@permission_classes([AllowAny])
def get_currency_pairs(request):
    """Get all available currency pairs"""
    pairs = CurrencyPair.objects.filter(is_active=True)
    data = [{'id': p.id, 'symbol': p.symbol, 'name': p.name} for p in pairs]
    return Response(data)

@api_view(['GET'])
@permission_classes([AllowAny])
def get_live_prices(request):
    """Get current prices for all active currency pairs"""
    pairs = CurrencyPair.objects.filter(is_active=True)
    prices = []
    
    for pair in pairs:
        latest_price = PriceData.objects.filter(currency_pair=pair).first()
        if latest_price:
            prices.append({
                'symbol': pair.symbol,
                'bid': float(latest_price.bid_price),
                'ask': float(latest_price.ask_price),
                'spread': float(latest_price.ask_price - latest_price.bid_price),
                'timestamp': latest_price.timestamp.isoformat()
            })
    
    return Response(prices)

@api_view(['GET'])
@permission_classes([AllowAny])
def get_price_history(request, symbol):
    """Get historical price data for technical analysis"""
    try:
        pair = CurrencyPair.objects.get(symbol=symbol)
        prices = PriceData.objects.filter(currency_pair=pair)[:100]
        
        data = []
        for price in reversed(prices):
            data.append({
                'timestamp': price.timestamp.isoformat(),
                'bid': float(price.bid_price),
                'ask': float(price.ask_price),
                'close': float((price.bid_price + price.ask_price) / 2)
            })
        
        return Response(data)
    except CurrencyPair.DoesNotExist:
        return Response({'error': 'Currency pair not found'}, status=404)

@api_view(['GET'])
@permission_classes([AllowAny])
def get_technical_analysis(request, symbol):
    """Calculate technical indicators using real Deriv data"""
    try:
        pair = CurrencyPair.objects.get(symbol=symbol)
        prices = PriceData.objects.filter(currency_pair=pair)[:50]
        
        if len(prices) < 20:
            return Response({'error': 'Insufficient data for analysis'}, status=400)
        
        # Convert to arrays for TA-Lib
        closes = np.array([float((p.bid_price + p.ask_price) / 2) for p in reversed(prices)])
        
        # Calculate indicators
        sma_20 = talib.SMA(closes, timeperiod=20)
        rsi = talib.RSI(closes, timeperiod=14)
        macd, macd_signal, macd_hist = talib.MACD(closes)
        
        # Determine trend
        current_price = closes[-1]
        sma_current = sma_20[-1] if not np.isnan(sma_20[-1]) else current_price
        
        if current_price > sma_current * 1.001:  # 0.1% above SMA
            trend = 'BULLISH'
        elif current_price < sma_current * 0.999:  # 0.1% below SMA  
            trend = 'BEARISH'
        else:
            trend = 'NEUTRAL'
        
        analysis = {
            'symbol': symbol,
            'current_price': float(current_price),
            'sma_20': float(sma_20[-1]) if not np.isnan(sma_20[-1]) else None,
            'rsi': float(rsi[-1]) if not np.isnan(rsi[-1]) else None,
            'macd': float(macd[-1]) if not np.isnan(macd[-1]) else None,
            'macd_signal': float(macd_signal[-1]) if not np.isnan(macd_signal[-1]) else None,
            'trend': trend,
            'data_source': 'Deriv API'
        }
        
        return Response(analysis)
    except CurrencyPair.DoesNotExist:
        return Response({'error': 'Currency pair not found'}, status=404)
    except Exception:
        logger.exception('Error in technical analysis')
        return Response(
            {'error': 'Could not compute analysis.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def place_trade(request):
    """Place a new trade using Deriv API"""
    try:
        profile = getattr(request.user, 'profile', None)
        if profile is None:
            profile = Profile.objects.get_or_create(user=request.user)[0]

        if profile.account_type == 'live' and not kyc_user_is_verified(request.user):
            return Response(
                {
                    'error': 'Live trading requires an approved identity verification. Complete KYC first.',
                    'code': 'KYC_REQUIRED',
                },
                status=403,
            )

        data = request.data

        symbol = (data.get('symbol') or '').strip()
        trade_type = (data.get('trade_type') or '').strip().upper()
        if trade_type not in dict(Trade.TRADE_TYPES):
            return Response(
                {'error': 'trade_type must be BUY or SELL.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            quantity = Decimal(str(data.get('quantity')))
        except (TypeError, ValueError, ArithmeticError):
            return Response(
                {'error': 'quantity must be a number.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not quantity.is_finite():
            return Response(
                {'error': 'quantity must be a finite number.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if quantity <= 0 or quantity > MAX_TRADE_QUANTITY:
            return Response(
                {
                    'error': (
                        f'quantity must be between 0 and {MAX_TRADE_QUANTITY}.'
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            pair = CurrencyPair.objects.get(symbol=symbol)
        except CurrencyPair.DoesNotExist:
            return Response({'error': 'Currency pair not found'}, status=404)

        def _optional_decimal(key):
            raw = data.get(key)
            if raw in (None, ''):
                return None
            try:
                value = Decimal(str(raw))
            except (TypeError, ValueError, ArithmeticError):
                raise ValueError(key)
            if not value.is_finite() or value < 0:
                raise ValueError(key)
            return value

        try:
            stop_loss = _optional_decimal('stop_loss')
            take_profit = _optional_decimal('take_profit')
        except ValueError as exc:
            return Response(
                {'error': f'{exc.args[0]} must be a positive number.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get current price from our database
        latest_price = PriceData.objects.filter(currency_pair=pair).first()
        if not latest_price:
            return Response({'error': 'No price data available'}, status=400)
        
        # Determine entry price
        entry_price = latest_price.ask_price if trade_type == 'BUY' else latest_price.bid_price
        
        # Create trade record
        trade = Trade.objects.create(
            currency_pair=pair,
            trade_type=trade_type,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        try:
            from monitoring.activity import is_large_trade, send_activity_on_commit

            notional = Decimal(str(entry_price)) * Decimal(str(quantity))
            if is_large_trade(notional):
                send_activity_on_commit(
                    f'Large single trade | user_id: {request.user.pk} | symbol: {symbol} | stake: {notional} | direction: {trade_type}',
                    level='warning',
                    user_id=request.user.pk,
                    dedupe_key=f'large-spot-trade:{trade.pk}',
                    cooldown_seconds=3600,
                    fields={'symbol': symbol, 'stake': notional, 'direction': trade_type},
                )
        except Exception:  # noqa: BLE001 - alerting must never fail a trade
            pass

        # Place order through Deriv API
        if deriv_client.is_connected:
            result = deriv_client.place_trade_order(
                symbol=symbol,
                trade_type=trade_type,
                amount=float(quantity),
                stop_loss=float(stop_loss) if stop_loss else None,
                take_profit=float(take_profit) if take_profit else None,
            )
            
            if result.get('success'):
                logger.info("Trade placed through Deriv API: %s", trade.id)
            else:
                logger.error("Deriv API error: %s", result.get('error'))
        else:
            logger.warning("Deriv API not connected, trade placed locally only")

        from .email_service import send_template_email

        send_template_email(
            'trade_notification',
            request.user.email,
            {
                'name': request.user.first_name or 'there',
                'symbol': symbol,
                'trade_type': trade_type,
                'quantity': f'{quantity}',
                'entry_price': f'{entry_price}',
                'status': 'OPEN',
            },
            defer=True,
        )
        
        return Response({
            'trade_id': trade.id,
            'message': f"{trade_type} order placed successfully",
            'entry_price': float(trade.entry_price),
            'deriv_connected': deriv_client.is_connected
        })
        
    except CurrencyPair.DoesNotExist:
        return Response({'error': 'Currency pair not found'}, status=404)
    except Exception:
        logger.exception('Error placing trade')
        return Response(
            {'error': 'Could not place the trade.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

@api_view(['POST'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def close_trade(request, trade_id):
    """Close an open trade"""
    try:
        trade = get_object_or_404(Trade, id=trade_id, status='OPEN')
        
        # Get current price
        latest_price = PriceData.objects.filter(currency_pair=trade.currency_pair).first()
        if not latest_price:
            return Response({'error': 'No price data available'}, status=400)
        
        # Determine exit price
        exit_price = latest_price.bid_price if trade.trade_type == 'BUY' else latest_price.ask_price
        
        # Calculate profit/loss
        if trade.trade_type == 'BUY':
            profit_loss = (exit_price - trade.entry_price) * trade.quantity
        else:
            profit_loss = (trade.entry_price - exit_price) * trade.quantity
        
        # Close through Deriv API if connected
        if deriv_client.is_connected and trade.deriv_contract_id:
            deriv_client.close_position(trade.deriv_contract_id)
        
        # Update trade
        trade.exit_price = exit_price
        trade.profit_loss = profit_loss
        trade.status = 'CLOSED'
        trade.closed_at = timezone.now()
        trade.save()

        from .email_service import send_template_email

        send_template_email(
            'trade_notification',
            request.user.email,
            {
                'name': request.user.first_name or 'there',
                'symbol': trade.currency_pair.symbol,
                'trade_type': trade.trade_type,
                'quantity': f'{trade.quantity}',
                'entry_price': f'{trade.entry_price}',
                'exit_price': f'{trade.exit_price}',
                'profit_loss': f'{trade.profit_loss}',
                'status': 'CLOSED',
            },
            defer=True,
        )
        
        return Response({
            'message': 'Trade closed successfully',
            'exit_price': float(exit_price),
            'profit_loss': float(profit_loss),
            'deriv_connected': deriv_client.is_connected
        })
        
    except Exception:
        logger.exception('Error closing trade')
        return Response(
            {'error': 'Could not close the trade.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def get_trades(request):
    """Get all trades"""
    trades = Trade.objects.all()
    data = []
    
    for trade in trades:
        data.append({
            'id': trade.id,
            'symbol': trade.currency_pair.symbol,
            'trade_type': trade.trade_type,
            'status': trade.status,
            'entry_price': float(trade.entry_price),
            'exit_price': float(trade.exit_price) if trade.exit_price else None,
            'quantity': float(trade.quantity),
            'profit_loss': float(trade.profit_loss) if trade.profit_loss else None,
            'opened_at': trade.opened_at.isoformat(),
            'closed_at': trade.closed_at.isoformat() if trade.closed_at else None,
            'deriv_contract_id': trade.deriv_contract_id
        })
    
    return Response(data)

@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def get_portfolio_summary(request):
    """Get portfolio summary"""
    open_trades = Trade.objects.filter(status='OPEN')
    closed_trades = Trade.objects.filter(status='CLOSED')
    
    total_profit_loss = sum([float(t.profit_loss) for t in closed_trades if t.profit_loss])
    open_positions = len(open_trades)
    total_trades = Trade.objects.count()
    
    # Calculate win rate
    winning_trades = closed_trades.filter(profit_loss__gt=0).count()
    win_rate = (winning_trades / len(closed_trades)) if closed_trades else 0
    
    return Response({
        'total_profit_loss': total_profit_loss,
        'open_positions': open_positions,
        'total_trades': total_trades,
        'win_rate': win_rate,
        'deriv_connected': deriv_client.is_connected
    })

@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated])
def get_deriv_status(request):
    """Get Deriv API connection status"""
    return Response({
        'connected': deriv_client.is_connected,
        'api_url': deriv_client.ws.url if deriv_client.ws else None,
        'subscriptions': len(deriv_client.subscriptions),
        'supported_pairs': list(deriv_client.forex_symbols.keys())
    })

@api_view(['GET'])
@authentication_classes([BearerTokenAuthentication])
@permission_classes([IsAuthenticated, IsAdminUser])
def get_account_info(request):
    """Get Deriv account information"""
    try:
        if deriv_client.is_connected:
            deriv_client.get_account_balance()
            deriv_client.get_portfolio()
            
        # Get from database
        accounts = DerivAccount.objects.all()
        account_data = []
        
        for account in accounts:
            account_data.append({
                'account_id': account.account_id,
                'currency': account.currency,
                'balance': float(account.balance),
                'is_demo': account.is_demo,
                'last_updated': account.last_updated.isoformat()
            })
        
        return Response({
            'accounts': account_data,
            'deriv_connected': deriv_client.is_connected
        })
        
    except Exception:
        logger.exception('Error getting account info')
        return Response(
            {'error': 'Could not load account info.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )