"""
Polymarket CLOB SDK 客户端封装 — 替代 CLI subprocess 调用
全局单例，启动时初始化一次，全程复用
"""
import os
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, MarketOrderArgs, OrderType,
    BalanceAllowanceParams, AssetType,
    PartialCreateOrderOptions,
)
from py_clob_client.order_builder.constants import BUY, SELL

logger = logging.getLogger(__name__)

# ── 全局单例 ──
_client: ClobClient = None
_client_lock = threading.Lock()  # httpx HTTP/2不线程安全，所有SDK HTTP调用需加锁
_executor = ThreadPoolExecutor(max_workers=4)

# ── 订单簿缓存（2秒TTL，消除同一分析周期内的重复HTTP请求） ──
_book_cache = {}        # token_id -> (book, timestamp)
_book_cache_lock = threading.Lock()
BOOK_CACHE_TTL = 2.0    # 秒


def init_client():
    """启动时调用一次，初始化 ClobClient + API 凭证 + 预热"""
    global _client
    private_key = os.environ.get("PRIVATE_KEY", "")
    sig_type = int(os.environ.get("CLOB_SIGNATURE_TYPE", "0"))
    # 官方文档：0=EOA；1=POLY_PROXY（Magic/email）；2=GNOSIS_SAFE（浏览器钱包/嵌入式钱包）。
    # 对官网账户来说，1 和 2 都应使用官网显示的 proxy wallet 作为 funder。
    funder = os.environ.get("PROXY_WALLET", "") if sig_type in (1, 2) else os.environ.get("EOA_WALLET", "")

    _client = ClobClient(
        "https://clob.polymarket.com",
        key=private_key,
        chain_id=137,
        signature_type=sig_type,
        funder=funder,
    )
    creds = _client.create_or_derive_api_creds()
    _client.set_api_creds(creds)
    logger.info(f"✅ CLOB SDK 初始化完成 | funder={funder[:10]}...")

    # Initialize User WS with same credentials
    try:
        from ai_trader.user_ws import user_trade_stream
        user_trade_stream.init(creds.api_key, creds.api_secret, creds.api_passphrase)
    except Exception as e:
        logger.warning(f"User WS init skipped: {e}")

    # 缩短 httpx 超时（默认5s太长，止损时每秒都在亏钱）
    import httpx
    from py_clob_client.http_helpers import helpers as _h
    fok_timeout = float(os.environ.get("FOK_TIMEOUT", "3"))
    _h._http_client = httpx.Client(http2=True, timeout=httpx.Timeout(fok_timeout))
    logger.info(f"⏱️ HTTP超时设置为 {fok_timeout}s")

    # 预热：用假单预加载 coincurve 签名库 + TLS 连接池
    # 原理：首次签名+HTTPS请求很慢（~200ms），预热后复用连接只需 ~26ms
    _warmup()

    # 启动连接保活守护线程：每30s发GET防止HTTP/2连接超时断开
    _start_keepalive()


def _start_keepalive():
    """后台守护线程，定期 ping 保持 HTTP/2 连接活跃"""
    def _keepalive_loop():
        while True:
            time.sleep(30)
            try:
                # 不阻塞交易路径：下单/查簿正在占用锁时，本轮 keepalive 直接跳过
                if _client_lock.acquire(blocking=False):
                    try:
                        _client.get_server_time()
                    finally:
                        _client_lock.release()
            except Exception:
                pass
    t = threading.Thread(target=_keepalive_loop, daemon=True)
    t.start()
    logger.info("🔗 HTTP/2 连接保活已启动 (30s间隔)")


def get_client() -> ClobClient:
    """获取已初始化的客户端（调试/高级用途）"""
    if _client is None:
        raise RuntimeError("CLOB client 未初始化，请先调用 init_client()")
    return _client


# ── 预缓存（消除下单时的额外HTTP查询） ──

_token_cache = {}  # token_id -> {"neg_risk": bool, "fee_rate_bps": int, "tick_size": str}


def precache_token(token_id):
    """预缓存 token 的 neg_risk/fee_rate/tick_size，避免下单时额外HTTP查询

    调用时机：获取到 token_id 后立即调用（在分析阶段），
    这样下单时 create_order 内部的缓存已就绪，延迟从 ~3.5s 降至 ~500ms。
    所有SDK HTTP调用需加 _client_lock，httpx HTTP/2连接不线程安全。
    """
    token_id = str(token_id)
    if token_id in _token_cache:
        return _token_cache[token_id]

    t0 = time.time()
    try:
        with _client_lock:
            neg_risk = _client.get_neg_risk(token_id)
            fee_rate = _client.get_fee_rate_bps(token_id)
            tick_size = _client.get_tick_size(token_id)

        _token_cache[token_id] = {
            "neg_risk": neg_risk,
            "fee_rate_bps": fee_rate,
            "tick_size": tick_size,
        }
        elapsed = (time.time() - t0) * 1000
        logger.info(f"📦 预缓存token: neg_risk={neg_risk} fee={fee_rate}bps tick={tick_size} | {elapsed:.0f}ms")
        return _token_cache[token_id]
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.warning(f"⚠️ 预缓存失败: {e} | {elapsed:.0f}ms")
        return None


def precache_tokens(token_ids):
    """预缓存多个token（串行，避免HTTP/2连接竞争）"""
    t0 = time.time()
    results = {}
    for tid in token_ids:
        results[tid] = precache_token(tid)
    elapsed = (time.time() - t0) * 1000
    logger.info(f"📦 并行预缓存{len(token_ids)}个token完成 | {elapsed:.0f}ms")
    return results


def get_token_metadata(token_id, refresh=False):
    """读取 token 元数据；默认优先返回缓存。"""
    token_id = str(token_id)
    if not refresh and token_id in _token_cache:
        return dict(_token_cache[token_id])
    meta = precache_token(token_id)
    return dict(meta) if meta else None


def get_fee_rate_bps(token_id, refresh=False):
    """返回 token 当前 feeRateBps；fee-free 市场返回 0。"""
    meta = get_token_metadata(token_id, refresh=refresh)
    if not meta:
        return 0
    try:
        return int(meta.get("fee_rate_bps") or 0)
    except (TypeError, ValueError):
        return 0


def _warmup():
    """预热签名库 + TLS/HTTP2连接池

    原理：首次 EIP-712 签名需加载 coincurve/secp256k1（~150ms），
    首次 HTTPS 请求需 TLS 握手 + HTTP/2 协商（~300ms）。
    预热后复用连接池，后续下单只剩纯网络延迟（~25-350ms 取决于物理距离）。

    步骤：
    1. 假签名：create_order 触发 coincurve 加载（纯本地，0 HTTP）
    2. GET 预热：get_server_time 建立 TLS + HTTP/2 连接
    3. POST 预热：假 post_order 确保 POST 路径的 HTTP/2 stream 就绪
    """
    FAKE_TOKEN = "0"
    t0 = time.time()
    try:
        # 1. 预热签名库（纯本地，0 HTTP 请求）
        #    SDK 的 create_order 内部 __resolve_tick_size / get_neg_risk / get_fee_rate_bps
        #    即使传了 options 也会查询。解决：直接写入 SDK 内部缓存，绕过 HTTP。
        _client._ClobClient__tick_sizes[FAKE_TOKEN] = "0.01"
        _client._ClobClient__tick_size_timestamps[FAKE_TOKEN] = time.monotonic()
        _client._ClobClient__neg_risk[FAKE_TOKEN] = False
        _client._ClobClient__fee_rates[FAKE_TOKEN] = 0

        _client.create_order(
            OrderArgs(token_id=FAKE_TOKEN, price=0.01, size=1.0, side=BUY),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=True),
        )
        t1 = time.time()
        sign_ms = (t1 - t0) * 1000

        # 2. GET 预热：建立 TLS 连接 + HTTP/2 协商
        _client.get_server_time()
        t2 = time.time()
        tls_ms = (t2 - t1) * 1000

        # 3. POST 预热：用假单发真实 POST 到 /order endpoint
        #    服务器会拒绝（假token），但 HTTP/2 POST stream + API 鉴权头序列化已完成
        try:
            fake_order = _client.create_order(
                OrderArgs(token_id=FAKE_TOKEN, price=0.01, size=1.0, side=BUY),
                options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=True),
            )
            _client.post_order(fake_order, OrderType.GTC)
        except Exception:
            pass  # 假token必定被拒绝，但 POST 连接已建立
        t3 = time.time()
        post_ms = (t3 - t2) * 1000

        logger.info(
            f"🔥 预热完成: 签名={sign_ms:.0f}ms TLS={tls_ms:.0f}ms "
            f"POST={post_ms:.0f}ms 总计={(t3-t0)*1000:.0f}ms"
        )
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.info(f"🔥 预热完成(部分): {elapsed:.0f}ms | {e}")


# ── 下单 ──

def place_order(token_id, side, price, size, order_type=OrderType.GTC, expiration=0):
    """GTC/GTD limit order

    Args:
        expiration: Unix timestamp for GTD orders (0 = no expiration / GTC).
                    Note: Polymarket has a 60s security threshold, so set
                    expiration = now + 60 + desired_lifetime_seconds.
    Returns:
        dict: {success, matched, order_id, status, making, taking, raw}
    """
    t0 = time.time()
    token_id = str(token_id)
    cached = _token_cache.get(token_id, {})
    neg_risk = cached.get("neg_risk", None)
    try:
        # 1. 签名（纯本地计算，不加锁）
        order = _client.create_order(
            OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=side,
                expiration=int(expiration),
            ),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk),
        )
        t_sign = time.time()
        # 2. 发送（网络IO，加锁）— 425 Too Early 自动重试
        resp = None
        for attempt in range(3):
            try:
                with _client_lock:
                    resp = _client.post_order(order, order_type)
                break
            except Exception as ex:
                if "425" in str(ex) or "not ready" in str(ex).lower():
                    logger.warning(f"⏳ 425 Too Early，{0.5*(attempt+1):.1f}s后重试 ({attempt+1}/3)")
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        if resp is None:
            raise Exception("425 Too Early: 重试3次仍未就绪")
        elapsed = (time.time() - t0) * 1000
        sign_ms = (t_sign - t0) * 1000
        net_ms = elapsed - sign_ms
        invalidate_book_cache(token_id)
        result = _parse_response(resp)
        result["elapsed_ms"] = round(elapsed, 1)
        logger.info(f"📡 SDK下单 {side} {size}@{price} | {result['status']} | sign={sign_ms:.0f}ms net={net_ms:.0f}ms total={elapsed:.0f}ms")
        return result
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.error(f"❌ SDK下单异常: {e} | {elapsed:.0f}ms")
        return {"success": False, "matched": False, "status": "ERROR",
                "error": str(e), "elapsed_ms": round(elapsed, 1),
                "making": 0, "taking": 0, "order_id": None, "raw": str(e)}


def place_fok_order(token_id, side, price, size):
    """FOK 即时成交单 — 用于入场/止损/止盈

    拆分 create_market_order（本地签名）+ post_order（网络），签名不加锁。

    Returns:
        dict: 同 place_order
    """
    t0 = time.time()
    token_id = str(token_id)
    cached = _token_cache.get(token_id, {})
    neg_risk = cached.get("neg_risk", None)
    try:
        # 1. 签名（纯本地计算，不加锁）
        amount = float(price) * float(size) if side == BUY else float(size)
        order = _client.create_market_order(
            MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=side,
                price=float(price),
                fee_rate_bps=0,
                nonce=0,
                taker="0x0000000000000000000000000000000000000000",
                order_type=OrderType.FOK,
            ),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk),
        )
        t_sign = time.time()
        # 2. 发送（网络IO，加锁）— 425 Too Early 自动重试
        resp = None
        for attempt in range(3):
            try:
                with _client_lock:
                    resp = _client.post_order(order, OrderType.FOK)
                break
            except Exception as ex:
                if "425" in str(ex) or "not ready" in str(ex).lower():
                    logger.warning(f"⏳ FOK 425 Too Early，{0.5*(attempt+1):.1f}s后重试 ({attempt+1}/3)")
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        if resp is None:
            raise Exception("425 Too Early: FOK重试3次仍未就绪")
        elapsed = (time.time() - t0) * 1000
        sign_ms = (t_sign - t0) * 1000
        net_ms = elapsed - sign_ms
        invalidate_book_cache(token_id)
        result = _parse_response(resp)
        result["elapsed_ms"] = round(elapsed, 1)
        logger.info(f"⚡ FOK {side} {size}@{price} | {result['status']} | sign={sign_ms:.0f}ms net={net_ms:.0f}ms total={elapsed:.0f}ms")
        return result
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.error(f"❌ FOK异常: {e} | {elapsed:.0f}ms")
        return {"success": False, "matched": False, "status": "ERROR",
                "error": str(e), "elapsed_ms": round(elapsed, 1),
                "making": 0, "taking": 0, "order_id": None, "raw": str(e)}


# ── 取消 ──

def place_fak_order(token_id, side, price, size):
    """FAK (Fill-And-Kill) — partial fill OK, remainder cancelled.

    Better than FOK for stop-loss: at least sells what's available
    instead of all-or-nothing rejection.
    """
    t0 = time.time()
    token_id = str(token_id)
    cached = _token_cache.get(token_id, {})
    neg_risk = cached.get("neg_risk", None)
    try:
        amount = float(price) * float(size) if side == BUY else float(size)
        order = _client.create_market_order(
            MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=side,
                price=float(price),
                fee_rate_bps=0,
                nonce=0,
                taker="0x0000000000000000000000000000000000000000",
                order_type=OrderType.FAK,
            ),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk),
        )
        t_sign = time.time()
        resp = None
        for attempt in range(3):
            try:
                with _client_lock:
                    resp = _client.post_order(order, OrderType.FAK)
                break
            except Exception as ex:
                if "425" in str(ex) or "not ready" in str(ex).lower():
                    logger.warning(f"⏳ FAK 425 Too Early ({attempt+1}/3)")
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        if resp is None:
            raise Exception("425 Too Early: FAK retries exhausted")
        elapsed = (time.time() - t0) * 1000
        sign_ms = (t_sign - t0) * 1000
        net_ms = elapsed - sign_ms
        invalidate_book_cache(token_id)
        result = _parse_response(resp)
        result["elapsed_ms"] = round(elapsed, 1)
        logger.info(f"⚡ FAK {side} {size}@{price} | {result['status']} | sign={sign_ms:.0f}ms net={net_ms:.0f}ms total={elapsed:.0f}ms")
        return result
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.error(f"❌ FAK error: {e} | {elapsed:.0f}ms")
        return {"success": False, "matched": False, "status": "ERROR",
                "error": str(e), "elapsed_ms": round(elapsed, 1),
                "making": 0, "taking": 0, "order_id": None, "raw": str(e)}


def cancel_order(order_id):
    """Cancel a single order by order_id"""
    try:
        with _client_lock:
            resp = _client.cancel(order_id=str(order_id))
        return True
    except Exception as e:
        logger.warning(f"Cancel order failed ({str(order_id)[:12]}): {e}")
        return False


def cancel_orders_batch(order_ids):
    """Cancel multiple orders in one API call (max 3000). Returns (canceled, not_canceled)."""
    if not order_ids:
        return [], {}
    ids = [str(oid) for oid in order_ids if oid]
    if not ids:
        return [], {}
    try:
        with _client_lock:
            resp = _client.cancel_orders(ids)
        canceled = resp.get("canceled", []) if isinstance(resp, dict) else []
        not_canceled = resp.get("not_canceled", {}) if isinstance(resp, dict) else {}
        if not_canceled:
            logger.warning(f"Batch cancel: {len(canceled)} ok, {len(not_canceled)} failed: {list(not_canceled.keys())[:3]}")
        return canceled, not_canceled
    except Exception as e:
        logger.warning(f"Batch cancel failed ({len(ids)} orders): {e}")
        return [], {}


def cancel_all(token_id=None):
    """Cancel all orders, optionally filtered by token_id"""
    try:
        with _client_lock:
            if token_id:
                resp = _client.cancel_market_orders(asset_id=str(token_id))
            else:
                resp = _client.cancel_all()
        return True
    except Exception as e:
        logger.warning(f"Cancel orders failed: {e}")
        return False


# ── 余额 ──

def get_balance():
    """获取 USDC 余额（collateral），返回美元单位"""
    try:
        with _client_lock:
            resp = _client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    token_id="",
                )
            )
        # API 返回原子单位（USDC 6位小数），需除以 1e6 转美元
        if isinstance(resp, dict):
            return float(resp.get("balance", 0)) / 1e6
        return float(getattr(resp, "balance", 0)) / 1e6
    except Exception as e:
        logger.warning(f"获取余额失败: {e}")
        return 0


def get_token_balance(token_id):
    """获取 conditional token 余额（份数单位）"""
    try:
        with _client_lock:
            resp = _client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=str(token_id),
                )
            )
        # API 返回原子单位（6位小数），需除以 1e6 转份数
        if isinstance(resp, dict):
            return float(resp.get("balance", 0)) / 1e6
        return float(getattr(resp, "balance", 0)) / 1e6
    except Exception as e:
        logger.warning(f"获取token余额失败: {e}")
        return None


def update_token_allowance(token_id):
    """刷新 conditional token 的 allowance（重新授权交易所扣token）"""
    try:
        with _client_lock:
            resp = _client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=str(token_id),
                )
            )
        logger.info(f"🔓 更新token allowance: {resp}")
        return True
    except Exception as e:
        logger.warning(f"更新token allowance失败: {e}")
        return False


# ── 市场数据 ──

def get_orderbook(token_id, max_age=None):
    """获取订单簿（2秒TTL缓存，同一分析周期内避免重复HTTP）

    Args:
        token_id: 代币ID
        max_age: 缓存最大年龄（秒），None=使用默认TTL，0=强制刷新
    """
    token_id = str(token_id)
    ttl = max_age if max_age is not None else BOOK_CACHE_TTL

    # 检查缓存
    if ttl > 0:
        with _book_cache_lock:
            if token_id in _book_cache:
                book, ts = _book_cache[token_id]
                if time.time() - ts < ttl:
                    return book

    # 缓存未命中或过期，发起HTTP请求
    try:
        with _client_lock:
            book = _client.get_order_book(token_id)
        with _book_cache_lock:
            _book_cache[token_id] = (book, time.time())
        return book
    except Exception as e:
        logger.warning(f"获取订单簿失败: {e}")
        return None


def invalidate_book_cache(token_id=None):
    """清除订单簿缓存（下单/平仓后调用，确保后续获取新鲜数据）"""
    with _book_cache_lock:
        if token_id:
            _book_cache.pop(str(token_id), None)
        else:
            _book_cache.clear()


def get_midpoint(token_id):
    """获取中间价"""
    try:
        with _client_lock:
            resp = _client.get_midpoint(str(token_id))
        if isinstance(resp, dict):
            return float(resp.get("mid", 0))
        return float(resp)
    except Exception:
        return None


def get_last_trade_price(token_id):
    """获取最近成交价"""
    try:
        with _client_lock:
            resp = _client.get_last_trade_price(str(token_id))
        if isinstance(resp, dict):
            return float(resp.get("price", 0))
        return float(resp)
    except Exception:
        return None


# ── 订单查询 ──

def get_order(order_id):
    """查询单个订单状态（用于挂单对账）"""
    try:
        with _client_lock:
            return _client.get_order(str(order_id))
    except Exception as e:
        logger.warning(f"查询订单失败: {e}")
        return None


def get_orders(asset_id=None):
    """查询所有订单（可按 asset_id 过滤）"""
    try:
        from py_clob_client.clob_types import OpenOrderParams
        params = OpenOrderParams(asset_id=str(asset_id)) if asset_id else None
        with _client_lock:
            return _client.get_orders(params)
    except Exception as e:
        logger.warning(f"查询订单列表失败: {e}")
        return []


# ── 内部工具 ──

def _parse_response(resp):
    """解析 SDK 响应为统一格式"""
    if resp is None:
        return {"success": False, "matched": False, "status": "NO_RESPONSE",
                "making": 0, "taking": 0, "order_id": None, "raw": ""}

    raw = resp if isinstance(resp, dict) else (resp.__dict__ if hasattr(resp, "__dict__") else str(resp))

    # SDK 返回格式可能是 dict 或对象
    if isinstance(raw, dict):
        status = raw.get("status", raw.get("orderStatus", ""))
        order_id = raw.get("orderID", raw.get("order_id", raw.get("id", "")))
        # API 返回的 status 可能是小写 "matched" 或大写 "MATCHED"
        status_upper = status.upper() if isinstance(status, str) else ""
        matched = status_upper in ("MATCHED", "FILLED")
        making = float(raw.get("making", raw.get("makingAmount", raw.get("makerAmount", 0))) or 0)
        taking = float(raw.get("taking", raw.get("takingAmount", raw.get("takerAmount", 0))) or 0)
    else:
        status = str(getattr(raw, "status", ""))
        order_id = str(getattr(raw, "orderID", getattr(raw, "order_id", "")))
        status_upper = status.upper()
        matched = status_upper in ("MATCHED", "FILLED")
        making = float(getattr(raw, "making", 0) or 0)
        taking = float(getattr(raw, "taking", 0) or 0)

    success = matched or status.upper() in ("MATCHED", "FILLED", "LIVE")

    return {
        "success": success,
        "matched": matched,
        "status": status,
        "order_id": order_id,
        "making": making,
        "taking": taking,
        "raw": str(raw)[:500],
    }
