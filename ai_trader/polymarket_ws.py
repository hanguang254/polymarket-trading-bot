"""
Polymarket Market Channel WebSocket — 实时 orderbook / best_bid_ask 推送

全局单例，后台线程维护连接，按 asset_id 订阅/退订。
消费方通过 get_best_bid/get_best_ask/get_book 零延迟读内存。

文档: https://docs.polymarket.com/market-data/websocket/market-channel
"""
import json
import logging
import threading
import time
from collections import deque

from ai_trader.ws_ssl import get_websocket_sslopt

logger = logging.getLogger(__name__)

try:
    import websocket
    _HAS_WEBSOCKET = True
except ImportError:
    _HAS_WEBSOCKET = False

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 9  # 文档要求每10s发PING，留1s余量
STALE_THRESHOLD = 1  # v12.8: 1s过期，WS不推送时快速降级REST拿最新价


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
        self._price_event = threading.Event()  # v12.8: 每次收到 BBA 更新时 set，供狙击线程事件驱动
        self._msg_count = 0  # v12.8 诊断：WS 收到的消息总数
        self._last_msg_log = 0  # 上次打印消息计数的时间
        # v14.1: 成交带缓冲区 — {asset_id: deque([(ts, price, side, size), ...])}
        self._trade_tape = {}

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

    # ── REST 种子（WS 订阅后立即用 REST 预读，避免初始无快照） ──

    def seed_from_rest(self, asset_ids, clob_client):
        """WS 订阅后立即用 REST 读一次 orderbook 写入内存，
        解决新市场 WS 初始无推送的问题。后续 WS 推送会自动覆盖。
        """
        if isinstance(asset_ids, str):
            asset_ids = [asset_ids]
        for token_id in asset_ids:
            # 已有新鲜数据(< 2s)则跳过（WS推送正常时不浪费REST）
            with self._lock:
                existing = self._bba.get(token_id)
            if existing and (time.time() - existing["ts"]) < 2:
                continue
            try:
                book = clob_client.get_orderbook(token_id)
                if not book:
                    continue
                raw_bids = [{"price": b.price, "size": b.size} for b in (book.bids or [])]
                raw_asks = [{"price": a.price, "size": a.size} for a in (book.asks or [])]
                sorted_bids = sorted(raw_bids, key=lambda x: float(x.get("price", 0)), reverse=True)
                sorted_asks = sorted(raw_asks, key=lambda x: float(x.get("price", 0)))
                if not sorted_bids or not sorted_asks:
                    continue
                bb = float(sorted_bids[0]["price"])
                ba = float(sorted_asks[0]["price"])
                if bb <= 0 or ba <= 0 or bb >= ba:
                    continue
                with self._lock:
                    # v12.8: 放宽写入条件 — WS 不推送时 seed 是唯一数据源
                    fresh = self._bba.get(token_id)
                    if not fresh or (time.time() - fresh["ts"]) >= 2:
                        self._bba[token_id] = {
                            "best_bid": bb,
                            "best_ask": ba,
                            "spread": round(ba - bb, 4),
                            "ts": time.time(),
                        }
                        self._books[token_id] = {
                            "bids": sorted_bids,
                            "asks": sorted_asks,
                            "ts": time.time(),
                        }
            except Exception:
                pass

    # ── 事件驱动接口 ──

    def wait_for_update(self, timeout=0.05):
        """阻塞等待 WS 推送新的 BBA 更新，或到 timeout 秒后返回。
        返回 True 表示收到新数据（age ~0ms），False 表示超时（兜底轮询）。
        狙击线程用这个替代固定 sleep，实现事件驱动扫描。"""
        updated = self._price_event.wait(timeout=timeout)
        self._price_event.clear()
        return updated

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

    def get_orderbook_imbalance(self, asset_id, depth=5):
        """v14.1: 订单簿失衡（OBI）— bid_vol / (bid_vol + ask_vol)
        返回 float [0,1]: >0.6=看涨, <0.4=看跌, None=数据不可用
        """
        bids, asks = self.get_book(asset_id)
        if not bids or not asks:
            return None
        bid_vol = sum(float(b.get("size", 0)) for b in bids[:depth])
        ask_vol = sum(float(a.get("size", 0)) for a in asks[:depth])
        total = bid_vol + ask_vol
        if total < 1:
            return None
        return round(bid_vol / total, 4)

    def get_trade_direction(self, asset_id, window_sec=10):
        """v14.1: 成交带方向 — 滚动窗口内BUY vs SELL净量
        返回 {"ofi": float, "direction": str|None, "n_trades": int}
        """
        with self._lock:
            tape = self._trade_tape.get(asset_id)
        if not tape:
            return {"ofi": 0, "direction": None, "n_trades": 0}
        now = time.time()
        cutoff = now - window_sec
        buy_vol = 0.0
        sell_vol = 0.0
        n = 0
        for ts, price, side, size in tape:
            if ts < cutoff:
                continue
            n += 1
            if side == "BUY":
                buy_vol += size
            else:
                sell_vol += size
        total = buy_vol + sell_vol
        ofi = (buy_vol - sell_vol) / total if total > 0 else 0
        direction = "UP" if ofi > 0.2 else ("DOWN" if ofi < -0.2 else None)
        return {"ofi": round(ofi, 4), "direction": direction, "n_trades": n}

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
        # 发送初始订阅（连接时用 type=market + initial_dump）
        if self._subscribed_ids:
            self._send_subscribe(list(self._subscribed_ids), initial=True)
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

    def _send_subscribe(self, asset_ids, initial=False):
        """发送订阅消息

        v12.8: 区分初始订阅（on_open）和动态追加（已连接后）
        初始: {"type": "market", "initial_dump": true} — 获取完整快照
        动态: {"assets_ids": [...], "operation": "subscribe"} — 追加订阅
        """
        try:
            if initial:
                # 连接建立时的初始订阅
                msg = json.dumps({
                    "assets_ids": asset_ids,
                    "type": "market",
                    "initial_dump": True,
                    "custom_feature_enabled": True,
                })
            else:
                # 动态追加订阅（已连接状态）
                msg = json.dumps({
                    "assets_ids": asset_ids,
                    "operation": "subscribe",
                    "custom_feature_enabled": True,
                })
            self._ws.send(msg)
            id_preview = [aid[:12] + ".." for aid in asset_ids[:4]]
            mode = "initial" if initial else "dynamic"
            logger.info(f"  📡 Polymarket WS 订阅({mode}) {len(asset_ids)} 个 asset: {id_preview}")
        except Exception as e:
            logger.warning(f"  ⚠️ Polymarket WS 订阅失败: {e}")

    def _on_message(self, ws, message):
        if message == "PONG":
            return
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        # 可能是单条或数组
        events = data if isinstance(data, list) else [data]

        # v12.8 诊断：每10秒打印一次WS消息统计，确认连接是否活跃
        self._msg_count += len(events)
        now_t = time.time()
        if now_t - self._last_msg_log >= 10:
            bba_keys = list(self._bba.keys())
            bba_preview = [k[:12] + ".." for k in bba_keys]
            sub_preview = [k[:12] + ".." for k in list(self._subscribed_ids)[:8]]
            logger.info(f"  [WS诊断] 总消息={self._msg_count} | _bba={len(bba_keys)}个 {bba_preview} | _sub={len(self._subscribed_ids)}个 {sub_preview}")
            self._last_msg_log = now_t

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
        self._price_event.set()

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
        self._price_event.set()

    def _handle_price_change(self, event):
        """price_change 事件包含 best_bid/best_ask 增量更新"""
        changes = event.get("price_changes", [])
        _signaled = False
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
                _signaled = True
        if _signaled:
            self._price_event.set()

    def _handle_last_trade(self, event):
        """v14.1: 记录成交带 — price/side/size 用于方向判断"""
        asset_id = event.get("asset_id", "")
        if not asset_id:
            return
        price = _safe_float(event.get("price"))
        side = event.get("side", "")  # "BUY" or "SELL"
        size = _safe_float(event.get("size")) or 0
        if price is None or not side:
            return
        with self._lock:
            tape = self._trade_tape.get(asset_id)
            if tape is None:
                tape = deque(maxlen=100)
                self._trade_tape[asset_id] = tape
            tape.append((time.time(), price, side, size))

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
