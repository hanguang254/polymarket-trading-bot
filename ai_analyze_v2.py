#!/usr/bin/env python3
"""
AI 分析和下注决策 v2
- 使用 ai_model_v2 的新策略
- 用期望值（EV）决定是否下注，而不是固定阈值
"""
import sys
import os
import json
from datetime import datetime, timezone

sys.path.insert(0, "/root/.openclaw/workspace/polymarket-arb-bot")

from ai_trader.ai_model_v2 import analyze_market
import subprocess


def log_decision(slug, coin, ptb, direction, confidence, up_odds, down_odds, details, action="SKIP"):
    """记录决策到统计文件"""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "coin": coin,
        "ptb": ptb,
        "direction": direction,
        "confidence": confidence,
        "up_odds": up_odds,
        "down_odds": down_odds,
        "ev": details.get("expected_value", 0),
        "discount": details.get("discount", 0),
        "discount_pct": details.get("discount_pct", 0),
        "estimated_value": details.get("estimated_value", 0),
        "price_diff_pct": details.get("price_diff_pct", 0),
        "diff_in_atr": details.get("diff_in_atr", 0),
        "current_price": details.get("current_price", 0),
        "total_score": details.get("total_score", 0),
        "action": action,
    }

    with open("logs/decisions_v2.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


def analyze_and_decide(coin, price_to_beat, up_odds, down_odds, slug, extra_info=None):
    """
    执行 AI 分析并返回决策

    返回: (should_bet, direction, confidence, details)
    """
    direction, confidence, details = analyze_market(
        coin, price_to_beat, up_odds, down_odds
    )

    if not direction:
        return False, None, 0, details

    # ── 下注条件（v2.1 折价套利策略） ──
    # 核心：折价空间足够大，提前平仓能锁利
    # 1. 折价 >= 12%（保守阈值，回测验证27.3%下注率）
    # 2. EV > 0.05（正期望）
    # 3. 赔率 < 0.85（不买太贵的token）
    # 4. 动量确认（置信度≥50%表示动量方向一致）
    target_odds = details.get("target_odds", up_odds if direction == "UP" else down_odds)
    ev = details.get("expected_value", 0)
    discount = details.get("discount", 0)

    # ── LMSR 流动性评估（替代粗糙的 odds_spread 判断）──
    # 用订单簿深度反推流动性参数 b，计算真实滑点
    liquidity_info = None
    token_id = extra_info.get("token_id") if extra_info else None
    if token_id:
        try:
            from ai_trader.lmsr_liquidity import estimate_lmsr_b, get_dynamic_discount_threshold
            liquidity_info = estimate_lmsr_b(token_id)
            discount_threshold = get_dynamic_discount_threshold(liquidity_info)
            details["lmsr_b"] = liquidity_info["b"]
            details["liquidity_score"] = liquidity_info["liquidity_score"]
            details["spread"] = liquidity_info["spread"]
            details["slippage_5"] = liquidity_info["slippage_5"]
        except Exception:
            liquidity_info = None

    if not liquidity_info:
        # fallback: 原有逻辑
        odds_spread = abs(up_odds - down_odds)
        is_liquid = odds_spread < 0.15
        discount_threshold = 0.10 if is_liquid else 0.15

    # 预热gap趋势覆盖（只能提高阈值，不能降低）
    if extra_info and "min_discount" in extra_info:
        discount_threshold = max(discount_threshold, extra_info["min_discount"])
    
    # 贝叶斯后验覆盖置信度（如果有预热数据）
    bayesian_info = extra_info.get("bayesian") if extra_info else None
    if bayesian_info:
        # 用贝叶斯后验 p_hat 计算真正的 EV = p_hat - price
        p_hat = bayesian_info.get("p_hat", 0.5)
        bayesian_conf = bayesian_info.get("confidence", 0)
        bayesian_dir = bayesian_info.get("direction", direction)

        # 贝叶斯方向与折价方向一致时，增强信号
        if bayesian_dir == direction and bayesian_conf > 0.3:
            # 用贝叶斯后验替代启发式置信度（加权融合）
            confidence = confidence * 0.4 + bayesian_conf * 0.6
            # 论文公式: EV = p̂ - p
            ev_bayesian = p_hat - target_odds
            if ev_bayesian > 0:
                ev = max(ev, ev_bayesian)
                details["ev_bayesian"] = round(ev_bayesian, 4)
            details["confidence_source"] = "bayesian_fused"
        elif bayesian_dir != direction:
            # 贝叶斯方向与折价方向矛盾，降低置信度
            confidence = confidence * 0.5
            details["confidence_source"] = "bayesian_conflict"
        else:
            details["confidence_source"] = "discount_only"

        details["bayesian_p_hat"] = round(p_hat, 4)
        details["bayesian_confidence"] = round(bayesian_conf, 4)
        details["bayesian_direction"] = bayesian_dir

    should_bet = (
        discount >= discount_threshold  # 动态折价阈值
        and ev > 0.1                   # 正期望
        and target_odds < 0.85          # 不买太贵
        and confidence >= 0.65          # 动量确认（65%置信度）
    )

    details["should_bet"] = should_bet
    liq_label = f"LMSR:{liquidity_info['liquidity_score']:.2f}" if liquidity_info else "fallback"
    details["liquidity"] = liq_label
    details["discount_threshold"] = discount_threshold
    details["bet_reason"] = (
        f"折价={discount:.3f}({'✅' if discount>=discount_threshold else '❌'}≥{discount_threshold:.3f}) "
        f"ev={ev:+.3f}({'✅' if ev>0.1 else '❌'}) "
        f"odds={target_odds:.3f}({'✅' if target_odds<0.85 else '❌'}) "
        f"conf={confidence:.0%}({'✅' if confidence>=0.65 else '❌'}≥65%) "
        f"流动性:{liq_label}"
    )

    # 记录决策（在计算 should_bet 之后）
    action = "BET" if should_bet else "SKIP"
    log_decision(slug, coin, price_to_beat, direction, confidence, up_odds, down_odds, details, action)

    return should_bet, direction, confidence, details


def calculate_kelly_size(confidence, ev, balance, target_price=None, p_hat=None):
    """
    修正的 1/4 Kelly 仓位计算（5分钟市场专用）

    正确公式（二元市场）:
        b = (1 - price) / price     — 净赔率
        f* = (p*b - q) / b = (p - price) / (1 - price)
    其中 p 是贝叶斯后验概率，price 是买入价格

    论文注释: "NEVER full Kelly on 5min markets!" → 1/4 Kelly

    Args:
        confidence: 置信度（用于 fallback）
        ev: 期望值
        balance: 当前余额
        target_price: 买入价格（市场赔率）
        p_hat: 贝叶斯后验概率（如果有的话，优先使用）
    """
    if ev <= 0:
        return 5

    # 胜率估计: 优先用贝叶斯后验，否则用 confidence 映射
    if p_hat and p_hat > 0.5:
        p_win = min(p_hat, 0.85)  # 上限保护
    else:
        p_win = 0.5 + (confidence * 0.3)
        p_win = max(0.5, min(0.80, p_win))

    # 买入价格: 优先用实际 target_price
    price = target_price if target_price and 0.01 < target_price < 0.99 else 0.50

    # 正确的二元市场 Kelly 公式:
    # f* = (p - price) / (1 - price)
    kelly_full = (p_win - price) / (1 - price) if price < 1.0 else 0

    if kelly_full <= 0:
        return 5  # 无正期望，最小仓位

    # 1/4 Kelly（论文: NEVER full Kelly on 5min markets!）
    kelly_quarter = kelly_full * 0.25
    kelly_quarter = max(0, min(0.25, kelly_quarter))

    # 转换为份数
    if kelly_quarter <= 0.05:
        size = 5
    elif kelly_quarter < 0.10:
        size = 5
    elif kelly_quarter < 0.15:
        size = 7
    elif kelly_quarter < 0.20:
        size = 8
    else:
        size = 10

    # 余额约束
    if balance < 20:
        max_by_balance = 10
    elif balance < 50:
        max_by_balance = max(5, int(balance * 0.20))
    else:
        max_by_balance = max(5, int(balance * 0.10))
    size = min(size, max_by_balance)

    # 硬约束: 5-10份
    size = max(5, min(10, size))

    print(f"  📊 Kelly仓位: p={p_win:.3f} price={price:.3f} f*={kelly_full:.3f} f/4={kelly_quarter:.3f} → {size}份")

    return size


def execute_bet(slug, direction, token_id, confidence=0.65, ev=0, amount=None, p_hat=None):
    """执行下注（通过 Polymarket CLI）
    
    Args:
        slug: 市场 slug（用于日志）
        direction: UP 或 DOWN
        token_id: 代币 ID
        confidence: AI置信度（用于Kelly仓位计算）
        ev: 期望值（用于Kelly仓位计算）
        amount: 下注金额（美元），None则自动计算
    """
    # 获取当前余额
    balance = 0  # 默认0，必须成功获取才下注
    try:
        result = subprocess.run(
            ["polymarket", "clob", "balance", "--signature-type", "eoa", "--asset-type", "collateral"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            import re
            match = re.search(r'Balance: \$([0-9.]+)', result.stdout)
            if match:
                balance = float(match.group(1))
    except:
        pass

    # 余额不足时直接跳过，不记录为失败
    MIN_BALANCE = float(os.environ.get("MIN_BALANCE", "5.0"))
    if balance < MIN_BALANCE:
        print(f"  ⚠️ 余额不足: ${balance:.2f} < ${MIN_BALANCE:.2f}，跳过下注")
        return False, 0, 0, "SKIP_NO_BALANCE"

    # 获取买入价：用订单簿 best_ask（确保吃单成交），失败回退 midpoint+0.01
    import requests
    price = 0.5
    try:
        resp = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=5)
        if resp.status_code == 200:
            asks = resp.json().get('asks', [])
            if asks:
                price = float(asks[0]['price'])  # 最优卖价即为买方成交价
    except:
        pass

    if price == 0.5:
        # 回退：midpoint + 0.01 滑点
        try:
            resp = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=5)
            if resp.status_code == 200:
                mid = float(resp.json().get('mid', 0.5))
                price = min(round(mid + 0.01, 2), 0.99)
        except:
            pass

    # 价格四舍五入到2位小数（Polymarket要求）
    price = round(price, 2)

    # 安全检查：price 仍为默认值说明订单簿和 midpoint 都获取失败
    if price == 0.5:
        print(f"  ⚠️ 无法获取真实价格（订单簿+midpoint均失败），跳过下注")
        return False, 0, 0, "SKIP_NO_PRICE"

    # Kelly动态仓位（修正公式，传入实际买入价和贝叶斯后验）
    size = calculate_kelly_size(confidence, ev, balance, target_price=price, p_hat=p_hat)
    
    cmd = [
        "polymarket", "clob", "create-order",
        "--signature-type", "eoa",
        "--token", token_id,
        "--side", "buy",
        "--price", str(price),
        "--size", str(size),
    ]

    print(f"  💸 下注命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    
    success = result.returncode == 0
    output = result.stdout if success else result.stderr
    
    # 记录下注结果
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "direction": direction,
        "token_id": token_id,
        "price": price,
        "size": size,
        "amount": price * size,
        "success": success,
        "output": output[:200],  # 截断输出
    }
    
    with open("logs/bets.jsonl", "a") as f:
        f.write(json.dumps(log_entry) + "\n")
    
    # 如果下注成功，记录持仓
    if success:
        position = {
            "token_id": token_id,
            "slug": slug,
            "direction": direction,
            "entry_price": price,
            "size": size,
            "confidence": confidence,
            "ev": ev,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "closed": False
        }
        with open("logs/positions.jsonl", "a") as f:
            f.write(json.dumps(position) + "\n")
    
    return success, price, size, output


if __name__ == "__main__":
    if len(sys.argv) > 1:
        coin = sys.argv[1]
        ptb = float(sys.argv[2])
        up_odds = float(sys.argv[3])
        down_odds = float(sys.argv[4])

        should_bet, direction, confidence, details = analyze_and_decide(
            coin, ptb, up_odds, down_odds, "test"
        )

        print(f"Direction: {direction}")
        print(f"Confidence: {confidence*100:.1f}%")
        print(f"Should bet: {should_bet}")
        print(f"EV: {details.get('expected_value', 0):+.3f}")
        print(f"Details: {json.dumps(details, indent=2)}")
