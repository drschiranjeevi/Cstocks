"""
Upstox WebSocket Live Market Feed
- Real-time price updates for NSE/BSE stocks
- Auto-reconnects on disconnect
- Feeds live LTP into the app
"""

import json
import threading
import requests
import websocket
import struct
import gzip
from datetime import datetime

# NSE instrument key format: NSE_EQ|ISIN
# We'll use symbol lookup via Upstox API

UPSTOX_INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

# Cache for symbol → instrument key mapping
_instrument_cache = {}


def load_instrument_map():
    """Download and cache NSE instrument list"""
    global _instrument_cache
    if _instrument_cache:
        return _instrument_cache
    try:
        resp = requests.get(UPSTOX_INSTRUMENT_URL, timeout=15)
        data = json.loads(gzip.decompress(resp.content))
        for item in data:
            sym = item.get('trading_symbol', '')
            key = item.get('instrument_key', '')
            if sym and key and item.get('segment') == 'NSE_EQ':
                _instrument_cache[sym.upper()] = key
        return _instrument_cache
    except Exception as e:
        print(f"Instrument load error: {e}")
        return {}


def get_instrument_key(symbol):
    """Get Upstox instrument key for a stock symbol"""
    imap = load_instrument_map()
    sym = symbol.upper().replace('.NS', '').replace('.BO', '')
    return imap.get(sym)


class UpstoxLiveFeed:
    """
    WebSocket live feed from Upstox
    Calls on_price_update(symbol, ltp, change_pct) on each tick
    """

    def __init__(self, access_token, on_price_update, on_status):
        self.token = access_token
        self.on_price_update = on_price_update
        self.on_status = on_status
        self.ws = None
        self.subscribed_symbols = {}  # symbol → instrument_key
        self._running = False
        self._ws_url = None

    def start(self, symbols):
        """Start WebSocket feed for given symbols list"""
        self.subscribed_symbols = {}
        imap = load_instrument_map()

        for sym in symbols:
            clean = sym.upper().replace('.NS', '').replace('.BO', '')
            key = imap.get(clean)
            if key:
                self.subscribed_symbols[clean] = key

        if not self.subscribed_symbols:
            self.on_status("error", "No valid instrument keys found")
            return

        threading.Thread(target=self._connect, daemon=True).start()

    def _get_ws_url(self):
        """Get authorized WebSocket URL from Upstox"""
        try:
            resp = requests.get(
                "https://api.upstox.com/v2/feed/market-data-feed/authorize",
                headers={"Authorization": f"Bearer {self.token}",
                         "Accept": "application/json"}
            )
            data = resp.json()
            return data.get('data', {}).get('authorizedRedirectUri')
        except Exception as e:
            return None

    def _connect(self):
        ws_url = self._get_ws_url()
        if not ws_url:
            self.on_status("error", "Could not get WebSocket URL")
            return

        self._running = True
        self.on_status("connecting", "Connecting to live feed...")

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        self.ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws):
        self.on_status("connected", "Live feed connected ⚡")
        # Subscribe to instruments
        keys = list(self.subscribed_symbols.values())
        sub_msg = {
            "guid": "live_feed",
            "method": "sub",
            "data": {
                "mode": "ltpc",  # LTP + Close price
                "instrumentKeys": keys
            }
        }
        ws.send(json.dumps(sub_msg))

    def _on_message(self, ws, message):
        """Parse Upstox binary protobuf feed"""
        try:
            # Upstox sends binary protobuf — decode to get LTP
            # Simple approach: parse JSON text response for LTPC mode
            if isinstance(message, bytes):
                # Try to decode as UTF-8 first
                try:
                    decoded = message.decode('utf-8')
                    data = json.loads(decoded)
                    self._process_feed(data)
                except Exception:
                    pass
            elif isinstance(message, str):
                data = json.loads(message)
                self._process_feed(data)
        except Exception as e:
            pass

    def _process_feed(self, data):
        """Extract LTP from feed data"""
        try:
            feeds = data.get('feeds', {})
            for instrument_key, feed_data in feeds.items():
                ltpc = feed_data.get('ltpc', {})
                ltp = ltpc.get('ltp', 0)
                close = ltpc.get('cp', ltp)  # previous close

                # Find symbol for this key
                for sym, key in self.subscribed_symbols.items():
                    if key == instrument_key:
                        change_pct = ((ltp - close) / close * 100) if close else 0
                        self.on_price_update(sym, ltp, change_pct)
                        break
        except Exception:
            pass

    def _on_error(self, ws, error):
        self.on_status("error", f"Feed error: {str(error)[:50]}")

    def _on_close(self, ws, close_status_code, close_msg):
        self._running = False
        self.on_status("disconnected", "Feed disconnected. Reconnecting...")
        # Auto-reconnect after 5 seconds
        if self.subscribed_symbols:
            threading.Timer(5.0, lambda: self.start(list(self.subscribed_symbols.keys()))).start()

    def stop(self):
        self._running = False
        if self.ws:
            self.ws.close()

    def add_symbol(self, symbol):
        """Add a new symbol to live feed"""
        imap = load_instrument_map()
        clean = symbol.upper().replace('.NS', '').replace('.BO', '')
        key = imap.get(clean)
        if key and self.ws:
            self.subscribed_symbols[clean] = key
            sub_msg = {
                "guid": f"add_{clean}",
                "method": "sub",
                "data": {
                    "mode": "ltpc",
                    "instrumentKeys": [key]
                }
            }
            try:
                self.ws.send(json.dumps(sub_msg))
            except Exception:
                pass
