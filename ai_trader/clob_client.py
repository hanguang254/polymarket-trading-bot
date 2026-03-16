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


def _warmup():
    """预热签名库 + TLS连接池，首次调用后后续下单延迟从 ~200ms 降至 ~26ms"""
    try:
        t0 = time.time()
        # 1. 预热TLS连接池：发真实HTTP请求建立连接
        _client.get_server_time()
        t1 = time.time()
        tls_ms = (t1 - t0) * 1000

        # 2. 预热签名库：本地创建假单触发 coincurve/secp256k1 加载
        #    使用 get_tick_size 先缓存 tick_size，避免额外查询
        _client.create_order(
            OrderArgs(token_id="0", price=0.01, size=1.0, side=BUY),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
        )
        t2 = time.time()
        sign_ms = (t2 - t1) * 1000

        logger.info(f"🔥 预热完成: TLS={tls_ms:.0f}ms 签名={sign_ms:.0f}ms 总计={(t2-t0)*1000:.0f}ms")
    except Exception as e:
        # create_order 用假token可能报错，但签名库已加载，目的达到
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
    try:
        resp = _client.create_and_post_order(
            OrderArgs(
                token_id=str(token_id),
                price=float(price),
                size=float(size),
                side=side,
            ),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
        )
        # post_order 不接受 order_type 参数，GTC 是默认值
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
    try:
        # MarketOrderArgs 的 amount 对 SELL 是份数，对 BUY 是美元金额
        # 但 FOK 需要 price 作为最差价限制
        amount = float(price) * float(size) if side == BUY else float(size)
        order = _client.create_market_order(
            MarketOrderArgs(
                token_id=str(token_id),
                amount=amount,
                side=side,
                price=float(price),
                fee_rate_bps=0,
                nonce=0,
                taker="0x0000000000000000000000000000000000000000",
                order_type=OrderType.FOK,
            ),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
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
        matched = status == "MATCHED" or status == "FILLED"
        making = float(raw.get("making", raw.get("makerAmount", 0)) or 0)
        taking = float(raw.get("taking", raw.get("takerAmount", 0)) or 0)
    else:
        status = str(getattr(raw, "status", ""))
        order_id = str(getattr(raw, "orderID", getattr(raw, "order_id", "")))
        matched = "MATCHED" in status or "FILLED" in status
        making = float(getattr(raw, "making", 0) or 0)
        taking = float(getattr(raw, "taking", 0) or 0)

    success = matched or status in ("MATCHED", "FILLED", "LIVE")

    return {
        "success": success,
        "matched": matched,
        "status": status,
        "order_id": order_id,
        "making": making,
        "taking": taking,
        "raw": str(raw)[:500],
    }
