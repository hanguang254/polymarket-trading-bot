#!/usr/bin/env python3
"""
Polymarket 自动领取已结算收益脚本 v3.3
- data-api REST 查持仓 + web3 EOA 直接链上结算（不走 Gnosis Safe）
- EOA 直接调用 CTF.redeemPositions / NegRiskAdapter.redeemPositions
- 支持 normal / neg-risk 自动切换重试
- 链上 payoutDenominator 验证已结算 + balanceOf 检查余额
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
if not PROXY_WALLET or not PRIVATE_KEY:
    raise RuntimeError("PROXY_WALLET and PRIVATE_KEY must be set in .env")
RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

REDEEM_INTERVAL = int(os.environ.get("REDEEM_INTERVAL", 600))  # 默认10分钟

# 已 redeem 记录文件
REDEEMED_FILE = os.path.join(SCRIPT_DIR, "logs", "redeemed_conditions.json")

# ==============================================================================
# 合约地址 (Polygon mainnet)
# ==============================================================================

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"       # Conditional Tokens Framework
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # NegRiskAdapter
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"      # USDC (PoS) on Polygon
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11" # Multicall3 (Polygon)

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


def _fetch_closed_positions_api(addr: str) -> list:
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
            log.info(f"  已关闭持仓: {len(positions)} 个")
        return positions
    except Exception as e:
        log.warning(f"  已关闭持仓查询失败: {str(e)[:100]}")
        return []


def fetch_positions() -> list:
    """通过 Polymarket data-api 获取钱包持仓（活跃+已关闭）"""
    all_positions = []

    for addr, label in [(PROXY_WALLET, "proxy"), (EOA_WALLET, "eoa")]:
        if not addr:
            continue
        all_positions.extend(_fetch_positions_api(addr, label))

    # 也查 closed-positions
    if PROXY_WALLET:
        all_positions.extend(_fetch_closed_positions_api(PROXY_WALLET))

    return all_positions


# ==============================================================================
# 链上验证
# ==============================================================================

def check_resolved_onchain(w3: Web3, cond_id: str) -> bool:
    """链上检查 condition 是否已结算: payoutDenominator > 0"""
    selector = w3.keccak(text="payoutDenominator(bytes32)")[:4]
    call_data = selector + abi_encode(
        ["bytes32"], [bytes.fromhex(cond_id.replace("0x", ""))]
    )
    result = w3.eth.call(
        {"to": Web3.to_checksum_address(CTF_ADDRESS), "data": call_data}
    )
    denominator = int(result.hex(), 16)
    return denominator > 0


def find_redeemable(w3: Web3, wallet, ctf_contract) -> list[dict]:
    """data-api 获取持仓 -> 字段预筛 -> 链上验证已结算 + 余额 > 0"""
    eoa_cs = Web3.to_checksum_address(wallet.address)
    # 检查余额的地址列表：EOA 优先，proxy 备选
    check_addrs = [eoa_cs]
    if PROXY_WALLET and PROXY_WALLET.lower() != wallet.address.lower():
        check_addrs.append(Web3.to_checksum_address(PROXY_WALLET))

    # 1. data-api 获取持仓
    positions = fetch_positions()
    if not positions:
        return []

    # 2. 字段预筛已结算的（减少链上调用）
    redeemable = []
    seen_conditions = set()

    for p in positions:
        cond_id = p.get("conditionId") or p.get("condition_id", "")
        token_id = p.get("asset") or p.get("token_id", "")
        title = p.get("title", p.get("slug", p.get("market_slug", cond_id[:16])))
        neg_risk = p.get("negativeRisk", p.get("neg_risk", p.get("negRisk", False)))
        cur_value = float(p.get("currentValue", p.get("current_value", p.get("value", 0))) or 0)
        size = float(p.get("size", 0) or 0)

        if not cond_id or cond_id in seen_conditions:
            continue
        seen_conditions.add(cond_id)

        # API 字段预筛：只对可能已结算的做链上验证
        is_likely_resolved = (
            p.get("resolved") is True
            or p.get("redeemable") is True
            or p.get("game_status") == "resolved"
            or p.get("status") == "resolved"
            or p.get("outcome") not in (None, "", "pending")
        )
        if not is_likely_resolved:
            continue

        # 3. 链上确认已结算
        try:
            if not check_resolved_onchain(w3, cond_id):
                continue
        except Exception as e:
            log.debug(f"  链上检查失败: {cond_id[:18]}... | {e}")
            continue

        # 4. 链上检查 token 余额（先查 EOA，再查 proxy）
        bal = 0
        holder = eoa_cs
        if token_id:
            for addr in check_addrs:
                try:
                    b = ctf_contract.functions.balanceOf(addr, int(token_id)).call()
                    if b > 0:
                        bal = b
                        holder = addr
                        break
                except Exception:
                    continue
            if bal == 0:
                continue
        else:
            bal = int(size * 1e6) if size > 0 else 0

        # 价值过小的不领取（Multicall批量gas均摊，阈值可以很低）
        if bal < 10_000:  # < 0.01 USDC
            continue

        redeemable.append({
            "condition_id": cond_id,
            "token_id": token_id,
            "balance": bal,
            "holder": holder,
            "slug": (title or "unknown")[:50],
            "value": cur_value,
            "neg_risk": neg_risk,
            "size": size,
        })
        log.info(f"  可领取: {(title or '')[:40]} (balance: {bal}, holder: {holder[:10]}...)")

    return redeemable

# ==============================================================================
# EOA 直接链上 Redeem
# ==============================================================================

def _send_tx(w3: Web3, wallet, target: str, call_data: bytes) -> str | None:
    """
    EOA 直接发送交易到目标合约
    gasPrice 加 1.3x 倍率应对 Polygon 拥堵；超时后回查 receipt 兜底。

    Returns: tx_hash hex string on success, None on failure
    """
    target_cs = Web3.to_checksum_address(target)

    gas_estimate = w3.eth.estimate_gas(
        {"from": wallet.address, "to": target_cs, "data": call_data}
    )

    tx = w3.eth.account.sign_transaction(
        {
            "to": target_cs,
            "data": call_data,
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
        # 超时：交易可能还在 mempool，回查一次 receipt
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt and receipt.status == 1:
                log.info(f"  ✅ 超时但交易已上链: {tx_hash.hex()}")
                return tx_hash.hex()
        except Exception:
            pass
        # 真的没上链，抛出带 tx_hash 的异常供上层判断
        raise TimeoutError(f"tx_pending:{tx_hash.hex()}")

    if receipt.status == 1:
        return tx_hash.hex()
    return None


def redeem_normal(w3: Web3, wallet, cond_id: str) -> str | None:
    """
    普通市场 redeem: CTF.redeemPositions(collateral, parentCollectionId, conditionId, indexSets)
    EOA 直接调用 CTF 合约
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

    return _send_tx(w3, wallet, CTF_ADDRESS, call_data)


def redeem_neg_risk(w3: Web3, wallet, cond_id: str, amounts: list[int]) -> str | None:
    """
    neg-risk 市场 redeem: NegRiskAdapter.redeemPositions(conditionId, amounts)
    EOA 直接调用 NegRiskAdapter
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

    return _send_tx(w3, wallet, NEG_RISK_ADAPTER, call_data)


def redeem_position(w3: Web3, wallet, cond_id: str,
                    neg_risk: bool = False, balance: int = 0) -> str | None:
    """
    执行单个 condition 的链上 redeem（EOA 直接调用）
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
            tx = redeem_neg_risk(w3, wallet, cond_id, [amt, amt])
        else:
            tx = redeem_normal(w3, wallet, cond_id)
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
            tx = redeem_normal(w3, wallet, cond_id)
        else:
            tx = redeem_neg_risk(w3, wallet, cond_id, [amt, amt])
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
# Multicall3 批量 Redeem
# ==============================================================================

MULTICALL_BATCH_SIZE = int(os.environ.get("REDEEM_BATCH_SIZE", "20"))  # 每批最多条数


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


def batch_redeem(w3: Web3, wallet, positions: list[dict]) -> list[dict]:
    """
    使用 Multicall3.aggregate3 批量 redeem 多个持仓（单笔交易）。
    aggregate3 允许单个子调用失败不影响整批（allowFailure=True）。

    Args:
        positions: find_redeemable 返回的列表

    Returns:
        list of {condition_id, success, tx_hash} for each position
    """
    if not positions:
        return []

    results = []

    # 分批处理
    for batch_start in range(0, len(positions), MULTICALL_BATCH_SIZE):
        batch = positions[batch_start:batch_start + MULTICALL_BATCH_SIZE]
        batch_num = batch_start // MULTICALL_BATCH_SIZE + 1
        total_batches = (len(positions) + MULTICALL_BATCH_SIZE - 1) // MULTICALL_BATCH_SIZE

        log.info(f"📦 Multicall 批次 {batch_num}/{total_batches}: {len(batch)} 个持仓")

        # 构建 aggregate3 的 Call3[] 参数
        # struct Call3 { address target; bool allowFailure; bytes callData; }
        calls = []
        for r in batch:
            try:
                target, call_data = _build_redeem_calldata(
                    w3, r["condition_id"],
                    neg_risk=r.get("neg_risk", False),
                    balance=r.get("balance", 0),
                )
                calls.append((Web3.to_checksum_address(target), True, call_data))
            except Exception as e:
                log.warning(f"  ⚠️ 构建calldata失败: {r['condition_id'][:18]}... | {e}")
                results.append({"condition_id": r["condition_id"], "success": False, "tx_hash": None})

        if not calls:
            continue

        # Multicall3.aggregate3(Call3[]) selector
        selector = w3.keccak(text="aggregate3((address,bool,bytes)[])")[:4]
        mc_calldata = selector + abi_encode(
            ["(address,bool,bytes)[]"],
            [calls],
        )

        mc_target = Web3.to_checksum_address(MULTICALL3_ADDRESS)

        try:
            gas_estimate = w3.eth.estimate_gas(
                {"from": wallet.address, "to": mc_target, "data": mc_calldata}
            )

            tx = w3.eth.account.sign_transaction(
                {
                    "to": mc_target,
                    "data": mc_calldata,
                    "gas": gas_estimate + 100_000,  # 批量操作多留 buffer
                    "gasPrice": int(w3.eth.gas_price * 1.3),
                    "nonce": w3.eth.get_transaction_count(wallet.address, "pending"),
                    "chainId": 137,
                },
                PRIVATE_KEY,
            )

            tx_hash = w3.eth.send_raw_transaction(tx.raw_transaction)
            tx_hex = tx_hash.hex()
            log.info(f"  🚀 Multicall tx 已发送: {tx_hex}")

            try:
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            except Exception:
                try:
                    receipt = w3.eth.get_transaction_receipt(tx_hash)
                    if receipt and receipt.status == 1:
                        log.info(f"  ✅ 超时但交易已上链: {tx_hex}")
                except Exception:
                    receipt = None

                if not receipt or receipt.status != 1:
                    log.warning(f"  ⚠️ Multicall tx pending/失败: {tx_hex}")
                    # 整批标记失败，下轮重试
                    for r in batch:
                        if not any(x["condition_id"] == r["condition_id"] for x in results):
                            results.append({"condition_id": r["condition_id"], "success": False, "tx_hash": tx_hex})
                    continue

            if receipt.status == 1:
                # aggregate3 成功：解析返回值判断每个子调用结果
                # 返回值: Result[] = (bool success, bytes returnData)[]
                # 即使整体 tx 成功，个别子调用可能 revert（allowFailure=True）
                try:
                    # 整体 tx 成功 → 标记全部成功
                    # Multicall3 aggregate3 的子调用失败会静默（allowFailure=True），
                    # 但链上余额会反映实际结果，下轮 find_redeemable 会重新检测未成功的
                    for r in batch:
                        if not any(x["condition_id"] == r["condition_id"] for x in results):
                            results.append({"condition_id": r["condition_id"], "success": True, "tx_hash": tx_hex})
                    log.info(f"  ✅ Multicall 批次{batch_num}成功: {len(batch)} 个持仓 | gas: {receipt.gasUsed}")
                except Exception:
                    for r in batch:
                        if not any(x["condition_id"] == r["condition_id"] for x in results):
                            results.append({"condition_id": r["condition_id"], "success": True, "tx_hash": tx_hex})
            else:
                log.warning(f"  ❌ Multicall tx revert: {tx_hex}")
                for r in batch:
                    if not any(x["condition_id"] == r["condition_id"] for x in results):
                        results.append({"condition_id": r["condition_id"], "success": False, "tx_hash": tx_hex})

        except Exception as e:
            err_msg = str(e)
            log.warning(f"  ❌ Multicall 批次{batch_num}失败: {err_msg[:150]}")

            # Multicall 失败 → fallback 逐个 redeem
            if "execution reverted" in err_msg.lower() or "gas" in err_msg.lower():
                log.info(f"  🔄 Fallback: 逐个 redeem {len(batch)} 个持仓...")
                for r in batch:
                    tx = redeem_position(
                        w3, wallet, r["condition_id"],
                        neg_risk=r.get("neg_risk", False),
                        balance=r.get("balance", 0),
                    )
                    results.append({
                        "condition_id": r["condition_id"],
                        "success": tx is not None,
                        "tx_hash": tx,
                    })
                    time.sleep(3)
            else:
                for r in batch:
                    results.append({"condition_id": r["condition_id"], "success": False, "tx_hash": None})

        # 批次间间隔
        if batch_start + MULTICALL_BATCH_SIZE < len(positions):
            time.sleep(3)

    return results


# ==============================================================================
# 核心逻辑
# ==============================================================================

def get_usdc_balance(wallet, usdc_contract) -> float:
    """查询 EOA 钱包 USDC 余额"""
    try:
        bal = usdc_contract.functions.balanceOf(
            Web3.to_checksum_address(wallet.address)
        ).call()
        return bal / 1e6
    except Exception:
        return -1


def do_redeem(w3: Web3, wallet, ctf_contract, usdc_contract) -> float:
    """执行一轮领取，返回实际 USDC 收益"""

    # 0. 当前余额
    usdc_now = get_usdc_balance(wallet, usdc_contract)
    if usdc_now >= 0:
        log.info(f"💰 当前余额: ${usdc_now:.2f} USDC")

    # 1. 获取可 redeem 的持仓
    redeemable = find_redeemable(w3, wallet, ctf_contract)

    if not redeemable:
        log.info("✅ 暂无可领取的已结算持仓")
        return 0.0

    # 2. 过滤已 redeem 的
    redeemed = load_redeemed()
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
    usdc_before = get_usdc_balance(wallet, usdc_contract)
    if usdc_before >= 0:
        log.info(f"💰 领取前余额: ${usdc_before:.2f} USDC")

    # 4. Multicall3 批量 redeem（单笔交易处理多个持仓）
    success_count = 0
    fail_count = 0

    batch_results = batch_redeem(w3, wallet, pending)

    for i, br in enumerate(batch_results):
        cid = br["condition_id"]
        if br["success"]:
            success_count += 1
            r = pending[i] if i < len(pending) else {}
            mark_redeemed(redeemed, cid, r.get("slug", ""), r.get("value", 0), r.get("size", 0), br.get("tx_hash", ""))
        else:
            fail_count += 1

    # 5. USDC 余额（领取后）
    usdc_after = get_usdc_balance(wallet, usdc_contract)
    gained = (usdc_after - usdc_before) if usdc_before >= 0 and usdc_after >= 0 else 0

    log.info(
        f"{'✅' if success_count > 0 else '⚠️'} 领取完成: "
        f"{success_count} 成功 / {fail_count} 失败 / {len(pending)} 总计"
    )
    if usdc_after >= 0:
        log.info(f"💵 USDC 变化: +${gained:.2f}  |  余额: ${usdc_after:.2f}")

    # 6. Telegram 通知
    if success_count > 0 and gained > 0:
        balance_line = f"💰 余额: ${usdc_after:.2f} USDC\n" if usdc_after >= 0 else ""
        send_telegram(
            f"💰 <b>Polymarket 链上自动结算</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"✅ 成功: {success_count}/{len(pending)} 笔\n"
            f"💵 收益: +${gained:.2f} USDC\n"
            f"{balance_line}"
            f"📅 时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    return gained

# ==============================================================================
# 主循环
# ==============================================================================

def main() -> None:
    log.info("=" * 55)
    log.info("🔄 Polymarket 链上自动结算 v3.4 启动 (Multicall3批量结算+EOA直接结算)")
    log.info(f"   RPC         : {RPC_URL}")
    log.info(f"   轮询间隔    : {REDEEM_INTERVAL}s ({REDEEM_INTERVAL // 60} 分钟)")
    log.info(f"   已领取记录  : {REDEEMED_FILE}")
    log.info("=" * 55)

    w3, wallet, ctf_contract, usdc_contract = init_web3()
    log.info(f"   EOA 钱包    : {wallet.address}")
    if PROXY_WALLET:
        log.info(f"   Proxy 钱包  : {PROXY_WALLET}")
    log.info(f"   RPC 已连接   : chainId={w3.eth.chain_id}")

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
