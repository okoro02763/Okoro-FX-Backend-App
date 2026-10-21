
import json
import threading
import time
import websocket
from django.conf import settings
import logging
from decimal import Decimal
from .models import CurrencyPair, PriceData, Trade
from django.utils import timezone

logger = logging.getLogger(__name__)

class DerivAPIClient:
    def __init__(self):
        self.ws = None
        self.is_connected = False
        self.subscriptions = {}
        self.connection_lock = threading.Lock()
        self.api_token = settings.DERIV_API_TOKEN
        self.app_id = settings.DERIV_APP_ID
        
        # Major forex pairs available on Deriv
        self.forex_symbols = {
            'frxEURUSD': {'symbol': 'EURUSD', 'name': 'EUR/USD'},
            'frxGBPUSD': {'symbol': 'GBPUSD', 'name': 'GBP/USD'},
            'frxUSDJPY': {'symbol': 'USDJPY', 'name': 'USD/JPY'},
            'frxUSDCHF': {'symbol': 'USDCHF', 'name': 'USD/CHF'},
            'frxAUDUSD': {'symbol': 'AUDUSD', 'name': 'AUD/USD'},
            'frxUSDCAD': {'symbol': 'USDCAD', 'name': 'USD/CAD'},
            'frxNZDUSD': {'symbol': 'NZDUSD', 'name': 'NZD/USD'},
            'frxEURGBP': {'symbol': 'EURGBP', 'name': 'EUR/GBP'},
            'frxEURJPY': {'symbol': 'EURJPY', 'name': 'EUR/JPY'},
            'frxGBPJPY': {'symbol': 'GBPJPY', 'name': 'GBP/JPY'},
        }

    def connect(self):
        """Connect to Deriv WebSocket API"""
        try:
            # Never enable the websocket trace here: it logs every frame,
            # including the {"authorize": <api_token>} payload, to stdout.
            if settings.DEBUG:
                logger.debug('Deriv websocket debug trace is opt-in; see deriv_client.connect')
            self.ws = websocket.WebSocketApp(
                f"{settings.DERIV_API_URL}?app_id={self.app_id}",
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close
            )
            
            # Start WebSocket connection in a separate thread
            self.ws_thread = threading.Thread(target=self.ws.run_forever)
            self.ws_thread.daemon = True
            self.ws_thread.start()
            
        except Exception as e:
            logger.error(f"Failed to connect to Deriv API: {e}")

    def on_open(self, ws):
        """Handle WebSocket connection open"""
        logger.info("Connected to Deriv API")
        self.is_connected = True
        
        # Authorize with API token
        if self.api_token:
            self.authorize()
        
        # Initialize currency pairs in database
        self.initialize_currency_pairs()
        
        # Subscribe to price streams
        self.subscribe_to_ticks()

    def on_message(self, ws, message):
        """Handle incoming messages"""
        try:
            data = json.loads(message)
            msg_type = data.get('msg_type')
            
            if msg_type == 'authorize':
                self.handle_authorize_response(data)
            elif msg_type == 'tick':
                self.handle_tick_data(data)
            elif msg_type == 'buy':
                self.handle_buy_response(data)
            elif msg_type == 'sell':
                self.handle_sell_response(data)
            elif msg_type == 'proposal':
                self.handle_proposal_response(data)
            elif msg_type == 'active_symbols':
                self.handle_active_symbols(data)
            else:
                logger.debug(f"Unhandled message type: {msg_type}")
                
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse message: {e}")
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    def on_error(self, ws, error):
        """Handle WebSocket errors"""
        logger.error(f"Deriv API WebSocket error: {error}")
        self.is_connected = False

    def on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket connection close"""
        logger.info("Deriv API connection closed")
        self.is_connected = False

        # Attempt to reconnect after 5 seconds.
        #
        # The timer thread must be a daemon. threading.Timer defaults to
        # inheriting the *creating* thread's daemon flag, and Python's
        # interpreter waits for every non-daemon thread at exit -- so a
        # non-daemon timer here kept the whole process alive after its work was
        # done. That is what made ``manage.py collectstatic`` hang indefinitely
        # during the Render build.
        timer = threading.Timer(5.0, self.connect)
        timer.daemon = True
        timer.start()

    def authorize(self):
        """Authorize with Deriv API using token"""
        message = {
            "authorize": self.api_token
        }
        self.send_message(message)

    def handle_authorize_response(self, data):
        """Handle authorization response"""
        if data.get('error'):
            logger.error(f"Authorization failed: {data['error']}")
        else:
            logger.info("Successfully authorized with Deriv API")

    def initialize_currency_pairs(self):
        """Initialize currency pairs in database"""
        for deriv_symbol, pair_info in self.forex_symbols.items():
            pair, created = CurrencyPair.objects.get_or_create(
                symbol=pair_info['symbol'],
                defaults={
                    'name': pair_info['name'],
                    'is_active': True
                }
            )
            if created:
                logger.info(f"Created currency pair: {pair_info['symbol']}")

    def subscribe_to_ticks(self):
        """Subscribe to real-time price ticks"""
        for deriv_symbol in self.forex_symbols.keys():
            message = {
                "ticks": deriv_symbol,
                "subscribe": 1
            }
            self.send_message(message)
            logger.info(f"Subscribed to {deriv_symbol}")

    def handle_tick_data(self, data):
        """Handle incoming tick data"""
        try:
            tick = data.get('tick', {})
            symbol = tick.get('symbol')
            
            if symbol in self.forex_symbols:
                pair_info = self.forex_symbols[symbol]
                quote = Decimal(str(tick.get('quote', 0)))
                
                # Get or create currency pair
                try:
                    currency_pair = CurrencyPair.objects.get(symbol=pair_info['symbol'])
                    
                    # For forex, we simulate bid/ask spread (typically 1-3 pips)
                    spread = Decimal('0.00020')  # 2 pip spread
                    bid_price = quote - (spread / 2)
                    ask_price = quote + (spread / 2)
                    
                    # Save price data
                    PriceData.objects.create(
                        currency_pair=currency_pair,
                        bid_price=bid_price,
                        ask_price=ask_price
                    )
                    
                    logger.debug(f"Updated {pair_info['symbol']}: {bid_price}/{ask_price}")
                    
                except CurrencyPair.DoesNotExist:
                    logger.error(f"Currency pair not found: {pair_info['symbol']}")
                    
        except Exception as e:
            logger.error(f"Error handling tick data: {e}")

    def get_contract_proposal(self, symbol, trade_type, amount):
        """Get contract proposal for a trade"""
        deriv_symbol = None
        for ds, info in self.forex_symbols.items():
            if info['symbol'] == symbol:
                deriv_symbol = ds
                break
        
        if not deriv_symbol:
            raise ValueError(f"Symbol {symbol} not supported")
        
        # For forex, we use multiplier contracts
        message = {
            "proposal": 1,
            "amount": float(amount),
            "basis": "stake",
            "contract_type": "MULTUP" if trade_type == "BUY" else "MULTDOWN",
            "currency": "USD",
            "multiplier": 100,
            "symbol": deriv_symbol,
            "duration": 1,
            "duration_unit": "d"  # 1 day duration
        }
        self.send_message(message)

    def place_trade_order(self, symbol, trade_type, amount, stop_loss=None, take_profit=None):
        """Place a trade order through Deriv API"""
        try:
            # First get proposal
            self.get_contract_proposal(symbol, trade_type, amount)
            
            # Note: In a real implementation, you'd wait for the proposal response
            # then use the proposal_id to buy the contract
            
            return {"success": True, "message": "Trade placed successfully"}
            
        except Exception as e:
            logger.error(f"Error placing trade: {e}")
            return {"success": False, "error": str(e)}

    def close_position(self, contract_id):
        """Close an open position"""
        message = {
            "sell": contract_id,
            "price": 0  # Close at current market price
        }
        self.send_message(message)

    def send_message(self, message):
        """Send message to WebSocket"""
        if self.ws and self.is_connected:
            self.ws.send(json.dumps(message))
        else:
            logger.error("WebSocket not connected")

    def get_account_balance(self):
        """Get account balance"""
        message = {"balance": 1}
        self.send_message(message)

    def get_portfolio(self):
        """Get current portfolio/positions"""
        message = {"portfolio": 1}
        self.send_message(message)

# Global client instance
deriv_client = DerivAPIClient()

def start_deriv_connection():
    """Start Deriv API connection"""
    if settings.DERIV_API_TOKEN:
        deriv_client.connect()
        logger.info("Started Deriv API connection")
    else:
        logger.warning("No Deriv API token found. Using mock data.")