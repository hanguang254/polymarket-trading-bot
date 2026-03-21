"""
Polymarket RTDS (Real-Time Data Socket) — Chainlink 链上价格流

WebSocket 连接 wss://ws-live-data.polymarket.com，订阅 crypto_prices_chainlink
获取 Polymarket 结算用的 Chainlink 喂价，零延迟读内存。

文档: https://docs.polymarket.com/market-data/websocket/rtds
"""
import json
import threading
import time

try:
    import websocket
    _HAS_WEBSOCKET = True
except ImportError:
    _HAS_WEBSOCKET = False

RTDS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL = 5  # 文档要求每5秒 PING
STALE_THRESHOLD = 5  # 超过5秒无更新视为过期

# Chainlink symbol 映射（从 coins.py 动态加载）
def _load_chainlink_symbols():
    try:
        from ai_trader.coins import get_coins_config
        return {coin: cfg["chainlink"] for coin, cfg in get_coins_config().items() if cfg.get("chainlink")}
    except Exception:
        return {"BTC": "btc/usd", "ETH": "eth/usd"}

CHAINLINK_SYMBOLS = _load_chainlink_symbols()
# 反向映射
_SYMBOL_TO_COIN = {v: k for k, v in CHAINLINK_SYMBOLS.items()}


class ChainlinkPriceStream:
    """后台 WebSocket 持续接收 Chainlink 链上价格，get_price() 零延迟读内存"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.prices = {}        # {"BTC": 83521.50, "ETH": 1920.30}
        self.last_update = {}   # {"BTC": time.time(), ...}
        self._running = False
        self._thread = None
        self._ws = None
        self._reconnect_delay = 2
        self._price_event = threading.Event()  # 每次收到新价格时 set

    def start(self):
        if not _HAS_WEBSOCKET:
            print("  ⚠️ websocket-client 未安装，RTDS Chainlink 不可用")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._connect_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def get_price(self, coin="BTC"):
        """获取 Chainlink 链上实时价格，数据超过5秒未更新则返回 None"""
        price = self.prices.get(coin)
        last = self.last_update.get(coin, 0)
        if price and (time.time() - last) < STALE_THRESHOLD:
            return price
        return None

    def wait_for_update(self, timeout=1.0):
        """阻塞等待 Chainlink 推送新价格，或到 timeout 秒后返回。
        返回 True 表示收到新价格，False 表示超时（兜底轮询）。"""
        updated = self._price_event.wait(timeout=timeout)
        self._price_event.clear()
        return updated

    # ── WebSocket 内部 ──

    def _connect_loop(self):
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    RTDS_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws.run_forever(ping_interval=0)
            except Exception as e:
                if self._running:
                    print(f"  ⚠️ RTDS Chainlink 连接异常: {e}")
            if self._running:
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30)

    def _on_open(self, ws):
        self._reconnect_delay = 2
        print("  ✅ RTDS Chainlink 已连接 (结算价格流)")
        # 订阅 Chainlink 价格
        msg = json.dumps({
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": "",
                }
            ]
        })
        ws.send(msg)
        # 启动 PING 心跳
        threading.Thread(target=self._ping_loop, daemon=True).start()

    def _ping_loop(self):
        while self._running and self._ws:
            try:
                self._ws.send("PING")
            except Exception:
                break
            time.sleep(PING_INTERVAL)

    def _on_message(self, ws, message):
        if message == "PONG":
            return
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        topic = data.get("topic", "")
        if topic != "crypto_prices_chainlink":
            return

        payload = data.get("payload")
        if not payload:
            return

        symbol = payload.get("symbol", "")  # "btc/usd", "eth/usd"
        value = payload.get("value")
        coin = _SYMBOL_TO_COIN.get(symbol)
        if coin and value is not None:
            try:
                self.prices[coin] = float(value)
                self.last_update[coin] = time.time()
                self._price_event.set()  # 通知监控循环有新价格
            except (ValueError, TypeError):
                pass

    def _on_error(self, ws, error):
        if self._running:
            print(f"  ⚠️ RTDS Chainlink 错误: {error}")

    def _on_close(self, ws, code, msg):
        if self._running:
            print(f"  ⚠️ RTDS Chainlink 断开(code={code})，{self._reconnect_delay}s后重连...")


# 全局单例
chainlink_stream = ChainlinkPriceStream()
