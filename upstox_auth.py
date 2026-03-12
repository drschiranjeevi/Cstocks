"""
Upstox OAuth 2.0 Authentication Manager
- Handles daily token refresh automatically
- Saves token to local file
- Opens system browser for one-tap login
"""

import requests
import json
import os
import time
import threading
from datetime import date
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from plyer import webbrowser

# ── Config file path (stored on device) ──
# On Android, these will be saved in the app's private internal storage
TOKEN_FILE = "upstox_token.json"
CONFIG_FILE = "upstox_config.json"


def save_config(api_key, api_secret, redirect_uri="http://127.0.0.1:8080"):
    config = {
        "api_key": api_key,
        "api_secret": api_secret,
        "redirect_uri": redirect_uri
    }
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)


def save_token(access_token):
    data = {
        "access_token": access_token,
        "date": str(date.today())
    }
    with open(TOKEN_FILE, 'w') as f:
        json.dump(data, f)


def load_token():
    """Load token — returns None if expired or missing"""
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, 'r') as f:
            data = json.load(f)
        # Check if token is from today
        if data.get("date") == str(date.today()):
            return data.get("access_token")
    except Exception:
        pass
    return None


class _AuthCallbackHandler(BaseHTTPRequestHandler):
    """Local HTTP server to catch OAuth redirect"""
    auth_code = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if 'code' in params:
            _AuthCallbackHandler.auth_code = params['code'][0]
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"""
                <html><body style='background:#0a1628;color:#00c8ff;font-family:sans-serif;text-align:center;padding:50px'>
                <h1 style='font-size:48px'>&#10003;</h1>
                <h2>Login Successful!</h2>
                <p>You can close this tab and return to the Stock Analyzer app.</p>
                </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress console logs


class UpstoxAuth:
    def __init__(self):
        self.config = load_config()
        self.access_token = None
        self._on_token_ready = None
        self._on_error = None

    def is_configured(self):
        return self.config is not None

    def configure(self, api_key, api_secret):
        # We use 127.0.0.1 as it's more stable on some Android versions than 'localhost'
        save_config(api_key, api_secret, "http://127.0.0.1:8080")
        self.config = load_config()

    def get_token(self, on_ready, on_error):
        self._on_token_ready = on_ready
        self._on_error = on_error

        token = load_token()
        if token:
            self.access_token = token
            on_ready(token)
            return

        threading.Thread(target=self._oauth_flow, daemon=True).start()

    def _oauth_flow(self):
        if not self.config:
            self._on_error("App not configured.")
            return

        api_key = self.config['api_key']
        api_secret = self.config['api_secret']
        redirect_uri = self.config['redirect_uri']

        auth_url = (
            f"https://api.upstox.com/v2/login/authorization/dialog"
            f"?response_type=code"
            f"&client_id={api_key}"
            f"&redirect_uri={redirect_uri}"
        )

        # Step 2: Start local server
        _AuthCallbackHandler.auth_code = None
        try:
            # Bind to 127.0.0.1 specifically
            server = HTTPServer(('127.0.0.1', 8080), _AuthCallbackHandler)
            server.timeout = 1  # Short timeout for checking auth_code loop
        except Exception as e:
            self._on_error(f"Server error: {e}")
            return

        # Step 3: Open system browser via Plyer
        try:
            webbrowser.open(auth_url)
        except Exception:
            # Fallback to standard if plyer fails
            import webbrowser as wb
            wb.open(auth_url)

        # Step 4: Wait for code (timeout 5 mins for mobile)
        start = time.time()
        while _AuthCallbackHandler.auth_code is None:
            server.handle_request()
            if time.time() - start > 300:
                server.server_close()
                self._on_error("Login timed out.")
                return
        
        auth_code = _AuthCallbackHandler.auth_code
        server.server_close()

        # Step 5: Exchange code
        try:
            response = requests.post(
                "https://api.upstox.com/v2/login/authorization/token",
                data={
                    "code": auth_code,
                    "client_id": api_key,
                    "client_secret": api_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code"
                },
                timeout=15
            )
            result = response.json()
            if "access_token" in result:
                token = result["access_token"]
                save_token(token)
                self.access_token = token
                self._on_token_ready(token)
            else:
                self._on_error(result.get('message', 'Auth failed'))
        except Exception as e:
            self._on_error(f"Connection error: {e}")
