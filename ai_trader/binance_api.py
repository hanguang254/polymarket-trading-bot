"""
币安 API 数据采集模块
"""
import json
import requests
import threading
import time
from collections import deque
from datetime import datetime

from ai_trader.ws_ssl import get_websocket_sslopt

try:
    import websocket
    _HAS_WEBSOCKET = True
except ImportError:
    _HAS_WEBSOCKET = False

BINANCE_API = "https://api.binance.com"


def _coerce_timestamp(value):
    """把秒/毫秒/微秒时间戳转成 epoch seconds。"""
    if value is None:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e15:
        ts /= 1_000_000.0
    elif ts > 1e12:
        ts /= 1_000.0
    if ts <= 0:
        return None
    return ts


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
        self.event_timestamps = {}  # {"BTC": event_time, ...}
        self.trade_timestamps = {}  # {"BTC": trade_time, ...}
        self.update_count = {}  # {"BTC": 123, ...}
        # v14.1: 交易流动量缓冲区 — (ts, price, qty, is_buyer_maker)
        self._trade_tape = {}  # {"BTC": deque(maxlen=500), ...}
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
            received_at = time.time()
            event_ts = _coerce_timestamp(data.get("E"))
            trade_ts = _coerce_timestamp(data.get("T"))
            # 动态解析: BTCUSDT→BTC, ETHUSDT→ETH, BNBUSDT→BNB
            coin = symbol.replace("USDT", "").upper() if symbol.endswith("USDT") else None
            if coin:
                self.prices[coin] = price
                self.last_update[coin] = received_at
                self.event_timestamps[coin] = event_ts
                self.trade_timestamps[coin] = trade_ts
                self.update_count[coin] = self.update_count.get(coin, 0) + 1
                # v14.1: 积累交易流数据 — qty + is_buyer_maker
                qty = float(data.get("q", 0))
                is_buyer_maker = data.get("m", False)  # True=卖方主动, False=买方主动
                if qty > 0:
                    tape = self._trade_tape.get(coin)
                    if tape is None:
                        tape = deque(maxlen=500)
                        self._trade_tape[coin] = tape
                    tape.append((received_at, price, qty, is_buyer_maker))
        except Exception:
            pass

    def _on_error(self, ws, error):
        print(f"  ⚠️ WebSocket 错误: {error}")

    def _on_close(self, ws, code, msg):
        if self._running:
            print(f"  ⚠️ WebSocket 断开(code={code})，2s后重连...")

    def get_price(self, coin="BTC"):
        """获取实时价格，数据超过5秒未更新则返回None（触发REST fallback）"""
        snapshot = self.get_snapshot(coin)
        if snapshot and not snapshot["stale"]:
            return snapshot["price"]
        return None

    def get_snapshot(self, coin="BTC"):
        """返回价格流调试信息，包含 age/event_age/trade_age/update_count。"""
        price = self.prices.get(coin)
        last = self.last_update.get(coin, 0)
        if price is None or last <= 0:
            return None
        now = time.time()
        age_sec = max(0.0, now - last)
        event_ts = self.event_timestamps.get(coin)
        trade_ts = self.trade_timestamps.get(coin)
        event_age_ms = round(max(0.0, now - event_ts) * 1000, 1) if event_ts else None
        trade_age_ms = round(max(0.0, now - trade_ts) * 1000, 1) if trade_ts else None
        return {
            "coin": coin,
            "price": price,
            "age_ms": round(age_sec * 1000, 1),
            "stale": age_sec >= 5,
            "last_update_ts": last,
            "source_ts": event_ts,
            "source_age_ms": event_age_ms,
            "trade_ts": trade_ts,
            "trade_age_ms": trade_age_ms,
            "updates": self.update_count.get(coin, 0),
        }

    def get_price_history(self, coin="BTC", window_sec=60):
        """Return recent (ts, price) Binance trade points for spread alignment."""
        now = time.time()
        cutoff = now - float(window_sec)
        tape = self._trade_tape.get(coin)
        points = []
        if tape:
            points.extend((ts, price) for ts, price, _qty, _maker in tape if ts >= cutoff)
        latest_price = self.prices.get(coin)
        latest_ts = self.last_update.get(coin, 0)
        if latest_price is not None and latest_ts >= cutoff:
            if not points or points[-1][0] != latest_ts:
                points.append((latest_ts, latest_price))
        points.sort(key=lambda item: item[0])
        return points

    def get_tick_momentum(self, coin="BTC", window_sec=10):
        """v14.1: 计算滚动窗口内的交易流动量

        返回 {"ofi": float, "buy_vol": float, "sell_vol": float, "n_trades": int, "direction": str|None}
        ofi = (buy_vol - sell_vol) / (buy_vol + sell_vol), 范围 [-1, 1]
        direction: "UP" if ofi > 0.15, "DOWN" if ofi < -0.15, None otherwise
        """
        tape = self._trade_tape.get(coin)
        if not tape:
            return {"ofi": 0, "buy_vol": 0, "sell_vol": 0, "n_trades": 0, "direction": None}
        now = time.time()
        cutoff = now - window_sec
        buy_vol = 0.0
        sell_vol = 0.0
        n = 0
        for ts, price, qty, is_buyer_maker in tape:
            if ts < cutoff:
                continue
            n += 1
            if is_buyer_maker:
                sell_vol += qty * price  # 卖方是maker=买方主动吃单=卖压
            else:
                buy_vol += qty * price   # 买方是maker=卖方主动吃单=买压
        total = buy_vol + sell_vol
        ofi = (buy_vol - sell_vol) / total if total > 0 else 0
        direction = "UP" if ofi > 0.15 else ("DOWN" if ofi < -0.15 else None)
        return {"ofi": round(ofi, 4), "buy_vol": round(buy_vol, 2),
                "sell_vol": round(sell_vol, 2), "n_trades": n, "direction": direction}

    def get_price_delta(self, coin="BTC", window_sec=15):
        """v14.4: Return net price change over the last window_sec seconds.

        Used by Oracle Sniper to cross-validate Chainlink direction with
        an independent Binance trend signal. This is a PRICE metric
        (net first→last in window), not an order-flow metric like OFI.

        Returns:
            {
                "start_price": float,   # first trade in window
                "end_price": float,     # last trade in window
                "delta_bps": float,     # (end/start - 1) * 10000
                "n_trades": int,        # trade count in window
                "stale": bool,          # True if n_trades == 0
                "direction": "UP"/"DOWN"/"FLAT"
            }
        """
        tape = self._trade_tape.get(coin)
        empty = {
            "start_price": 0.0,
            "end_price": 0.0,
            "delta_bps": 0.0,
            "n_trades": 0,
            "stale": True,
            "direction": "FLAT",
        }
        if not tape:
            return empty
        now = time.time()
        cutoff = now - window_sec
        in_window = [(ts, price) for (ts, price, _qty, _maker) in tape if ts >= cutoff]
        if not in_window:
            return empty
        in_window.sort(key=lambda x: x[0])
        start_price = in_window[0][1]
        end_price = in_window[-1][1]
        if start_price <= 0:
            return empty
        delta_bps = (end_price / start_price - 1.0) * 10000.0
        if delta_bps > 0.01:
            direction = "UP"
        elif delta_bps < -0.01:
            direction = "DOWN"
        else:
            direction = "FLAT"
        return {
            "start_price": round(start_price, 4),
            "end_price": round(end_price, 4),
            "delta_bps": round(delta_bps, 4),
            "n_trades": len(in_window),
            "stale": False,
            "direction": direction,
        }

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

def get_price_delta(coin="BTC", window_sec=15):
    """Module-level convenience wrapper for BinancePriceStream.get_price_delta."""
    return price_stream.get_price_delta(coin, window_sec)
