"""
Polymarket User Channel WebSocket — real-time order/trade updates.

Replaces 2s polling for ambush fill detection with instant WS push (~0ms).

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/user
Auth: API key/secret/passphrase in subscription message.

Events:
  - trade: MATCHED/MINED/CONFIRMED/RETRYING/FAILED
  - order: PLACEMENT/UPDATE/CANCELLATION
"""

import json
import threading
import time
import logging
import websocket

logger = logging.getLogger("ai_trader.user_ws")

_USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class UserTradeStream:
    """Singleton WebSocket client for user trade/order events."""

    def __init__(self):
        self._ws = None
        self._thread = None
        self._running = False
        self._creds = None  # {api_key, api_secret, api_passphrase}
        self._markets = []  # condition_ids to subscribe
        self._lock = threading.Lock()
        self._callbacks = []  # list of callback(event_dict)
        self._last_trades = {}  # order_id -> trade event (latest)
        self._fill_event = threading.Event()  # signal when any fill happens
        self._connected = False

    def init(self, api_key, api_secret, api_passphrase):
        """Store credentials. Connection is lazy (on first subscribe)."""
        self._creds = {
            "apiKey": api_key,
            "secret": api_secret,
            "passphrase": api_passphrase,
        }

    def on_fill(self, callback):
        """Register callback for trade MATCHED events. callback(event_dict)."""
        self._callbacks.append(callback)

    def subscribe_market(self, condition_id):
        """Subscribe to a market's user events."""
        if condition_id in self._markets:
            return
        self._markets.append(condition_id)
        if self._connected and self._ws:
            # Send subscription update
            try:
                msg = {
                    "auth": self._creds,
                    "markets": self._markets,
                    "type": "user",
                }
                self._ws.send(json.dumps(msg))
            except Exception as e:
                logger.debug(f"User WS subscribe error: {e}")

    def unsubscribe_market(self, condition_id):
        """Remove a market from subscriptions."""
        if condition_id in self._markets:
            self._markets.remove(condition_id)

    def get_latest_trade(self, order_id):
        """Get latest trade event for an order_id, or None."""
        return self._last_trades.get(order_id)

    def wait_for_fill(self, timeout=2.0):
        """Block until a fill event or timeout. Returns True if fill happened."""
        self._fill_event.clear()
        return self._fill_event.wait(timeout=timeout)

    def start(self):
        """Start WS connection in background thread."""
        if self._running or not self._creds:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="user-ws")
        self._thread.start()
        logger.info("User WS: starting...")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _run(self):
        """WS connection loop with auto-reconnect."""
        backoff = 2
        while self._running:
            try:
                from ai_trader.ws_ssl import get_websocket_sslopt
                sslopt = get_websocket_sslopt()

                self._ws = websocket.WebSocketApp(
                    _USER_WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(sslopt=sslopt, ping_interval=9, ping_timeout=5)
            except Exception as e:
                logger.warning(f"User WS error: {e}")

            self._connected = False
            if self._running:
                time.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)

    def _on_open(self, ws):
        self._connected = True
        backoff = 2
        logger.info("User WS: connected")
        # Send auth + subscribe (empty markets = all markets)
        if self._creds:
            msg = {
                "auth": self._creds,
                "markets": self._markets if self._markets else [],
                "type": "user",
            }
            ws.send(json.dumps(msg))
            logger.info(f"User WS: subscribed ({'all markets' if not self._markets else f'{len(self._markets)} markets'})")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message) if isinstance(message, str) else json.loads(message.decode())
        except Exception:
            return

        if not isinstance(data, dict):
            return

        event_type = data.get("event_type") or data.get("type", "")

        if event_type == "trade" or data.get("type") == "TRADE":
            # Trade event — order matched/mined/confirmed/failed
            status = data.get("status", "")
            order_ids = []

            # Collect all order_ids involved
            taker_oid = data.get("taker_order_id")
            if taker_oid:
                order_ids.append(taker_oid)
            for maker in data.get("maker_orders", []):
                moid = maker.get("order_id")
                if moid:
                    order_ids.append(moid)

            # Store latest trade for each order_id
            for oid in order_ids:
                self._last_trades[oid] = data

            if status == "MATCHED":
                # Signal fill
                self._fill_event.set()
                # Notify callbacks
                for cb in self._callbacks:
                    try:
                        cb(data)
                    except Exception as e:
                        logger.warning(f"User WS callback error: {e}")

                asset_id = data.get("asset_id", "")[:12]
                side = data.get("side", "?")
                size = data.get("size", "?")
                price = data.get("price", "?")
                logger.info(f"User WS: FILL {side} {size}@{price} asset={asset_id}...")

        elif event_type == "order":
            order_type = data.get("type", "")
            if order_type == "CANCELLATION":
                oid = data.get("id", "")[:12]
                logger.debug(f"User WS: order cancelled {oid}")

    def _on_error(self, ws, error):
        logger.debug(f"User WS error: {error}")

    def _on_close(self, ws, close_status_code=None, close_msg=None):
        self._connected = False
        logger.info("User WS: disconnected")


# Singleton
user_trade_stream = UserTradeStream()
