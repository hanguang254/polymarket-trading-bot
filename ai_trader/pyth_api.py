"""
Pyth Network 链上价格源 - 替代 Binance 作为持仓监控的主数据源
使用 Hermes v2 API (SSE stream + REST fallback)，无需 API key
"""
import json
import threading
import time

import requests

HERMES_BASE = "https://hermes.pyth.network"

# Pyth 官方 price feed IDs（从 coins.py 动态加载）
def _load_pyth_feeds():
    try:
        from ai_trader.coins import get_coins_config
        return {coin: cfg["pyth_feed"] for coin, cfg in get_coins_config().items() if cfg.get("pyth_feed")}
    except Exception:
        return {
            "BTC": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
            "ETH": "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
        }

PYTH_FEED_IDS = _load_pyth_feeds()


class PythPriceStream:
    """SSE stream 持续接收 Pyth 链上价格，get_price() 零延迟读内存"""
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

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._connect, daemon=True)
        self._thread.start()

    def _connect(self):
        ids_params = "&".join(f"ids[]={fid}" for fid in PYTH_FEED_IDS.values())
        url = f"{HERMES_BASE}/v2/updates/price/stream?{ids_params}&parsed=true"

        while self._running:
            try:
                with requests.get(url, stream=True, timeout=30) as resp:
                    if resp.status_code == 200:
                        print("  ✅ Pyth SSE 已连接 (链上价格流)")
                    buffer = ""
                    for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
                        if not self._running:
                            break
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if line.startswith("data:"):
                                self._parse_sse_data(line[5:].strip())
            except Exception as e:
                if self._running:
                    print(f"  ⚠️ Pyth SSE 连接异常: {e}")
            if self._running:
                time.sleep(2)

    def _parse_sse_data(self, raw):
        try:
            data = json.loads(raw)
            for item in data.get("parsed", []):
                feed_id = item["id"]
                coin = next((c for c, fid in PYTH_FEED_IDS.items() if fid == feed_id), None)
                if coin is None:
                    continue
                price_data = item["price"]
                expo = price_data["expo"]
                price = int(price_data["price"]) * (10 ** expo)
                self.prices[coin] = price
                self.last_update[coin] = time.time()
        except Exception:
            pass

    def get_price(self, coin="BTC"):
        """获取链上实时价格，数据超过5秒未更新则返回None"""
        price = self.prices.get(coin)
        last = self.last_update.get(coin, 0)
        if price and (time.time() - last) < 5:
            return price
        return None

    def stop(self):
        self._running = False


# 全局单例
pyth_stream = PythPriceStream()


def get_pyth_price(coin="BTC"):
    """获取 Pyth 链上价格 - 优先 SSE stream(0ms)，fallback REST"""
    # 优先从 SSE 内存读取
    price = pyth_stream.get_price(coin)
    if price is not None:
        return price
    # SSE 未就绪或超时，fallback REST
    feed_id = PYTH_FEED_IDS.get(coin)
    if not feed_id:
        return None
    try:
        resp = requests.get(
            f"{HERMES_BASE}/v2/updates/price/latest",
            params={"ids[]": feed_id, "parsed": "true"},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("parsed", []):
                price_data = item["price"]
                expo = price_data["expo"]
                return int(price_data["price"]) * (10 ** expo)
    except Exception:
        pass
    return None
