"""
币安 API 数据采集模块
"""
import json
import requests
import threading
import time
from datetime import datetime

from ai_trader.ws_ssl import get_websocket_sslopt

try:
    import websocket
    _HAS_WEBSOCKET = True
except ImportError:
    _HAS_WEBSOCKET = False

BINANCE_API = "https://api.binance.com"


# ═══ Binance WebSocket 实时价格流（全局单例） ═══
class BinancePriceStream:
    """后台 WebSocket 持续接收 BTC/ETH 实时成交价，get_price() 零延迟读内存"""
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
        self.prices = {}       # {"BTC": 83521.50, "ETH": 1920.30}
        self.last_update = {}  # {"BTC": time.time(), ...}
        self._ws = None
        self._running = False
        self._thread = None

    def start(self):
        if not _HAS_WEBSOCKET:
            print("  ⚠️ websocket-client 未安装，WebSocket 不可用")
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._connect, daemon=True)
        self._thread.start()

    def _connect(self):
        while self._running:
            try:
                from ai_trader.coins import get_coins_config
                streams = "/".join(cfg["binance_ws"] for cfg in get_coins_config().values())
                url = f"wss://stream.binance.com:9443/ws/{streams}"
                self._ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws.run_forever(
                    sslopt=get_websocket_sslopt(),
                    ping_interval=30,
                    ping_timeout=10,
                )
            except Exception as e:
                print(f"  ⚠️ WebSocket 连接异常: {e}")
            if self._running:
                time.sleep(2)

    def _on_open(self, ws):
        print("  ✅ Binance WebSocket 已连接 (实时价格流)")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            symbol = data.get("s", "")
            price = float(data["p"])
            # 动态解析: BTCUSDT→BTC, ETHUSDT→ETH, BNBUSDT→BNB
            coin = symbol.replace("USDT", "").upper() if symbol.endswith("USDT") else None
            if coin:
                self.prices[coin] = price
                self.last_update[coin] = time.time()
        except Exception:
            pass

    def _on_error(self, ws, error):
        print(f"  ⚠️ WebSocket 错误: {error}")

    def _on_close(self, ws, code, msg):
        if self._running:
            print(f"  ⚠️ WebSocket 断开(code={code})，2s后重连...")

    def get_price(self, coin="BTC"):
        """获取实时价格，数据超过5秒未更新则返回None（触发REST fallback）"""
        price = self.prices.get(coin)
        last = self.last_update.get(coin, 0)
        if price and (time.time() - last) < 5:
            return price
        return None

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()


# 全局单例
price_stream = BinancePriceStream()

def get_klines(symbol, interval, limit=10):
    """获取 K线数据"""
    url = f"{BINANCE_API}/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    try:
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # 返回格式: [open_time, open, high, low, close, volume, ...]
            return [{
                'time': int(k[0]),
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5])
            } for k in data]
    except Exception as e:
        print(f"获取K线失败: {e}")
    return []

def get_current_price(symbol):
    """获取实时价格 - 优先WebSocket(0ms)，fallback REST"""
    # 优先从 WebSocket 内存读取
    coin = symbol.replace("USDT", "").upper()
    ws_price = price_stream.get_price(coin)
    if ws_price is not None:
        return ws_price
    # WebSocket 未就绪或超时，fallback REST
    url = f"{BINANCE_API}/api/v3/ticker/price"
    try:
        resp = requests.get(url, params={"symbol": symbol}, timeout=5)
        if resp.status_code == 200:
            return float(resp.json()['price'])
    except Exception:
        pass
    return None

def get_24h_stats(symbol):
    """获取24小时统计"""
    url = f"{BINANCE_API}/api/v3/ticker/24hr"
    try:
        resp = requests.get(url, params={"symbol": symbol}, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return {
                'volume': float(data['volume']),
                'quote_volume': float(data['quoteVolume']),
                'price_change_pct': float(data['priceChangePercent'])
            }
    except:
        pass
    return None
