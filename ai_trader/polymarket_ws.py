"""
Polymarket Market Channel WebSocket — 实时 orderbook / best_bid_ask 推送

全局单例，后台线程维护连接，按 asset_id 订阅/退订。
消费方通过 get_best_bid/get_best_ask/get_book 零延迟读内存。

文档: https://docs.polymarket.com/market-data/websocket/market-channel
"""
import json
import threading
import time

from ai_trader.ws_ssl import get_websocket_sslopt

try:
    import websocket
    _HAS_WEBSOCKET = True
except ImportError:
    _HAS_WEBSOCKET = False

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 9  # 文档要求每10s发PING，留1s余量
STALE_THRESHOLD = 15  # 超过15s无更新视为过期（5分钟市场，orderbook变动频繁）


class PolymarketOrderbookStream:
    """后台 WebSocket 持续接收 Polymarket orderbook 数据"""
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
        # {asset_id: {"best_bid": float, "best_ask": float, "ts": float}}
        self._bba = {}
        # {asset_id: {"bids": [...], "asks": [...], "ts": float}}
        self._books = {}
        self._lock = threading.Lock()
        self._ws = None
        self._running = False
        self._thread = None
        self._subscribed_ids = set()  # 当前已订阅的 asset_ids
        self._ping_thread = None
        self._ready = False  # start() 标记就绪，subscribe() 触发连接
        self._reconnect_delay = 2  # 指数退避: 2→4→8→...→30s
        self._connected_at = 0  # 连接建立时间（用于判断是否稳定）

    def start(self):
        """标记就绪，实际连接延迟到第一次 subscribe() 时建立
        （Polymarket WS 要求连接后立即发送订阅消息，否则服务器会断开）
        """
        if not _HAS_WEBSOCKET:
            print("  ⚠️ websocket-client 未安装，Polymarket WS 不可用")
            return
        self._ready = True
        print("  📡 Polymarket WS 就绪 (等待首次订阅后建立连接)")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    # ── 订阅管理 ──

    def subscribe(self, asset_ids):
        """订阅一组 asset_ids — 首次调用时自动建立 WS 连接"""
        if isinstance(asset_ids, str):
            asset_ids = [asset_ids]
        new_ids = [aid for aid in asset_ids if aid not in self._subscribed_ids]
        if not new_ids:
            return
        self._subscribed_ids.update(new_ids)
        # 首次订阅时才真正建立连接（确保 on_open 有 asset_ids 可发）
        if not self._running and getattr(self, "_ready", False):
            self._running = True
            self._thread = threading.Thread(target=self._connect_loop, daemon=True)
            self._thread.start()
            return  # on_open 会发送 _subscribed_ids
        # WS 已连接，动态追加订阅
        if self._ws and self._running:
            self._send_subscribe(new_ids)

    def unsubscribe(self, asset_ids):
        """退订一组 asset_ids"""
        if isinstance(asset_ids, str):
            asset_ids = [asset_ids]
        remove_ids = [aid for aid in asset_ids if aid in self._subscribed_ids]
        if not remove_ids:
            return
        self._subscribed_ids -= set(remove_ids)
        # 清理内存
        with self._lock:
            for aid in remove_ids:
                self._bba.pop(aid, None)
                self._books.pop(aid, None)
        # 发送退订
        if self._ws and self._running:
            try:
                msg = json.dumps({
                    "assets_ids": remove_ids,
                    "operation": "unsubscribe",
                })
                self._ws.send(msg)
            except Exception:
                pass

    # ── 数据读取（零延迟） ──

    def get_best_bid(self, asset_id):
        """获取实时 best_bid，过期返回 None"""
        with self._lock:
            data = self._bba.get(asset_id)
        if data and (time.time() - data["ts"]) < STALE_THRESHOLD:
            return data.get("best_bid")
        return None

    def get_best_ask(self, asset_id):
        """获取实时 best_ask，过期返回 None"""
        with self._lock:
            data = self._bba.get(asset_id)
        if data and (time.time() - data["ts"]) < STALE_THRESHOLD:
            return data.get("best_ask")
        return None

    def get_best_bid_ask(self, asset_id):
        """获取 (best_bid, best_ask)，过期返回 (None, None)"""
        with self._lock:
            data = self._bba.get(asset_id)
        if data and (time.time() - data["ts"]) < STALE_THRESHOLD:
            return data.get("best_bid"), data.get("best_ask")
        return None, None

    def get_best_bid_ask_snapshot(self, asset_id):
        """获取 best_bid/best_ask + age_ms，过期返回 None"""
        with self._lock:
            data = self._bba.get(asset_id)
        if not data:
            return None
        age_sec = time.time() - data["ts"]
        if age_sec >= STALE_THRESHOLD:
            return None
        return {
            "best_bid": data.get("best_bid"),
            "best_ask": data.get("best_ask"),
            "spread": data.get("spread"),
            "age_ms": round(age_sec * 1000, 1),
        }

    def get_book(self, asset_id):
        """获取完整 orderbook snapshot，返回 (bids, asks) 或 (None, None)
        bids: [{"price": "0.48", "size": "30"}, ...] 降序
        asks: [{"price": "0.52", "size": "25"}, ...] 升序
        """
        with self._lock:
            data = self._books.get(asset_id)
        if data and (time.time() - data["ts"]) < STALE_THRESHOLD:
            return data.get("bids", []), data.get("asks", [])
        return None, None

    def get_book_snapshot(self, asset_id):
        """获取完整订单簿 + age_ms，过期返回 None"""
        with self._lock:
            data = self._books.get(asset_id)
        if not data:
            return None
        age_sec = time.time() - data["ts"]
        if age_sec >= STALE_THRESHOLD:
            return None
        return {
            "bids": data.get("bids", []),
            "asks": data.get("asks", []),
            "age_ms": round(age_sec * 1000, 1),
        }

    # ── WebSocket 内部 ──

    def _connect_loop(self):
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                # 不使用 websocket 库的 ping，我们自己发 PING 文本
                self._ws.run_forever(
                    sslopt=get_websocket_sslopt(),
                    ping_interval=0,
                )
            except Exception as e:
                print(f"  ⚠️ Polymarket WS 连接异常: {e}")
            if self._running:
                time.sleep(self._reconnect_delay)
                # 指数退避: 2→4→8→16→30s（避免重连风暴触发服务器限速）
                self._reconnect_delay = min(self._reconnect_delay * 2, 30)

    def _on_open(self, ws):
        self._connected_at = time.time()
        # 连接成功，重置退避延迟
        self._reconnect_delay = 2
        print("  ✅ Polymarket WS 已连接 (实时orderbook)")
        # 发送初始订阅
        if self._subscribed_ids:
            self._send_subscribe(list(self._subscribed_ids))
        # 启动 PING 心跳线程
        if self._ping_thread is None or not self._ping_thread.is_alive():
            self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
            self._ping_thread.start()

    def _ping_loop(self):
        """每9秒发送 PING 保持连接"""
        while self._running and self._ws:
            try:
                self._ws.send("PING")
            except Exception:
                break
            time.sleep(PING_INTERVAL)

    def _send_subscribe(self, asset_ids):
        """发送订阅消息"""
        try:
            msg = json.dumps({
                "assets_ids": asset_ids,
                "type": "market",
                "custom_feature_enabled": True,
            })
            self._ws.send(msg)
            print(f"  📡 Polymarket WS 订阅 {len(asset_ids)} 个 asset")
        except Exception as e:
            print(f"  ⚠️ Polymarket WS 订阅失败: {e}")

    def _on_message(self, ws, message):
        if message == "PONG":
            return
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        # 可能是单条或数组
        events = data if isinstance(data, list) else [data]
        for event in events:
            event_type = event.get("event_type", "")
            if event_type == "best_bid_ask":
                self._handle_best_bid_ask(event)
            elif event_type == "book":
                self._handle_book(event)
            elif event_type == "price_change":
                self._handle_price_change(event)
            elif event_type == "last_trade_price":
                self._handle_last_trade(event)

    def _handle_best_bid_ask(self, event):
        asset_id = event.get("asset_id", "")
        if not asset_id:
            return
        best_bid = _safe_float(event.get("best_bid"))
        best_ask = _safe_float(event.get("best_ask"))
        with self._lock:
            self._bba[asset_id] = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": _safe_float(event.get("spread")),
                "ts": time.time(),
            }

    def _handle_book(self, event):
        asset_id = event.get("asset_id", "")
        if not asset_id:
            return
        bids = event.get("bids", [])
        asks = event.get("asks", [])
        # 规范化排序
        sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
        sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 0)))
        with self._lock:
            self._books[asset_id] = {
                "bids": sorted_bids,
                "asks": sorted_asks,
                "ts": time.time(),
            }
            # book 事件也更新 best_bid_ask
            bb = float(sorted_bids[0]["price"]) if sorted_bids else None
            ba = float(sorted_asks[0]["price"]) if sorted_asks else None
            if bb is not None or ba is not None:
                self._bba[asset_id] = {
                    "best_bid": bb,
                    "best_ask": ba,
                    "spread": round(ba - bb, 4) if bb and ba else None,
                    "ts": time.time(),
                }

    def _handle_price_change(self, event):
        """price_change 事件包含 best_bid/best_ask 增量更新"""
        changes = event.get("price_changes", [])
        for change in changes:
            asset_id = change.get("asset_id", "")
            if not asset_id:
                continue
            best_bid = _safe_float(change.get("best_bid"))
            best_ask = _safe_float(change.get("best_ask"))
            if best_bid is not None or best_ask is not None:
                with self._lock:
                    existing = self._bba.get(asset_id, {})
                    self._bba[asset_id] = {
                        "best_bid": best_bid if best_bid is not None else existing.get("best_bid"),
                        "best_ask": best_ask if best_ask is not None else existing.get("best_ask"),
                        "spread": None,
                        "ts": time.time(),
                    }

    def _handle_last_trade(self, event):
        # 暂不使用，预留扩展
        pass

    def _on_error(self, ws, error):
        # 频繁断连时减少日志噪音（连接稳定>10s才打印错误）
        if time.time() - self._connected_at > 10:
            print(f"  ⚠️ Polymarket WS 错误: {error}")

    def _on_close(self, ws, code, msg):
        if self._running:
            print(f"  ⚠️ Polymarket WS 断开(code={code})，{self._reconnect_delay}s后重连...")


def _safe_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# 全局单例
poly_ws = PolymarketOrderbookStream()
