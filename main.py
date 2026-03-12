"""
NSE/BSE Stock Analyzer - Swing Trading Pro (Upstox Live Edition)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Live prices via Upstox WebSocket
✅ Historical data via Upstox API
✅ Auto token refresh every day (one tap)
✅ All indicators: RSI, MACD, BB, EMA, Volume, S/R, Gann Fan
✅ Swing trading score + Buy/Sell suggestions
"""

import pandas as pd
import numpy as np
import threading
import json
import os
import requests
from datetime import datetime, timedelta

from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.clock import Clock
from kivy.graphics import Color, Rectangle, Line
from kivy.metrics import dp
from kivy.core.window import Window

from upstox_auth import UpstoxAuth, load_token
from upstox_feed import UpstoxLiveFeed, get_instrument_key

# ─────────────────────────────────────────────
# INDICATOR ENGINE
# ─────────────────────────────────────────────

class IndicatorEngine:
    def __init__(self, df):
        self.df = df.copy()
        self.close = df['Close']
        self.high = df['High']
        self.low = df['Low']
        self.volume = df['Volume']

    def rsi(self, period=14):
        delta = self.close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def macd(self, fast=12, slow=26, signal=9):
        ema_fast = self.close.ewm(span=fast).mean()
        ema_slow = self.close.ewm(span=slow).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram

    def bollinger_bands(self, period=20, std=2):
        sma = self.close.rolling(period).mean()
        std_dev = self.close.rolling(period).std()
        upper = sma + (std * std_dev)
        lower = sma - (std * std_dev)
        return upper, sma, lower

    def ema(self, period):
        return self.close.ewm(span=period).mean()

    def sma(self, period):
        return self.close.rolling(period).mean()

    def volume_analysis(self):
        avg_vol = self.volume.rolling(20).mean()
        return self.volume / avg_vol

    def support_resistance(self, window=10):
        highs = self.high.rolling(window, center=True).max()
        lows = self.low.rolling(window, center=True).min()
        resistance = highs[highs == self.high].dropna()
        support = lows[lows == self.low].dropna()
        return support.tail(3), resistance.tail(3)

    def gann_fan(self):
        recent_low_idx = self.low.tail(60).idxmin()
        recent_low = self.low[recent_low_idx]
        price_range = self.high.max() - self.low.min()
        unit = price_range / 100
        gann_levels = {
            "1x1 (45°)":   recent_low + unit * 1,
            "1x2 (26.5°)": recent_low + unit * 0.5,
            "2x1 (63.5°)": recent_low + unit * 2,
            "1x4 (15°)":   recent_low + unit * 0.25,
            "4x1 (75°)":   recent_low + unit * 4,
        }
        nearest = min(gann_levels, key=lambda k: abs(gann_levels[k] - self.close.iloc[-1]))
        return gann_levels, nearest, recent_low

    def swing_score(self):
        score = 0
        signals = []
        rsi_series = self.rsi()
        if rsi_series.empty or pd.isna(rsi_series.iloc[-1]): return 0, ["Insufficient Data"]
        
        rsi = rsi_series.iloc[-1]
        ml, sl, hist = self.macd()
        upper_bb, mid_bb, lower_bb = self.bollinger_bands()
        ema20 = self.ema(20).iloc[-1]
        ema50 = self.ema(50).iloc[-1]
        vol_ratio = self.volume_analysis().iloc[-1]
        current_price = self.close.iloc[-1]

        if 40 <= rsi <= 60:
            score += 15; signals.append("RSI Neutral")
        elif rsi < 35:
            score += 25; signals.append("RSI Oversold ✅")
        elif rsi > 65:
            score -= 10; signals.append("RSI Overbought ⚠️")

        if hist.iloc[-1] > 0 and hist.iloc[-2] < 0:
            score += 25; signals.append("MACD Bullish Crossover ✅")
        elif hist.iloc[-1] > 0:
            score += 10; signals.append("MACD Positive")
        elif hist.iloc[-1] < 0:
            score -= 10; signals.append("MACD Negative ⚠️")

        bb_pos = (current_price - lower_bb.iloc[-1]) / (upper_bb.iloc[-1] - lower_bb.iloc[-1])
        if bb_pos < 0.2:
            score += 20; signals.append("Near BB Lower ✅")
        elif bb_pos > 0.8:
            score -= 15; signals.append("Near BB Upper ⚠️")
        else:
            score += 5

        if ema20 > ema50:
            score += 15; signals.append("EMA Bullish ✅")
        else:
            score -= 5; signals.append("EMA Bearish")

        if vol_ratio > 1.5:
            score += 15; signals.append("High Volume ✅")
        elif vol_ratio > 1.0:
            score += 5

        return max(0, min(100, score)), signals

    def get_suggestion(self, score):
        if score >= 70: return "STRONG BUY 🚀", (0, 0.78, 0.32, 1)
        elif score >= 55: return "BUY 📈", (0.1, 0.6, 0.2, 1)
        elif score >= 40: return "HOLD / WATCH 👀", (1, 0.53, 0, 1)
        elif score >= 25: return "AVOID ⚠️", (0.8, 0.2, 0.1, 1)
        else: return "DO NOT ENTER 🚫", (0.9, 0.1, 0.1, 1)


# ─────────────────────────────────────────────
# DATA FETCHER (Now using Upstox)
# ─────────────────────────────────────────────

def fetch_historical(symbol, access_token=None):
    """
    Fetch 3 months of daily candle data.
    Uses Upstox API if token is provided, else falls back to Yahoo Finance.
    """
    clean_sym = symbol.upper().replace('.NS', '').replace('.BO', '')
    
    if access_token:
        # 1. Try Upstox API
        try:
            instrument_key = get_instrument_key(clean_sym)
            if not instrument_key:
                return None, f"Instrument key not found for {clean_sym}"
            
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=100)).strftime('%Y-%m-%d')
            
            url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/day/{end_date}/{start_date}"
            headers = {'Accept': 'application/json'}
            
            resp = requests.get(url, headers=headers, timeout=10)
            data = resp.json()
            
            if data.get('status') == 'success':
                candles = data['data']['candles']
                # Upstox returns: [timestamp, open, high, low, close, volume, open_interest]
                df = pd.DataFrame(candles, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'OI'])
                df['Date'] = pd.to_datetime(df['Date'])
                df = df.sort_values('Date').reset_index(drop=True)
                return df, None
            else:
                return None, f"Upstox Error: {data.get('errors', [{'message': 'Unknown'}])[0]['message']}"
        except Exception as e:
            return None, f"Upstox Request Error: {str(e)}"
    
    # 2. Fallback to Yahoo Finance (Offline mode)
    try:
        import yfinance as yf
        s = clean_sym + ".NS"
        t = yf.Ticker(s)
        df = t.history(period='3mo', interval='1d')
        if df.empty:
            df = yf.Ticker(clean_sym + ".BO").history(period='3mo', interval='1d')
        if df.empty:
            return None, "Symbol not found in Offline Mode."
        return df, None
    except Exception as e:
        return None, f"Offline Fetch Error: {str(e)}"


NIFTY50 = [
    "RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK",
    "HINDUNILVR","BAJFINANCE","KOTAKBANK","WIPRO","AXISBANK",
    "LT","SBIN","BHARTIARTL","ASIANPAINT","MARUTI",
    "SUNPHARMA","TITAN","ULTRACEMCO","NESTLEIND","POWERGRID"
]


# ─────────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────────

class AppState:
    auth = UpstoxAuth()
    live_feed = None
    access_token = None
    live_prices = {}
    price_callbacks = {}


# ─────────────────────────────────────────────
# SETUP SCREEN
# ─────────────────────────────────────────────

class SetupScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._build()

    def _build(self):
        layout = BoxLayout(orientation='vertical', padding=dp(20), spacing=dp(12))
        with layout.canvas.before:
            Color(0.04, 0.08, 0.18, 1)
            self._bg = Rectangle(pos=layout.pos, size=layout.size)
        layout.bind(pos=lambda i, v: setattr(self._bg, 'pos', v),
                    size=lambda i, v: setattr(self._bg, 'size', v))

        layout.add_widget(Label(
            text="[b]⚙️ Upstox API Setup[/b]", markup=True,
            font_size=dp(22), color=(0.2, 0.8, 1, 1),
            size_hint_y=None, height=dp(55)
        ))
        layout.add_widget(Label(
            text="Enter your Upstox Developer credentials.\nSaved only on your device.",
            font_size=dp(13), color=(0.7, 0.75, 0.85, 1),
            size_hint_y=None, height=dp(45), halign='center'
        ))

        self.api_key_input = TextInput(
            hint_text="API Key  (from developer.upstox.com)",
            multiline=False, size_hint_y=None, height=dp(48),
            background_color=(0.1, 0.14, 0.28, 1),
            foreground_color=(1, 1, 1, 1),
            hint_text_color=(0.5, 0.55, 0.7, 1), font_size=dp(14)
        )
        self.api_secret_input = TextInput(
            hint_text="API Secret",
            multiline=False, password=True,
            size_hint_y=None, height=dp(48),
            background_color=(0.1, 0.14, 0.28, 1),
            foreground_color=(1, 1, 1, 1),
            hint_text_color=(0.5, 0.55, 0.7, 1), font_size=dp(14)
        )
        layout.add_widget(self.api_key_input)
        layout.add_widget(self.api_secret_input)

        save_btn = Button(
            text="SAVE & CONTINUE →",
            size_hint_y=None, height=dp(52),
            background_color=(0.1, 0.55, 1, 1),
            font_size=dp(16), bold=True
        )
        save_btn.bind(on_press=self._save)
        layout.add_widget(save_btn)

        layout.add_widget(Label(
            text="Don't have API credentials yet?\n"
                 "Go to developer.upstox.com → Create New App\n"
                 "Set Redirect URL: http://127.0.0.1:8080",
            font_size=dp(12), color=(0.55, 0.65, 0.75, 1),
            size_hint_y=None, height=dp(65), halign='center'
        ))
        self.add_widget(layout)

    def _save(self, *args):
        key = self.api_key_input.text.strip()
        secret = self.api_secret_input.text.strip()
        if not key or not secret:
            popup = Popup(title="Missing Info", size_hint=(0.8, 0.28),
                          content=Label(text="Both fields are required.",
                                        color=(1, 0.4, 0.4, 1)))
            popup.open()
            return
        AppState.auth.configure(key, secret)
        self.manager.transition = SlideTransition(direction='left')
        self.manager.current = 'login'


# ─────────────────────────────────────────────
# LOGIN SCREEN (Daily one-tap)
# ─────────────────────────────────────────────

class LoginScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._build()

    def _build(self):
        layout = BoxLayout(orientation='vertical', padding=dp(24), spacing=dp(14))
        with layout.canvas.before:
            Color(0.04, 0.08, 0.18, 1)
            self._bg = Rectangle(pos=layout.pos, size=layout.size)
        layout.bind(pos=lambda i, v: setattr(self._bg, 'pos', v),
                    size=lambda i, v: setattr(self._bg, 'size', v))

        layout.add_widget(Label(
            text="[b]📊 NSE/BSE Swing Analyzer[/b]", markup=True,
            font_size=dp(23), color=(0.2, 0.8, 1, 1),
            size_hint_y=None, height=dp(55)
        ))
        layout.add_widget(Label(
            text="Powered by Upstox Data ⚡",
            font_size=dp(13), color=(0.5, 0.7, 0.9, 1),
            size_hint_y=None, height=dp(28)
        ))

        self.status_lbl = Label(
            text="Tap below to connect to live market data",
            font_size=dp(14), color=(0.7, 0.75, 0.85, 1),
            size_hint_y=None, height=dp(36), halign='center'
        )
        layout.add_widget(self.status_lbl)

        self.connect_btn = Button(
            text="⚡ CONNECT TO UPSTOX  (One Tap Daily)",
            size_hint_y=None, height=dp(58),
            background_color=(0.05, 0.55, 0.3, 1),
            font_size=dp(15), bold=True
        )
        self.connect_btn.bind(on_press=self._connect)
        layout.add_widget(self.connect_btn)

        offline_btn = Button(
            text="📉 Use Offline Mode  (Yahoo Finance — No Login)",
            size_hint_y=None, height=dp(46),
            background_color=(0.2, 0.2, 0.35, 1),
            font_size=dp(13)
        )
        offline_btn.bind(on_press=self._go_offline)
        layout.add_widget(offline_btn)

        self.add_widget(layout)

    def on_enter(self):
        token = load_token()
        if token:
            AppState.access_token = token
            self.status_lbl.text = "✅ Token valid for today — connecting..."
            Clock.schedule_once(lambda dt: self._launch_home(), 0.7)

    def _connect(self, *args):
        if not AppState.auth.is_configured():
            self.manager.current = 'setup'
            return
        self.connect_btn.disabled = True
        self.status_lbl.text = "⏳ Opening browser for Upstox login..."
        AppState.auth.get_token(
            on_ready=self._on_ready,
            on_error=self._on_error
        )

    def _on_ready(self, token):
        AppState.access_token = token
        Clock.schedule_once(lambda dt: self._launch_home())

    def _on_error(self, error):
        def _upd(dt):
            self.status_lbl.text = f"❌ {error}"
            self.connect_btn.disabled = False
        Clock.schedule_once(_upd)

    def _launch_home(self, *args):
        self.manager.transition = SlideTransition(direction='left')
        self.manager.current = 'home'

    def _go_offline(self, *args):
        AppState.access_token = None
        self.manager.transition = SlideTransition(direction='left')
        self.manager.current = 'home'


# ─────────────────────────────────────────────
# HOME SCREEN
# ─────────────────────────────────────────────

class HomeScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.live_feed = None
        self._build()

    def _build(self):
        layout = BoxLayout(orientation='vertical', padding=dp(8), spacing=dp(6))
        with layout.canvas.before:
            Color(0.04, 0.08, 0.18, 1)
            self._bg = Rectangle(pos=layout.pos, size=layout.size)
        layout.bind(pos=lambda i, v: setattr(self._bg, 'pos', v),
                    size=lambda i, v: setattr(self._bg, 'size', v))

        # Header
        hrow = BoxLayout(size_hint_y=None, height=dp(44))
        hrow.add_widget(Label(
            text="[b]📊 Swing Analyzer[/b]", markup=True,
            font_size=dp(19), color=(0.2, 0.8, 1, 1), halign='left'
        ))
        self.feed_dot = Label(
            text="● OFFLINE", font_size=dp(11),
            color=(0.55, 0.55, 0.55, 1),
            size_hint_x=0.38, halign='right'
        )
        hrow.add_widget(self.feed_dot)
        layout.add_widget(hrow)

        # Search
        srow = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        self.search_input = TextInput(
            hint_text="Stock symbol (RELIANCE, TCS...)",
            multiline=False, font_size=dp(13),
            background_color=(0.1, 0.13, 0.24, 1), foreground_color=(1, 1, 1, 1)
        )
        self.search_input.bind(on_text_validate=self._analyze)
        go_btn = Button(text="GO", size_hint_x=0.2, background_color=(0.1, 0.52, 1, 1), bold=True)
        go_btn.bind(on_press=self._analyze)
        srow.add_widget(self.search_input)
        srow.add_widget(go_btn)
        layout.add_widget(srow)

        # Buttons
        brow = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        scan_btn = Button(text="🔍 Scan Nifty 20", background_color=(0.05, 0.42, 0.26, 1), font_size=dp(12))
        scan_btn.bind(on_press=self._quick_scan)
        live_btn = Button(text="⚡ Start Live Feed", background_color=(0.45, 0.28, 0.05, 1), font_size=dp(12))
        live_btn.bind(on_press=self._start_live)
        brow.add_widget(scan_btn)
        brow.add_widget(live_btn)
        layout.add_widget(brow)

        self.status_lbl = Label(text="Ready.", font_size=dp(11), color=(0.5, 0.72, 0.58, 1), size_hint_y=None, height=dp(24))
        layout.add_widget(self.status_lbl)

        self.scroll = ScrollView()
        self.results = BoxLayout(orientation='vertical', size_hint_y=None, spacing=dp(6))
        self.results.bind(minimum_height=self.results.setter('height'))
        self.scroll.add_widget(self.results)
        layout.add_widget(self.scroll)
        self.add_widget(layout)

    def on_enter(self):
        self.feed_dot.text = "⚡ LIVE READY" if AppState.access_token else "● OFFLINE"
        self.feed_dot.color = (0.2, 0.9, 0.4, 1) if AppState.access_token else (0.55, 0.55, 0.55, 1)

    def _analyze(self, *args):
        sym = self.search_input.text.strip()
        if not sym: return
        self.results.clear_widgets()
        self.status_lbl.text = f"⏳ Fetching {sym.upper()} via Upstox..."
        threading.Thread(target=self._do_analyze, args=(sym,), daemon=True).start()

    def _do_analyze(self, sym):
        df, err = fetch_historical(sym, AppState.access_token)
        if err:
            Clock.schedule_once(lambda dt: setattr(self.status_lbl, 'text', f"❌ {err}"))
            return
        eng = IndicatorEngine(df)
        result = self._build_result(sym, df, eng)
        Clock.schedule_once(lambda dt: self._show_card(result))

    def _show_card(self, result):
        card = StockCard(result)
        self.results.add_widget(card)
        self.status_lbl.text = f"✅ {result['symbol']} analyzed"
        if AppState.access_token and self.live_feed:
            self.live_feed.add_symbol(result['symbol'])

    def _build_result(self, sym, df, eng):
        score, signals = eng.swing_score()
        suggestion, color = eng.get_suggestion(score)
        rsi_val = eng.rsi().iloc[-1]
        ml, sl, hist = eng.macd()
        ub, mb, lb = eng.bollinger_bands()
        return {
            'symbol': sym.upper(),
            'price': df['Close'].iloc[-1],
            'score': score, 'suggestion': suggestion, 'color': color,
            'rsi': rsi_val,
            'macd_hist': hist.iloc[-1],
            'bb_upper': ub.iloc[-1], 'bb_mid': mb.iloc[-1], 'bb_lower': lb.iloc[-1],
            'signals': signals,
        }

    def _quick_scan(self, *args):
        self.results.clear_widgets()
        self.status_lbl.text = "⏳ Scanning Nifty 20 via Upstox..."
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        results = []
        for i, sym in enumerate(NIFTY50):
            df, err = fetch_historical(sym, AppState.access_token)
            if err: continue
            eng = IndicatorEngine(df)
            results.append(self._build_result(sym, df, eng))
            Clock.schedule_once(lambda dt, n=i+1: setattr(self.status_lbl, 'text', f"⏳ {n}/20"))
        
        results.sort(key=lambda x: x['score'], reverse=True)
        def _done(dt):
            self.status_lbl.text = "✅ Scan complete"
            for r in results: self.results.add_widget(StockCard(r, compact=True))
        Clock.schedule_once(_done)

    def _start_live(self, *args):
        if not AppState.access_token:
            self.status_lbl.text = "❌ Upstox login required for live feed"
            return
        if self.live_feed: return
        self.live_feed = UpstoxLiveFeed(AppState.access_token, self._on_price, self._on_status)
        self.live_feed.start(NIFTY50[:5])

    def _on_price(self, symbol, ltp, change_pct):
        def _upd(dt):
            for w in self.results.children:
                if hasattr(w, 'data') and w.data['symbol'] == symbol:
                    w.update_live_price(ltp, change_pct)
        Clock.schedule_once(_upd)

    def _on_status(self, status, msg):
        Clock.schedule_once(lambda dt: setattr(self.status_lbl, 'text', f"⚡ {status.upper()}: {msg}"))


class StockCard(BoxLayout):
    def __init__(self, data, compact=False, **kwargs):
        super().__init__(orientation='vertical', size_hint_y=None, padding=dp(10), spacing=dp(3), **kwargs)
        self.data = data
        self.compact = compact
        self.height = dp(160) if compact else dp(320)
        self._build()

    def _build(self):
        d = self.data
        with self.canvas.before:
            Color(0.09, 0.13, 0.23, 1)
            self._bg = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._upd, size=self._upd)

        row = BoxLayout(size_hint_y=None, height=dp(30))
        row.add_widget(Label(text=f"[b]{d['symbol']}[/b]", markup=True, color=(0.2, 0.8, 1, 1)))
        self.price_lbl = Label(text=f"₹{d['price']:.2f}", color=(1, 1, 0.35, 1))
        row.add_widget(self.price_lbl)
        self.add_widget(row)

        self.add_widget(Label(text=d['suggestion'], bold=True, color=d['color'], size_hint_y=None, height=dp(25)))
        
        srow = BoxLayout(size_hint_y=None, height=dp(20))
        srow.add_widget(Label(text=f"Score: {d['score']}", size_hint_x=0.3))
        srow.add_widget(ProgressBar(max=100, value=d['score']))
        self.add_widget(srow)

        self.add_widget(Label(text=f"RSI: {d['rsi']:.1f}", size_hint_y=None, height=dp(20)))
        
        if not self.compact:
            self.add_widget(Label(text=f"BB: ▲{d['bb_upper']:.1f} ▼{d['bb_lower']:.1f}", size_hint_y=None, height=dp(20)))
            self.add_widget(Label(text=" | ".join(d['signals']), font_size=dp(10), color=(0.6, 0.6, 0.6, 1)))

    def update_live_price(self, ltp, change_pct):
        self.price_lbl.text = f"₹{ltp:.2f} ({change_pct:+.2f}%)"

    def _upd(self, *args):
        self._bg.pos, self._bg.size = self.pos, self.size

class StockAnalyzerApp(App):
    def build(self):
        Window.bind(on_keyboard=self._on_back_button)
        sm = ScreenManager()
        sm.add_widget(SetupScreen(name='setup'))
        sm.add_widget(LoginScreen(name='login'))
        sm.add_widget(HomeScreen(name='home'))
        sm.current = 'login' if AppState.auth.is_configured() else 'setup'
        return sm

    def _on_back_button(self, window, key, *args):
        if key == 27: return False
        return False

if __name__ == '__main__':
    StockAnalyzerApp().run()
