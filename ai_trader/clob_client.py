"""
Polymarket CLOB SDK 客户端封装 — 替代 CLI subprocess 调用
全局单例，启动时初始化一次，全程复用
"""
import os
import time
import logging

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


def init_client():
    """启动时调用一次，初始化 ClobClient + API 凭证 + 预热"""
    global _client
    private_key = os.environ.get("PRIVATE_KEY", "")
    # 资金在EOA主钱包时，funder=EOA；资金在Proxy时，funder=PROXY且需改signature_type=1
    funder = os.environ.get("EOA_WALLET", "")

    _client = ClobClient(
        "https://clob.polymarket.com",
        key=private_key,
        chain_id=137,
        signature_type=0,  # EOA
        funder=funder,
    )
    creds = _client.create_or_derive_api_creds()
    _client.set_api_creds(creds)
    logger.info(f"✅ CLOB SDK 初始化完成 | funder={funder[:10]}...")

    # 预热：用假单预加载 coincurve 签名库 + TLS 连接池
    # 原理：首次签名+HTTPS请求很慢（~200ms），预热后复用连接只需 ~26ms
    _warmup()


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
    """
    token_id = str(token_id)
    if token_id in _token_cache:
        return _token_cache[token_id]

    t0 = time.time()
    try:
        # 这3个调用会填充 SDK 内部缓存，后续 create_order 直接命中缓存
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


def _warmup():
    """预热签名库 + TLS/HTTP2连接池

    原理：首次 EIP-712 签名需加载 coincurve/secp256k1（~150ms），
    首次 HTTPS 请求需 TLS 握手 + HTTP/2 协商（~300ms）。
    预热后复用连接池，后续下单只剩纯网络延迟（~25-350ms 取决于物理距离）。

    步骤：
    1. 假签名：create_order 触发 coincurve 加载（本地，~1ms after first）
    2. GET 预热：get_server_time 建立 TLS + HTTP/2 连接
    3. POST 预热：假 post_order 确保 POST endpoint 的 HTTP/2 stream 就绪
    """
    t0 = time.time()
    try:
        # 1. 预热签名库：本地创建假单触发 coincurve/secp256k1 加载
        #    neg_risk=True 避免 SDK 内部把 False 当 falsy 触发额外 HTTP 查询
        _client.create_order(
            OrderArgs(token_id="0", price=0.01, size=1.0, side=BUY),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=True),
        )
        t1 = time.time()
        sign_ms = (t1 - t0) * 1000

        # 2. GET 预热：建立 TLS 连接 + HTTP/2 协商
        _client.get_server_time()
        t2 = time.time()
        tls_ms = (t2 - t1) * 1000

        # 3. POST 预热：用假单发一次真实 POST 到 /order endpoint
        #    SDK 内部 httpx.Client 按 (host,port) 复用连接，但 POST 可能走不同的 HTTP/2 stream
        #    这一步确保 POST 路径完全就绪（含 API 鉴权头序列化）
        try:
            fake_order = _client.create_order(
                OrderArgs(token_id="0", price=0.01, size=1.0, side=BUY),
                options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=True),
            )
            _client.post_order(fake_order, OrderType.GTC)
        except Exception:
            pass  # 假token必定被服务器拒绝，但 POST 连接已建立
        t3 = time.time()
        post_ms = (t3 - t2) * 1000

        logger.info(
            f"🔥 预热完成: 签名={sign_ms:.0f}ms TLS={tls_ms:.0f}ms "
            f"POST={post_ms:.0f}ms 总计={(t3-t0)*1000:.0f}ms"
        )
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.info(f"🔥 预热完成(签名库已加载): {elapsed:.0f}ms | {e}")


# ── 下单 ──

def place_order(token_id, side, price, size, order_type=OrderType.GTC):
    """GTC 限价单 — 用于入场

    Args:
        token_id: 代币ID
        side: BUY 或 SELL
        price: 限价 (0.01~0.99)
        size: 份数
        order_type: 订单类型，默认GTC

    Returns:
        dict: {success, matched, order_id, status, making, taking, raw}
    """
    t0 = time.time()
    token_id = str(token_id)
    cached = _token_cache.get(token_id, {})
    neg_risk = cached.get("neg_risk", None)
    # neg_risk=None 时 SDK 内部查缓存（precache_token 已填充）；True 直接跳过查询
    try:
        resp = _client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=side,
            ),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk),
        )
        elapsed = (time.time() - t0) * 1000
        result = _parse_response(resp)
        result["elapsed_ms"] = round(elapsed, 1)
        logger.info(f"📡 SDK下单 {side} {size}@{price} | {result['status']} | {elapsed:.0f}ms")
        return result
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.error(f"❌ SDK下单异常: {e} | {elapsed:.0f}ms")
        return {"success": False, "matched": False, "status": "ERROR",
                "error": str(e), "elapsed_ms": round(elapsed, 1),
                "making": 0, "taking": 0, "order_id": None, "raw": str(e)}


def place_fok_order(token_id, side, price, size):
    """FOK 即时成交单 — 用于止损/止盈平仓

    FOK = Fill-or-Kill: 要么全部立即成交，要么整单取消。
    不会留下挂单。

    Args:
        token_id: 代币ID
        side: BUY 或 SELL
        price: 最差可接受价格（滑点保护）
        size: 份数

    Returns:
        dict: 同 place_order
    """
    t0 = time.time()
    token_id = str(token_id)
    cached = _token_cache.get(token_id, {})
    neg_risk = cached.get("neg_risk", None)
    try:
        # MarketOrderArgs 的 amount 对 SELL 是份数，对 BUY 是美元金额
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
        resp = _client.post_order(order, OrderType.FOK)
        elapsed = (time.time() - t0) * 1000
        result = _parse_response(resp)
        result["elapsed_ms"] = round(elapsed, 1)
        logger.info(f"⚡ FOK {side} {size}@{price} | {result['status']} | {elapsed:.0f}ms")
        return result
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.error(f"❌ FOK异常: {e} | {elapsed:.0f}ms")
        return {"success": False, "matched": False, "status": "ERROR",
                "error": str(e), "elapsed_ms": round(elapsed, 1),
                "making": 0, "taking": 0, "order_id": None, "raw": str(e)}


# ── 取消 ──

def cancel_all(token_id=None):
    """取消订单
    token_id=None: 取消所有订单
    token_id指定: 取消该token的所有订单
    """
    try:
        if token_id:
            resp = _client.cancel_market_orders(asset_id=str(token_id))
        else:
            resp = _client.cancel_all()
        return True
    except Exception as e:
        logger.warning(f"取消订单失败: {e}")
        return False


# ── 余额 ──

def get_balance():
    """获取 USDC 余额（collateral）"""
    try:
        resp = _client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                token_id="",
                signature_type=0,
            )
        )
        # resp 可能是 dict 或对象
        if isinstance(resp, dict):
            return float(resp.get("balance", 0))
        return float(getattr(resp, "balance", 0))
    except Exception as e:
        logger.warning(f"获取余额失败: {e}")
        return 0


def get_token_balance(token_id):
    """获取 conditional token 余额"""
    try:
        resp = _client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=str(token_id),
                signature_type=0,
            )
        )
        if isinstance(resp, dict):
            return float(resp.get("balance", 0))
        return float(getattr(resp, "balance", 0))
    except Exception as e:
        logger.warning(f"获取token余额失败: {e}")
        return None


# ── 市场数据 ──

def get_orderbook(token_id):
    """获取订单簿"""
    try:
        return _client.get_order_book(str(token_id))
    except Exception as e:
        logger.warning(f"获取订单簿失败: {e}")
        return None


def get_midpoint(token_id):
    """获取中间价"""
    try:
        resp = _client.get_midpoint(str(token_id))
        if isinstance(resp, dict):
            return float(resp.get("mid", 0))
        return float(resp)
    except Exception:
        return None


def get_last_trade_price(token_id):
    """获取最近成交价"""
    try:
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
        return _client.get_order(str(order_id))
    except Exception as e:
        logger.warning(f"查询订单失败: {e}")
        return None


def get_orders(asset_id=None):
    """查询所有订单（可按 asset_id 过滤）"""
    try:
        from py_clob_client.clob_types import OpenOrderParams
        params = OpenOrderParams(asset_id=str(asset_id)) if asset_id else None
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
