#!/usr/bin/env python3
"""
Polymarket 自动领取已结算收益脚本 v3.8
- data-api REST 查可领取持仓（redeemable=true），零 RPC 调用
- Relayer 免 gas 提交（py-builder-relayer-client SDK EIP-712 签名）
- 自付 gas 回退（Relayer 失败时）
- 并行 redeem: Relayer 优先 → 自付 gas 批量 → 统一收回执
- revert 自动切换 normal↔neg-risk 重试
- 启动时 data-api 清理假标记（零 RPC）
- 持久化已 redeem 记录 + 日志按天写入文件 + Telegram 通知
"""
import os
import sys
import json
import time
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from eth_abi import encode as abi_encode
from web3 import Web3

# ==============================================================================
# 配置
# ==============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

EOA_WALLET = os.environ.get("EOA_WALLET", "").strip()
PROXY_WALLET = os.environ.get("PROXY_WALLET", "").strip()
PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "").strip()
CLOB_SIGNATURE_TYPE = int(os.environ.get("CLOB_SIGNATURE_TYPE", "0"))
if not PROXY_WALLET or not PRIVATE_KEY:
    raise RuntimeError("PROXY_WALLET and PRIVATE_KEY must be set in .env")
RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

REDEEM_INTERVAL = int(os.environ.get("REDEEM_INTERVAL", 600))  # 默认10分钟

# 已 redeem 记录文件
REDEEMED_FILE = os.path.join(SCRIPT_DIR, "logs", "redeemed_conditions.json")
POSITIONS_FILE = os.path.join(SCRIPT_DIR, "logs", "positions.jsonl")

# ==============================================================================
# 合约地址 (Polygon mainnet)
# ==============================================================================

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"       # Conditional Tokens Framework
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # NegRiskAdapter
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"      # USDC (PoS) on Polygon
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11" # Multicall3 (Polygon)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# ==============================================================================
# 最小 ABI
# ==============================================================================

CTF_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]

USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]

SAFE_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "getOwners",
        "outputs": [{"name": "", "type": "address[]"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "getThreshold",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

_SAFE_CONTEXT_CACHE: dict[str, dict] = {}

# ==============================================================================
# 日志：终端 + 按天日志文件
# ==============================================================================

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


class _TeeWriter:
    """同时写入终端和日志文件，日志按天自动轮转"""
    def __init__(self, stream):
        self._stream = stream
        self._date = None
        self._file = None

    def _ensure_file(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._date != today:
            if self._file:
                self._file.close()
            self._date = today
            path = os.path.join(LOG_DIR, f"redeem_{today}.log")
            self._file = open(path, "a", encoding="utf-8")
        return self._file

    def write(self, msg):
        self._stream.write(msg)
        try:
            f = self._ensure_file()
            f.write(msg)
            f.flush()
        except Exception:
            pass

    def flush(self):
        self._stream.flush()
        if self._file:
            try:
                self._file.flush()
            except Exception:
                pass


sys.stdout = _TeeWriter(sys.__stdout__)
sys.stderr = _TeeWriter(sys.__stderr__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [REDEEM] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("auto_redeem")

# ==============================================================================
# 已 redeem 记录持久化
# ==============================================================================

def load_redeemed() -> dict:
    try:
        with open(REDEEMED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_redeemed(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(REDEEMED_FILE), exist_ok=True)
        with open(REDEEMED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"保存 redeem 记录失败: {e}")


def mark_redeemed(redeemed: dict, condition_id: str, slug: str, value: float,
                  size: float, tx_hash: str = "") -> None:
    redeemed[condition_id] = {
        "slug": slug,
        "value": value,
        "size": size,
        "tx_hash": tx_hash,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_redeemed(redeemed)


def clear_stale_redeemed_marks(redeemed: dict, redeemable: list[dict]) -> int:
    """
    每轮动态清理误标记：
    如果某个 condition 已在 redeemed_conditions.json 中，
    但本轮扫描时链上余额仍 > 0，说明它并未真正领干净，移除标记立即重试。
    """
    stale_ids = {
        r.get("condition_id")
        for r in redeemable
        if r.get("condition_id") in redeemed and (r.get("balance", 0) or 0) > 0
    }
    stale_ids.discard(None)

    if not stale_ids:
        return 0

    for cid in stale_ids:
        del redeemed[cid]

    save_redeemed(redeemed)
    log.info(f"🔄 发现 {len(stale_ids)} 个已标记但链上仍有余额的头寸，已移除标记并重新领取")
    return len(stale_ids)


def _safe_float(value) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_jsonl(path: str) -> list[dict]:
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
    except FileNotFoundError:
        pass
    return records


def _position_sort_key(position: dict | None) -> str:
    if not position:
        return ""
    return str(
        position.get("exit_time")
        or position.get("updated_time")
        or position.get("entry_time")
        or ""
    )


def _load_closed_position_indexes() -> tuple[dict[str, dict], dict[str, dict]]:
    by_token_id: dict[str, dict] = {}
    by_slug: dict[str, dict] = {}

    for pos in _load_jsonl(POSITIONS_FILE):
        if pos.get("closed") is not True:
            continue

        token_id = str(pos.get("token_id") or "").strip()
        market_slug = str(pos.get("slug") or "").strip()

        if token_id and _position_sort_key(pos) >= _position_sort_key(by_token_id.get(token_id)):
            by_token_id[token_id] = pos
        if market_slug and _position_sort_key(pos) >= _position_sort_key(by_slug.get(market_slug)):
            by_slug[market_slug] = pos

    return by_token_id, by_slug


def estimate_redeem_profit(redeemed_positions: list[dict], settled_amount: float | None) -> tuple[float | None, int]:
    """
    用本地已关闭持仓成本估算本次链上结算的净收益。
    返回: (net_profit, matched_count)
    """
    if not redeemed_positions:
        return None, 0

    by_token_id, by_slug = _load_closed_position_indexes()
    if not by_token_id and not by_slug:
        return None, 0

    matched_cost = 0.0
    matched_count = 0

    for pos in redeemed_positions:
        token_id = str(pos.get("token_id") or "").strip()
        market_slug = str(pos.get("market_slug") or "").strip()
        local_pos = by_token_id.get(token_id) or (by_slug.get(market_slug) if market_slug else None)
        if not local_pos:
            continue

        entry_price = _safe_float(local_pos.get("entry_price"))
        balance_raw = _safe_float(pos.get("balance"))
        if entry_price is None or balance_raw is None or balance_raw <= 0:
            continue

        redeemed_size = balance_raw / 1e6
        matched_cost += redeemed_size * entry_price
        matched_count += 1

    if matched_count == 0 or settled_amount is None or settled_amount < 0:
        return None, 0

    if matched_count != len(redeemed_positions):
        return None, matched_count

    net_profit = settled_amount - matched_cost
    return round(net_profit, 4), matched_count

# ==============================================================================
# Telegram 通知
# ==============================================================================

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram 通知失败: {e}")

# ==============================================================================
# Web3 初始化
# ==============================================================================

def init_web3():
    """初始化 web3 连接和钱包"""
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        raise ConnectionError(f"无法连接 RPC: {RPC_URL}")

    wallet = w3.eth.account.from_key(PRIVATE_KEY)

    ctf_contract = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI
    )
    usdc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI
    )

    return w3, wallet, ctf_contract, usdc_contract

# ==============================================================================
# REST API 持仓获取
# ==============================================================================

DATA_API = "https://data-api.polymarket.com"


def _fetch_positions_api(addr: str, label: str) -> list:
    """通过 data-api 获取单个地址的活跃持仓"""
    try:
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": addr, "limit": 200, "sizeThreshold": 0},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        positions = data if isinstance(data, list) else []
        if positions:
            log.info(f"  {label} 地址: {len(positions)} 个持仓")
        return positions
    except Exception as e:
        log.warning(f"  {label} 持仓查询失败: {str(e)[:100]}")
        return []


def _fetch_closed_positions_api(addr: str, label: str) -> list:
    """通过 data-api 获取已关闭持仓"""
    try:
        resp = requests.get(
            f"{DATA_API}/closed-positions",
            params={"user": addr, "limit": 50},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        positions = data if isinstance(data, list) else []
        if positions:
            log.info(f"  {label} 已关闭持仓: {len(positions)} 个")
        return positions
    except Exception as e:
        log.warning(f"  {label} 已关闭持仓查询失败: {str(e)[:100]}")
        return []


def fetch_positions() -> list:
    """通过 Polymarket data-api 获取钱包持仓（活跃+已关闭）"""
    all_positions = []

    for addr, label in [(PROXY_WALLET, "proxy"), (EOA_WALLET, "eoa")]:
        if not addr:
            continue
        active_positions = _fetch_positions_api(addr, label)
        closed_positions = _fetch_closed_positions_api(addr, label)
        if not active_positions and not closed_positions:
            log.info(f"  {label} 地址: data-api 未返回任何持仓")
        all_positions.extend(active_positions)
        all_positions.extend(closed_positions)

    if not all_positions:
        log.info("  data-api 未返回任何 active/closed 持仓")

    return all_positions


# ==============================================================================
# 链上验证
# ==============================================================================


def _is_likely_resolved(position: dict) -> bool:
    return (
        position.get("resolved") is True
        or position.get("redeemable") is True
        or position.get("game_status") == "resolved"
        or position.get("status") == "resolved"
        or position.get("outcome") not in (None, "", "pending")
    )


def find_redeemable(w3: Web3, wallet, ctf_contract) -> list[dict]:
    """data-api 获取可领取持仓（redeemable=true），零 RPC 调用"""
    eoa_cs = Web3.to_checksum_address(wallet.address)
    proxy_cs = Web3.to_checksum_address(PROXY_WALLET) if PROXY_WALLET else ""

    redeemable = []
    seen_conditions: set[str] = set()

    for addr, label in [(proxy_cs, "proxy"), (eoa_cs, "eoa")]:
        if not addr:
            continue

        # data-api 查可领取持仓（活跃 + 已关闭两个端点）
        api_positions: list[dict] = []
        for endpoint in ["/positions", "/closed-positions"]:
            try:
                resp = requests.get(
                    f"{DATA_API}{endpoint}",
                    params={"user": addr, "redeemable": "true", "limit": 200, "sizeThreshold": 0},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    api_positions.extend(data)
            except Exception as e:
                log.warning(f"  {label} {endpoint} 查询失败: {str(e)[:80]}")

        # 也加载全量持仓中字段标记为已结算的（兜底）
        all_positions = _fetch_positions_api(addr, label)
        closed_positions = _fetch_closed_positions_api(addr, label)
        for p in all_positions + closed_positions:
            if _is_likely_resolved(p) and p not in api_positions:
                api_positions.append(p)

        if not api_positions:
            log.info(f"  {label} 地址: 无可领取持仓")
            continue

        # 按 conditionId 分组
        by_condition: dict[str, list[dict]] = {}
        for p in api_positions:
            cid = p.get("conditionId") or p.get("condition_id", "")
            if cid and cid not in seen_conditions:
                by_condition.setdefault(cid, []).append(p)

        for cond_id, cond_positions in by_condition.items():
            seen_conditions.add(cond_id)

            # 选 size 最大的记录
            best = max(cond_positions, key=lambda p: float(p.get("size", 0) or 0))
            size = float(best.get("size", 0) or 0)
            if size <= 0:
                continue

            token_id = best.get("asset") or best.get("token_id", "")
            bal = int(size * 1e6)

            market_slug = best.get("slug", best.get("market_slug", ""))
            title = best.get("title", market_slug or cond_id[:16])
            neg_risk = best.get(
                "negativeRisk",
                best.get("neg_risk", best.get("negRisk", False)),
            )
            cur_value = float(
                best.get(
                    "currentValue",
                    best.get("current_value", best.get("value", 0)),
                ) or 0
            )

            redeemable.append({
                "condition_id": cond_id,
                "token_id": token_id,
                "balance": bal,
                "holder": addr,
                "slug": (title or "unknown")[:50],
                "market_slug": market_slug,
                "value": cur_value,
                "neg_risk": neg_risk,
                "size": size,
            })
            log.info(f"  可领取: {(title or '')[:40]} (balance: {bal}, holder: {addr[:10]}...)")

    return redeemable

# ==============================================================================
# 链上 Redeem（EOA / Proxy Safe）
# ==============================================================================

def _get_safe_context(w3: Web3, safe_address: str) -> dict:
    safe_cs = Web3.to_checksum_address(safe_address)
    cached = _SAFE_CONTEXT_CACHE.get(safe_cs)
    if cached:
        return cached

    safe_contract = w3.eth.contract(address=safe_cs, abi=SAFE_ABI)
    threshold = safe_contract.functions.getThreshold().call()
    owners = [Web3.to_checksum_address(addr) for addr in safe_contract.functions.getOwners().call()]

    context = {
        "address": safe_cs,
        "contract": safe_contract,
        "threshold": threshold,
        "owners": owners,
    }
    _SAFE_CONTEXT_CACHE[safe_cs] = context
    return context


def _build_safe_prevalidated_signature(owner_address: str) -> bytes:
    owner_hex = owner_address.lower().replace("0x", "").rjust(64, "0")
    return bytes.fromhex(owner_hex) + (b"\x00" * 32) + b"\x01"


def _build_safe_exec_calldata(w3: Web3, wallet, safe_address: str, target: str, call_data: bytes) -> bytes:
    safe_ctx = _get_safe_context(w3, safe_address)
    wallet_cs = Web3.to_checksum_address(wallet.address)

    if safe_ctx["threshold"] != 1:
        raise RuntimeError(f"proxy safe threshold={safe_ctx['threshold']}，当前脚本只支持 1/1 Safe")
    if wallet_cs not in safe_ctx["owners"]:
        raise RuntimeError(f"EOA {wallet.address} 不是 proxy safe owner，无法代 safe 执行 redeem")

    selector = w3.keccak(
        text="execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)"
    )[:4]
    return selector + abi_encode(
        ["address", "uint256", "bytes", "uint8", "uint256", "uint256", "uint256", "address", "address", "bytes"],
        [
            Web3.to_checksum_address(target),
            0,
            call_data,
            0,
            0,
            0,
            0,
            Web3.to_checksum_address(ZERO_ADDRESS),
            Web3.to_checksum_address(ZERO_ADDRESS),
            _build_safe_prevalidated_signature(wallet.address),
        ],
    )


def _prepare_execution_tx(w3: Web3, wallet, holder: str | None, target: str, call_data: bytes) -> tuple[str, bytes, int, str]:
    holder_cs = Web3.to_checksum_address(holder or wallet.address)
    wallet_cs = Web3.to_checksum_address(wallet.address)

    if holder_cs == wallet_cs:
        return Web3.to_checksum_address(target), call_data, REDEEM_FIXED_GAS, "eoa"

    proxy_cs = Web3.to_checksum_address(PROXY_WALLET)
    if holder_cs != proxy_cs:
        raise RuntimeError(f"发现持仓在未知 holder={holder_cs}，当前脚本只支持 EOA 或配置的 PROXY_WALLET")
    if CLOB_SIGNATURE_TYPE != 2:
        raise RuntimeError(
            f"proxy 持仓当前只支持 CLOB_SIGNATURE_TYPE=2 (GNOSIS_SAFE)，当前为 {CLOB_SIGNATURE_TYPE}"
        )

    safe_exec_data = _build_safe_exec_calldata(w3, wallet, proxy_cs, target, call_data)
    return proxy_cs, safe_exec_data, SAFE_REDEEM_FIXED_GAS, "gnosis-safe"


RELAYER_API_KEY = os.environ.get("RELAYER_API_KEY", "").strip()
RELAYER_API_KEY_ADDRESS = os.environ.get("RELAYER_API_KEY_ADDRESS", EOA_WALLET).strip()
RELAYER_URL = "https://relayer-v2.polymarket.com"



def _send_tx_relayer(w3: Web3, wallet, target: str, call_data: bytes, holder: str | None = None) -> str | None:
    """通过 Polymarket Relayer 发送交易（免 gas）。
    使用官方 py-builder-relayer-client SDK 构建 EIP-712 签名请求。
    """
    if not RELAYER_API_KEY:
        return None

    wallet_cs = Web3.to_checksum_address(wallet.address)
    target_cs = Web3.to_checksum_address(target)

    try:
        from py_builder_relayer_client.builder.safe import build_safe_transaction_request
        from py_builder_relayer_client.models import SafeTransaction, SafeTransactionArgs, OperationType
        from py_builder_relayer_client.config import get_contract_config
        from py_builder_relayer_client.signer import Signer
    except ImportError:
        log.warning("  py-builder-relayer-client 未安装，回退自付gas")
        return None

    # 获取 Safe nonce
    try:
        nonce_resp = requests.get(
            f"{RELAYER_URL}/nonce",
            params={"address": wallet_cs, "type": "SAFE"},
            timeout=10,
        )
        nonce = nonce_resp.json().get("nonce", "0")
    except Exception as e:
        log.warning(f"  Relayer nonce 获取失败: {e}，回退自付gas")
        return None

    # 使用官方 SDK 构建签名请求
    config = get_contract_config(137)
    signer = Signer(PRIVATE_KEY, 137)
    txn = SafeTransaction(
        to=target_cs,
        operation=OperationType.Call,
        data="0x" + call_data.hex(),
        value="0",
    )
    args = SafeTransactionArgs(
        from_address=wallet_cs,
        nonce=nonce,
        chain_id=137,
        transactions=[txn],
    )
    txn_request = build_safe_transaction_request(signer=signer, args=args, config=config)
    body = txn_request.to_dict()

    try:
        resp = requests.post(
            f"{RELAYER_URL}/submit",
            json=body,
            headers={
                "Content-Type": "application/json",
                "RELAYER_API_KEY": RELAYER_API_KEY,
                "RELAYER_API_KEY_ADDRESS": RELAYER_API_KEY_ADDRESS,
            },
            timeout=15,
        )
        result = resp.json()
        tx_id = result.get("transactionID", "")
        state = result.get("state", "")

        if resp.status_code == 200 and tx_id:
            log.info(f"  ⚡ Relayer 免gas提交: txID={tx_id[:20]}... state={state}")

            # 等待确认（轮询状态）
            for _ in range(30):  # 最多等30秒
                time.sleep(1)
                try:
                    status_resp = requests.get(
                        f"{RELAYER_URL}/transaction",
                        params={"id": tx_id},
                        headers={
                            "RELAYER_API_KEY": RELAYER_API_KEY,
                            "RELAYER_API_KEY_ADDRESS": RELAYER_API_KEY_ADDRESS,
                        },
                        timeout=10,
                    )
                    status = status_resp.json()
                    cur_state = status.get("state", "")
                    tx_hash = status.get("transactionHash", "")
                    if cur_state == "STATE_CONFIRMED":
                        log.info(f"  ✅ Relayer 免gas确认: {tx_hash}")
                        return tx_hash
                    elif cur_state == "STATE_FAILED":
                        log.warning(f"  ❌ Relayer 交易失败: {status}")
                        return None
                    elif cur_state == "STATE_INVALID":
                        log.warning(f"  ❌ Relayer 交易无效: {status}")
                        return None
                except Exception:
                    pass

            log.warning(f"  ⏳ Relayer 超时未确认: txID={tx_id}")
            return tx_id  # 返回 txID 供后续查

        else:
            log.warning(f"  ❌ Relayer 提交失败: {resp.status_code} {result}")
            return None

    except Exception as e:
        log.warning(f"  Relayer 异常: {e}，回退自付gas")
        return None


def _send_tx(w3: Web3, wallet, target: str, call_data: bytes, holder: str | None = None) -> str | None:
    """
    发送链上交易。优先 Relayer（免gas），失败回退自付 gas。
    """
    # 优先 Relayer（仅 Proxy Safe 持仓）
    holder_cs = Web3.to_checksum_address(holder or wallet.address)
    proxy_cs = Web3.to_checksum_address(PROXY_WALLET) if PROXY_WALLET else ""
    log.info(f"  [Relayer] check: key={'✓' if RELAYER_API_KEY else '✗'} holder={holder_cs[:10]} proxy={proxy_cs[:10]} match={holder_cs==proxy_cs} sig_type={CLOB_SIGNATURE_TYPE}")
    if RELAYER_API_KEY and holder_cs == proxy_cs and CLOB_SIGNATURE_TYPE == 2:
        result = _send_tx_relayer(w3, wallet, target, call_data, holder)
        if result:
            return result
        log.info("  ↻ Relayer 失败，回退自付gas")

    # 回退：自付 gas
    target_cs, tx_data, _, route = _prepare_execution_tx(w3, wallet, holder, target, call_data)

    gas_estimate = w3.eth.estimate_gas(
        {"from": wallet.address, "to": target_cs, "data": tx_data}
    )

    tx = w3.eth.account.sign_transaction(
        {
            "to": target_cs,
            "data": tx_data,
            "gas": gas_estimate + 50_000,
            "gasPrice": int(w3.eth.gas_price * 1.3),
            "nonce": w3.eth.get_transaction_count(wallet.address, "pending"),
            "chainId": 137,
        },
        PRIVATE_KEY,
    )

    tx_hash = w3.eth.send_raw_transaction(tx.raw_transaction)
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    except Exception:
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt and receipt.status == 1:
                log.info(f"  ✅ 超时但交易已上链: {tx_hash.hex()}")
                return tx_hash.hex()
        except Exception:
            pass
        raise TimeoutError(f"tx_pending:{tx_hash.hex()}")

    if receipt.status == 1:
        if route == "gnosis-safe":
            log.info(f"  🔐 proxy safe 已执行 redeem: {target_cs[:10]}...")
        return tx_hash.hex()
    return None


def redeem_normal(w3: Web3, wallet, cond_id: str, holder: str | None = None) -> str | None:
    """
    普通市场 redeem: CTF.redeemPositions(collateral, parentCollectionId, conditionId, indexSets)
    EOA 直调或经 1/1 Safe 转发到 CTF 合约
    """
    usdc_cs = Web3.to_checksum_address(USDC_ADDRESS)

    selector = w3.keccak(
        text="redeemPositions(address,bytes32,bytes32,uint256[])"
    )[:4]
    call_data = selector + abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            usdc_cs,
            b"\x00" * 32,  # parentCollectionId (root)
            bytes.fromhex(cond_id.replace("0x", "")),
            [1, 2],        # indexSets: YES + NO
        ],
    )

    return _send_tx(w3, wallet, CTF_ADDRESS, call_data, holder=holder)


def redeem_neg_risk(w3: Web3, wallet, cond_id: str, amounts: list[int], holder: str | None = None) -> str | None:
    """
    neg-risk 市场 redeem: NegRiskAdapter.redeemPositions(conditionId, amounts)
    EOA 直调或经 1/1 Safe 转发到 NegRiskAdapter
    """
    selector = w3.keccak(
        text="redeemPositions(bytes32,uint256[])"
    )[:4]
    call_data = selector + abi_encode(
        ["bytes32", "uint256[]"],
        [
            bytes.fromhex(cond_id.replace("0x", "")),
            amounts,
        ],
    )

    return _send_tx(w3, wallet, NEG_RISK_ADAPTER, call_data, holder=holder)


def redeem_position(w3: Web3, wallet, cond_id: str,
                    neg_risk: bool = False, balance: int = 0, holder: str | None = None) -> str | None:
    """
    执行单个 condition 的链上 redeem（EOA 直调 / Proxy Safe 转发）
    策略：先按 neg_risk 标志尝试，失败则切换另一种方式重试

    Returns: tx_hash on success, None on failure
    """
    if not cond_id.startswith("0x"):
        cond_id = "0x" + cond_id

    amt = balance if balance > 0 else 1

    # 第一次尝试
    label1 = "neg-risk" if neg_risk else "normal"
    try:
        if neg_risk:
            tx = redeem_neg_risk(w3, wallet, cond_id, [amt, amt], holder=holder)
        else:
            tx = redeem_normal(w3, wallet, cond_id, holder=holder)
        if tx:
            log.info(f"  ✅ redeem 成功({label1}): {cond_id[:18]}... | tx: {tx}")
            return tx
    except Exception as e:
        err_msg = str(e).lower()
        if "tx_pending:" in err_msg:
            # 交易还在 mempool pending，不要发新交易避免 nonce 冲突
            pending_hash = str(e).split("tx_pending:")[-1]
            log.warning(f"  ⚠️ {label1}交易pending中: {pending_hash[:18]}... | 等下一轮重查")
            return None
        if "execution reverted" in err_msg:
            log.info(f"  🔄 {label1}方式 revert，切换重试: {cond_id[:18]}...")
        else:
            log.warning(f"  ⚠️ {label1}失败: {cond_id[:18]}... | {str(e)[:120]}")

    # 第二次：切换方式重试
    label2 = "normal" if neg_risk else "neg-risk"
    try:
        if neg_risk:
            tx = redeem_normal(w3, wallet, cond_id, holder=holder)
        else:
            tx = redeem_neg_risk(w3, wallet, cond_id, [amt, amt], holder=holder)
        if tx:
            log.info(f"  ✅ redeem 成功({label2}): {cond_id[:18]}... | tx: {tx}")
            return tx
    except Exception as e:
        err_msg = str(e).lower()
        if "tx_pending:" in err_msg:
            pending_hash = str(e).split("tx_pending:")[-1]
            log.warning(f"  ⚠️ {label2}交易pending中: {pending_hash[:18]}... | 等下一轮重查")
            return None
        log.warning(f"  ❌ {label2}也失败: {cond_id[:18]}... | {str(e)[:120]}")

    return None


# ==============================================================================
# 并行 Redeem（预分配 nonce，批量发送，统一收集回执）
# ==============================================================================

REDEEM_FIXED_GAS = 300_000  # redeem 通常 100-150k，固定 gas 避免逐个 estimate
SAFE_REDEEM_FIXED_GAS = 600_000  # 通过 1/1 Safe 执行 redeem 的外层 gas


def _build_redeem_calldata(w3: Web3, cond_id: str, neg_risk: bool, balance: int) -> tuple[str, bytes]:
    """
    构建单个 redeem 的 calldata + target 地址
    Returns: (target_address, call_data)
    """
    if not cond_id.startswith("0x"):
        cond_id = "0x" + cond_id
    amt = balance if balance > 0 else 1

    if neg_risk:
        selector = w3.keccak(text="redeemPositions(bytes32,uint256[])")[:4]
        call_data = selector + abi_encode(
            ["bytes32", "uint256[]"],
            [bytes.fromhex(cond_id.replace("0x", "")), [amt, amt]],
        )
        return NEG_RISK_ADAPTER, call_data
    else:
        usdc_cs = Web3.to_checksum_address(USDC_ADDRESS)
        selector = w3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        call_data = selector + abi_encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [usdc_cs, b"\x00" * 32, bytes.fromhex(cond_id.replace("0x", "")), [1, 2]],
        )
        return CTF_ADDRESS, call_data


def parallel_redeem(w3: Web3, wallet, positions: list[dict]) -> list[dict]:
    """
    并行发送 redeem 交易（预分配 nonce）：
    Phase 1: 批量构建 + 签名 + 快速发送（不等回执）
    Phase 2: 统一收集回执
    Phase 3: revert 的切换 normal↔neg-risk 逐个重试

    Returns: list of {position, success, tx_hash}
    """
    if not positions:
        return []

    proxy_cs = Web3.to_checksum_address(PROXY_WALLET) if PROXY_WALLET else ""
    use_relayer = bool(RELAYER_API_KEY and CLOB_SIGNATURE_TYPE == 2 and proxy_cs)

    relayer_results = []
    selfpay_positions = []

    # ── Relayer 免gas 阶段 ──
    if use_relayer:
        log.info(f"⚡ Relayer 免gas: 尝试 {len(positions)} 笔...")
        for r in positions:
            holder = r.get("holder", wallet.address)
            holder_cs = Web3.to_checksum_address(holder)
            if holder_cs != proxy_cs:
                selfpay_positions.append(r)
                continue
            cid = r["condition_id"]
            neg_risk = r.get("neg_risk", False)
            balance = r.get("balance", 0)
            try:
                target, call_data = _build_redeem_calldata(w3, cid, neg_risk, balance)
                tx_hash = _send_tx_relayer(w3, wallet, target, call_data, holder)
                if tx_hash:
                    log.info(f"  ⚡ Relayer 免gas成功: {r.get('slug', '')[:35]}")
                    relayer_results.append({"position": r, "success": True, "tx_hash": tx_hash})
                    continue
            except Exception as e:
                log.warning(f"  Relayer 异常: {r.get('slug', '')[:35]} | {e}")
            log.info(f"  ↻ Relayer→自付gas: {r.get('slug', '')[:35]}")
            selfpay_positions.append(r)

        relayer_ok = len(relayer_results)
        log.info(f"  📋 Relayer: {relayer_ok}/{len(positions)} 免gas成功, {len(selfpay_positions)} 回退自付gas")
        if not selfpay_positions:
            return relayer_results
    else:
        selfpay_positions = positions

    # ── Phase 1: 批量发送（自付gas）──
    base_nonce = w3.eth.get_transaction_count(wallet.address, "pending")
    gas_price = int(w3.eth.gas_price * 1.3)
    sent = []  # (position, tx_hash_bytes, neg_risk_used)
    send_ok = 0

    log.info(f"⚡ Phase 1: 并行发送 {len(selfpay_positions)} 笔 redeem 交易...")

    for i, r in enumerate(selfpay_positions):
        cid = r["condition_id"]
        neg_risk = r.get("neg_risk", False)
        balance = r.get("balance", 0)
        holder = r.get("holder", wallet.address)

        try:
            target, call_data = _build_redeem_calldata(w3, cid, neg_risk, balance)
            tx_target, tx_data, gas_limit, route = _prepare_execution_tx(w3, wallet, holder, target, call_data)
            target_cs = Web3.to_checksum_address(tx_target)

            signed_tx = w3.eth.account.sign_transaction(
                {
                    "to": target_cs,
                    "data": tx_data,
                    "gas": gas_limit,
                    "gasPrice": gas_price,
                    "nonce": base_nonce + i,
                    "chainId": 137,
                },
                PRIVATE_KEY,
            )

            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            sent.append((r, tx_hash, neg_risk))
            send_ok += 1
            if route == "gnosis-safe":
                log.info(f"  🔐 通过 proxy safe 发送: {r.get('slug', '')[:35]}")

        except Exception as e:
            err_msg = str(e).lower()
            if "nonce too low" in err_msg or "already known" in err_msg:
                # nonce 冲突，说明前面的 tx 有问题，后续 nonce 也会失败
                log.warning(f"  ⚠️ nonce 冲突，停止发送: {e}")
                sent.append((r, None, neg_risk))
                break
            log.warning(f"  ⚠️ 发送失败: {r.get('slug', '')[:35]} | {str(e)[:80]}")
            sent.append((r, None, neg_risk))

        # 每 20 笔打印进度
        if (i + 1) % 20 == 0:
            log.info(f"  📤 已发送 {i+1}/{len(selfpay_positions)} ...")

    log.info(f"  📤 发送完毕: {send_ok}/{len(selfpay_positions)} 笔")

    # ── Phase 2: 收集回执 ──
    log.info(f"⏳ Phase 2: 收集回执...")
    results = []
    retry_list = []  # (position, alt_neg_risk)

    for r, tx_hash, neg_risk_used in sent:
        if tx_hash is None:
            results.append({"position": r, "success": False, "tx_hash": None})
            continue

        tx_hex = tx_hash.hex()
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                results.append({"position": r, "success": True, "tx_hash": tx_hex})
            else:
                # revert → 可能 neg_risk 标志错误，加入重试队列
                log.info(f"  🔄 revert: {r.get('slug', '')[:35]} → 加入重试队列")
                results.append({"position": r, "success": False, "tx_hash": tx_hex, "retry": True})
                retry_list.append((r, not neg_risk_used))
        except Exception as e:
            log.warning(f"  ⚠️ 回执获取失败: {tx_hex[:16]}... | {str(e)[:80]}")
            results.append({"position": r, "success": False, "tx_hash": tx_hex})

    success_p2 = sum(1 for x in results if x["success"])
    log.info(f"  📋 Phase 2 结果: {success_p2} 成功 / {len(results) - success_p2} 失败")

    # ── Phase 3: 重试 revert 的（切换 normal↔neg-risk）──
    if retry_list:
        log.info(f"🔄 Phase 3: 重试 {len(retry_list)} 个 revert 持仓（切换合约类型）...")
        for r, alt_neg_risk in retry_list:
            cid = r["condition_id"]
            # 用 redeem_position 的完整重试逻辑（会再次尝试两种方式）
            tx = redeem_position(
                w3, wallet, cid,
                neg_risk=alt_neg_risk,
                balance=r.get("balance", 0),
                holder=r.get("holder", wallet.address),
            )
            # 更新 results 中对应项
            for res in results:
                if (res["position"]["condition_id"] == cid
                        and res.get("retry")):
                    res["success"] = tx is not None
                    res["tx_hash"] = tx
                    res.pop("retry", None)
                    break
            time.sleep(1)

        retry_ok = sum(1 for r, _ in retry_list
                       for res in results
                       if res["position"]["condition_id"] == r["condition_id"] and res["success"])
        log.info(f"  📋 Phase 3 结果: {retry_ok}/{len(retry_list)} 重试成功")

    return relayer_results + results


# ==============================================================================
# 核心逻辑
# ==============================================================================

def _get_usdc_balance_for_address(usdc_contract, address: str) -> float | None:
    try:
        bal = usdc_contract.functions.balanceOf(
            Web3.to_checksum_address(address)
        ).call()
        return bal / 1e6
    except Exception:
        return None


def get_usdc_balances(wallet, usdc_contract) -> dict[str, float | None]:
    """查询 EOA / Proxy / 合计 USDC 余额"""
    eoa_balance = _get_usdc_balance_for_address(usdc_contract, wallet.address)
    proxy_balance = None

    if PROXY_WALLET and PROXY_WALLET.lower() != wallet.address.lower():
        proxy_balance = _get_usdc_balance_for_address(usdc_contract, PROXY_WALLET)

    if eoa_balance is None:
        total_balance = None
    elif proxy_balance is None:
        total_balance = eoa_balance
    else:
        total_balance = eoa_balance + proxy_balance

    return {
        "eoa": eoa_balance,
        "proxy": proxy_balance,
        "total": total_balance,
    }


def _format_usdc_balance_line(label: str, balances: dict[str, float | None]) -> str | None:
    total = balances.get("total")
    eoa_balance = balances.get("eoa")
    proxy_balance = balances.get("proxy")

    if total is None:
        return None
    if proxy_balance is None or eoa_balance is None:
        return f"{label}: ${total:.2f} USDC"
    return (
        f"{label}: EOA ${eoa_balance:.2f} | "
        f"Proxy ${proxy_balance:.2f} | 合计 ${total:.2f} USDC"
    )


def cleanup_false_redeemed(w3: Web3 = None, wallet=None, ctf_contract=None) -> int:
    """
    启动时清理假标记：检查 redeemed_conditions.json 中的记录，
    如果 data-api 仍返回该 condition 为 redeemable，说明未真正领取，移除标记以便重试。
    零 RPC 调用。
    """
    redeemed = load_redeemed()
    if not redeemed:
        return 0

    # 通过 data-api 获取当前仍可领取的 condition
    still_redeemable: set[str] = set()
    proxy_cs = Web3.to_checksum_address(PROXY_WALLET) if PROXY_WALLET else ""
    eoa_cs = Web3.to_checksum_address(EOA_WALLET) if EOA_WALLET else ""

    for addr, label in [(proxy_cs, "proxy"), (eoa_cs, "eoa")]:
        if not addr:
            continue
        for endpoint in ["/positions", "/closed-positions"]:
            try:
                resp = requests.get(
                    f"{DATA_API}{endpoint}",
                    params={"user": addr, "redeemable": "true", "limit": 200, "sizeThreshold": 0},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    for p in data:
                        cid = p.get("conditionId") or p.get("condition_id", "")
                        if cid and float(p.get("size", 0) or 0) > 0:
                            still_redeemable.add(cid)
            except Exception:
                pass

    removed = 0
    to_remove = []

    for cond_id in list(redeemed.keys()):
        if cond_id in still_redeemable:
            slug = redeemed[cond_id].get("slug", cond_id[:18])
            log.info(f"  🔄 清理假标记: {slug} (API 仍显示可领取)")
            to_remove.append(cond_id)

    for cid in to_remove:
        del redeemed[cid]
        removed += 1

    if removed > 0:
        save_redeemed(redeemed)
        log.info(f"🧹 清理了 {removed} 个假标记，将在本轮重新领取")

    return removed


def do_redeem(w3: Web3, wallet, ctf_contract, usdc_contract) -> float:
    """执行一轮领取，返回实际 USDC 到账金额"""

    # 0. 当前余额
    balances_now = get_usdc_balances(wallet, usdc_contract)
    usdc_now = balances_now.get("total")
    balance_line = _format_usdc_balance_line("💰 当前余额", balances_now)
    if balance_line:
        log.info(balance_line)

    # 1. 获取可 redeem 的持仓
    redeemable = find_redeemable(w3, wallet, ctf_contract)

    if not redeemable:
        log.info("✅ 暂无可领取的已结算持仓")
        return 0.0

    # 2. 过滤已 redeem 的
    redeemed = load_redeemed()
    clear_stale_redeemed_marks(redeemed, redeemable)
    pending = [r for r in redeemable if r["condition_id"] not in redeemed]
    skipped = len(redeemable) - len(pending)

    log.info(
        f"📋 链上已结算: {len(redeemable)}，"
        f"待领取: {len(pending)}" + (f"，已跳过: {skipped}" if skipped else "")
    )

    if not pending:
        log.info("✅ 暂无新的已结算头寸可领取")
        return 0.0

    total_value = sum(r["value"] for r in pending)
    preview = ", ".join(r["slug"] for r in pending[:5])
    if len(pending) > 5:
        preview += f" ... 共 {len(pending)} 个"
    log.info(f"🎯 准备领取 {len(pending)} 个头寸，约 ${total_value:.2f} USDC: {preview}")

    # 3. USDC 余额（领取前）
    balances_before = get_usdc_balances(wallet, usdc_contract)
    usdc_before = balances_before.get("total")
    balance_line = _format_usdc_balance_line("💰 领取前余额", balances_before)
    if balance_line:
        log.info(balance_line)

    # 4. 并行 redeem（预分配 nonce + 批量发送 + 统一收回执）
    eoa_cs = Web3.to_checksum_address(wallet.address)
    check_addrs = [eoa_cs]
    if PROXY_WALLET and PROXY_WALLET.lower() != wallet.address.lower():
        check_addrs.append(Web3.to_checksum_address(PROXY_WALLET))

    batch_results = parallel_redeem(w3, wallet, pending)

    # 5. 链上验证 + 标记
    success_count = 0
    fail_count = 0
    successful_positions = []

    for br in batch_results:
        r = br["position"]
        cid = r["condition_id"]
        slug = r.get("slug", cid[:18])
        token_id = r.get("token_id", "")
        balance_before = r.get("balance", 0)

        if not br["success"]:
            fail_count += 1
            continue

        # 验证: tx 成功即视为已领取（Relayer 已确认 / receipt.status==1）
        # 链上 balanceOf 仅作 best-effort 二次验证，超时不阻塞
        actually_redeemed = True
        if token_id:
            try:
                for addr in check_addrs:
                    new_bal = ctf_contract.functions.balanceOf(addr, int(token_id)).call(
                        block_identifier="latest"
                    )
                    if new_bal < balance_before:
                        break
                else:
                    # 所有地址余额未变 — 可能 RPC 缓存延迟，仍标记成功但 warn
                    log.warning(f"  ⚠️ tx成功但余额未变（可能RPC延迟）: {slug}")
            except Exception:
                pass  # RPC 不可达时不阻塞，信任 tx receipt

        success_count += 1
        successful_positions.append(r)
        mark_redeemed(redeemed, cid, slug, r.get("value", 0), r.get("size", 0), br.get("tx_hash", ""))

    # 6. USDC 余额（领取后）+ 统计到账/净收益
    balances_after = get_usdc_balances(wallet, usdc_contract)
    usdc_after = balances_after.get("total")
    settled_amount = 0.0
    if usdc_after is not None and usdc_before is not None:
        settled_amount = usdc_after - usdc_before
    elif usdc_after is not None and usdc_now is not None:
        settled_amount = usdc_after - usdc_now

    net_profit, matched_profit_count = estimate_redeem_profit(successful_positions, settled_amount)

    if settled_amount <= 0.001 and success_count > 0:
        log.info("  💡 全部为输的 token（$0 到账），已清理链上残余余额")

    log.info(
        f"{'✅' if success_count > 0 else '⚠️'} 领取完成: "
        f"{success_count} 成功 / {fail_count} 失败 / {len(pending)} 总计"
    )
    if usdc_after is not None:
        log.info(f"💸 USDC 到账: +${settled_amount:.2f}")
        balance_line = _format_usdc_balance_line("💰 领取后余额", balances_after)
        if balance_line:
            log.info(balance_line)
    if net_profit is not None:
        match_label = f"{matched_profit_count}/{success_count}" if success_count > 0 else "0/0"
        log.info(f"📈 估算净收益: {net_profit:+.2f} USDC | 已匹配本地持仓 {match_label}")
    elif matched_profit_count > 0 and success_count > 0:
        log.info(f"📈 净收益未显示：本地成本只匹配到 {matched_profit_count}/{success_count} 笔")

    # 6. Telegram 通知
    if success_count > 0 and settled_amount > 0:
        balance_line = f"💰 余额: ${usdc_after:.2f} USDC\n" if usdc_after is not None else ""
        payout_line = f"💸 到账: +${settled_amount:.2f} USDC\n"
        profit_line = ""
        if net_profit is not None:
            profit_label = "📈 净收益" if matched_profit_count == success_count else f"📈 净收益(已匹配{matched_profit_count}/{success_count})"
            profit_line = f"{profit_label}: {net_profit:+.2f} USDC\n"
        send_telegram(
            f"💰 <b>Polymarket 链上自动结算</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"✅ 成功: {success_count}/{len(pending)} 笔\n"
            f"{payout_line}"
            f"{profit_line}"
            f"{balance_line}"
            f"📅 时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    return settled_amount

# ==============================================================================
# 主循环
# ==============================================================================

def main() -> None:
    log.info("=" * 55)
    log.info("🔄 Polymarket 链上自动结算 v3.7 启动 (并行结算+链上验证+假标记清理+proxy safe)")
    log.info(f"   RPC         : {RPC_URL}")
    log.info(f"   轮询间隔    : {REDEEM_INTERVAL}s ({REDEEM_INTERVAL // 60} 分钟)")
    log.info(f"   已领取记录  : {REDEEMED_FILE}")
    log.info("=" * 55)

    w3, wallet, ctf_contract, usdc_contract = init_web3()
    log.info(f"   EOA 钱包    : {wallet.address}")
    if PROXY_WALLET:
        log.info(f"   Proxy 钱包  : {PROXY_WALLET}")
    log.info(f"   RPC 已连接   : chainId={w3.eth.chain_id}")

    # 启动时清理假标记（之前 Multicall 可能标记了未真正领取的记录）
    try:
        cleanup_false_redeemed(w3, wallet, ctf_contract)
    except Exception as e:
        log.warning(f"⚠️ 假标记清理异常: {e}")

    total_redeemed = 0.0
    cycle = 0

    while True:
        cycle += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        log.info(f"─── 第 {cycle} 轮 [{now} UTC] ───")

        try:
            amount = do_redeem(w3, wallet, ctf_contract, usdc_contract)
            total_redeemed += amount
            if amount > 0:
                log.info(f"📊 累计已领取: ${total_redeemed:.2f} USDC")
        except ConnectionError as e:
            log.error(f"❌ RPC 连接失败: {e}")
            try:
                w3, wallet, ctf_contract, usdc_contract = init_web3()
                log.info("  🔄 RPC 重连成功")
            except Exception:
                pass
        except Exception as e:
            log.error(f"❌ 本轮异常: {type(e).__name__}: {e}")

        log.info(f"⏳ 等待 {REDEEM_INTERVAL // 60} 分钟后进行下一轮...\n")
        time.sleep(REDEEM_INTERVAL)


if __name__ == "__main__":
    main()
