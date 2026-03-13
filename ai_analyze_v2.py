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


from ai_trader.ai_model_v2 import analyze_market
from ai_trader.base_rate import get_base_rate
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

    # Bug 7 fix: 方向确定后选择正确的 token_id 做 LMSR 评估
    if extra_info and direction:
        if direction == "UP" and extra_info.get("up_token"):
            extra_info["token_id"] = extra_info["up_token"]
        elif direction == "DOWN" and extra_info.get("down_token"):
            extra_info["token_id"] = extra_info["down_token"]

    # ── 下注条件（v2.1 折价套利策略） ──
    # 核心：折价空间足够大，提前平仓能锁利
    # 1. 折价 >= 12%（保守阈值，回测验证27.3%下注率）
    # 2. EV > 0.05（正期望）
    # 3. 赔率 < 0.85（不买太贵的token）
    # 4. 动量确认（置信度≥50%表示动量方向一致）
    target_odds = details.get("target_odds", up_odds if direction == "UP" else down_odds)
    discount = details.get("discount", 0)

    # ── Base Rate 校准 (P0) ──
    diff_in_atr = details.get("diff_in_atr", 0)
    base_rate = get_base_rate(diff_in_atr)
    details["base_rate"] = round(base_rate, 4)

    # Kelly 缩减：base_rate < 0.55 表示无统计优势，仓位减半
    kelly_reduction = 0.5 if base_rate < 0.55 else 1.0
    details["kelly_reduction"] = kelly_reduction

    # ── LMSR 流动性评估 ──
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
        odds_spread = abs(up_odds - down_odds)
        is_liquid = odds_spread < 0.15
        discount_threshold = 0.10 if is_liquid else 0.15

    # 预热gap趋势覆盖（只能提高阈值，不能降低）
    if extra_info and "min_discount" in extra_info:
        discount_threshold = max(discount_threshold, extra_info["min_discount"])

    # ── 严格概率 EV 公式 (P2) ──
    # p_win = base_rate（保守先验），贝叶斯可用时加权融合
    p_win = details.get("p_win", base_rate)

    # 贝叶斯后验融合
    bayesian_info = extra_info.get("bayesian") if extra_info else None
    if bayesian_info:
        b_p_hat = bayesian_info.get("p_hat", 0.5)
        bayesian_conf = bayesian_info.get("confidence", 0)
        bayesian_dir = bayesian_info.get("direction", direction)

        if bayesian_dir == direction and bayesian_conf > 0.3:
            # 贝叶斯有实时数据，权重更高
            p_win = p_win * 0.4 + b_p_hat * 0.6
            confidence = confidence * 0.4 + bayesian_conf * 0.6
            details["confidence_source"] = "bayesian_fused"
        elif bayesian_dir != direction:
            confidence = confidence * 0.5
            details["confidence_source"] = "bayesian_conflict"
        else:
            details["confidence_source"] = "discount_only"

        details["bayesian_p_hat"] = round(b_p_hat, 4)
        details["bayesian_confidence"] = round(bayesian_conf, 4)
        details["bayesian_direction"] = bayesian_dir

    p_win = min(p_win, 0.82)  # P1: 防止贝叶斯累积推高 p_win

    # 交叉验证：estimated_value 比 p_win 高 0.15+ → 高估警告
    estimated_value = details.get("estimated_value", 0.5)
    if estimated_value > p_win + 0.15:
        details["overestimated"] = True
        confidence *= 0.85

    # #7: 无效率信号 — 实时 best_ask vs p_win 偏离
    if liquidity_info and liquidity_info.get("best_ask"):
        realtime_ask = liquidity_info["best_ask"]
        inefficiency = p_win - realtime_ask
        details["inefficiency"] = round(inefficiency, 4)
        details["realtime_ask"] = realtime_ask
        # 强无效率: 市场明显错价，降低入场门槛
        if inefficiency > 0.10:
            old_threshold = discount_threshold
            discount_threshold = max(discount_threshold - 0.02, 0.06)
            details["inefficiency_boost"] = True
            print(f"  📊 #7无效率信号: p_win={p_win:.3f} vs ask={realtime_ask:.3f} 偏离={inefficiency:.3f} → 阈值{old_threshold:.3f}→{discount_threshold:.3f}")

    # 严格二元 EV: p_win - price
    ev = p_win - target_odds
    details["expected_value"] = round(ev, 4)
    details["p_win_final"] = round(p_win, 4)
    details["ev_positive"] = ev > 0

    MAX_PRICE = float(os.environ.get("MAX_BUY_PRICE", "0.92"))
    should_bet = (
        discount >= discount_threshold  # 动态折价阈值
        and ev > 0.03                  # 严格EV: 至少3%边际（新公式产出较小）
        and target_odds < MAX_PRICE     # 不买太贵（env可配置）
        and confidence >= 0.65          # 动量确认
    )

    details["should_bet"] = should_bet
    liq_label = f"LMSR:{liquidity_info['liquidity_score']:.2f}" if liquidity_info else "fallback"
    details["liquidity"] = liq_label
    details["discount_threshold"] = discount_threshold
    details["bet_reason"] = (
        f"折价={discount:.3f}({'✅' if discount>=discount_threshold else '❌'}≥{discount_threshold:.3f}) "
        f"ev={ev:+.4f}({'✅' if ev>0.03 else '❌'}>0.03) "
        f"p_win={p_win:.3f} base_rate={base_rate:.3f} "
        f"odds={target_odds:.3f}({'✅' if target_odds<MAX_PRICE else '❌'}<{MAX_PRICE}) "
        f"conf={confidence:.0%}({'✅' if confidence>=0.65 else '❌'}≥65%) "
        f"流动性:{liq_label}"
    )

    action = "BET" if should_bet else "SKIP"
    log_decision(slug, coin, price_to_beat, direction, confidence, up_odds, down_odds, details, action)

    return should_bet, direction, confidence, details


def check_bid_depth(token_id):
    """P2: 检查token的买方深度（bid-side总量），评估退出流动性"""
    import requests
    try:
        resp = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=5)
        if resp.status_code == 200:
            bids = resp.json().get('bids', [])
            total_depth = sum(float(b['size']) for b in bids)
            return total_depth
    except:
        pass
    return None


def calculate_kelly_size(confidence, ev, balance, target_price=None, p_hat=None,
                          p_win=None, kelly_reduction=1.0, exit_bid_depth=None):
    """
    修正的 1/4 Kelly 仓位计算（5分钟市场专用）

    正确公式（二元市场）:
        f* = (p - price) / (1 - price)

    论文注释: "NEVER full Kelly on 5min markets!" → 1/4 Kelly

    Args:
        confidence: 置信度（用于 fallback）
        ev: 期望值
        balance: 当前余额
        target_price: 买入价格（市场赔率）
        p_hat: 贝叶斯后验概率
        p_win: 严格概率估计（来自 base_rate + 贝叶斯融合，优先级最高）
        kelly_reduction: 缩减因子（base_rate < 0.55 → 0.5, 相关性 → 0.5）
    """
    if ev <= 0:
        return 5

    # 胜率估计优先级: p_win > p_hat > confidence映射
    if p_win and p_win > 0.5:
        p = min(p_win, 0.85)
    elif p_hat and p_hat > 0.5:
        p = min(p_hat, 0.85)
    else:
        p = 0.5 + (confidence * 0.3)
        p = max(0.5, min(0.80, p))

    # 买入价格
    price = target_price if target_price and 0.01 < target_price < 0.99 else 0.50

    # 二元市场 Kelly: f* = (p - price) / (1 - price)
    kelly_full = (p - price) / (1 - price) if price < 1.0 else 0

    if kelly_full <= 0:
        return 5

    # 1/4 Kelly + 缩减因子
    kelly_quarter = kelly_full * 0.25 * kelly_reduction
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

    # P3: 流动性上限 — 不超过退出流动性的50%
    if exit_bid_depth and exit_bid_depth > 0:
        max_by_liquidity = max(5, int(exit_bid_depth * 0.5))
        if size > max_by_liquidity:
            print(f"  📊 P3流动性上限: bid_depth={exit_bid_depth:.1f} → max={max_by_liquidity}份")
            size = min(size, max_by_liquidity)

    # 硬约束: 5-10份
    size = max(5, min(10, size))

    red_label = f" red={kelly_reduction}" if kelly_reduction < 1.0 else ""
    liq_label = f" liq_cap={exit_bid_depth:.0f}" if exit_bid_depth else ""
    print(f"  📊 Kelly仓位: p={p:.3f} price={price:.3f} f*={kelly_full:.3f} f/4={kelly_quarter:.3f}{red_label}{liq_label} → {size}份")

    return size


def execute_bet(slug, direction, token_id, confidence=0.65, ev=0, amount=None,
                 p_hat=None, entry_details=None, kelly_reduction=1.0):
    """执行下注（通过 Polymarket CLI）

    Args:
        slug: 市场 slug
        direction: UP 或 DOWN
        token_id: 代币 ID
        confidence: AI置信度
        ev: 期望值
        amount: 下注金额（美元），None则自动计算
        p_hat: 贝叶斯后验概率
        entry_details: 完整分析详情（用于持仓记录丰富字段）
        kelly_reduction: Kelly 缩减因子（base_rate/相关性）
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

    # 安全检查：实际买入价不能超过上限（env可配置，默认0.92）
    MAX_PRICE = float(os.environ.get("MAX_BUY_PRICE", "0.92"))
    if price >= MAX_PRICE:
        print(f"  ⚠️ 实际买入价${price:.2f}≥${MAX_PRICE}，上行空间不足，跳过下注")
        return False, 0, 0, "SKIP_PRICE_TOO_HIGH"

    # P2: 退出流动性检查 — 下注前检查能否卖出
    bid_depth = check_bid_depth(token_id)
    if bid_depth is not None:
        print(f"  📊 P2退出流动性: bid_depth={bid_depth:.1f}份")
        if bid_depth < 5:
            print(f"  ⚠️ 退出流动性极低({bid_depth:.1f}<5)，跳过下注")
            return False, 0, 0, "SKIP_NO_EXIT_LIQUIDITY"

    # Kelly动态仓位（修正公式 + base_rate/相关性缩减 + P3流动性上限）
    p_win_final = entry_details.get("p_win_final") if entry_details else None
    size = calculate_kelly_size(
        confidence, ev, balance, target_price=price, p_hat=p_hat,
        p_win=p_win_final, kelly_reduction=kelly_reduction,
        exit_bid_depth=bid_depth
    )
    
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

    output = result.stdout if result.returncode == 0 else result.stderr

    # 解析CLI输出，提取实际成交状态和价格
    from position_monitor import parse_order_output
    info = parse_order_output(output) if result.returncode == 0 else {}

    # 仅 MATCHED 视为成功（LIVE=挂单未成交，不记录持仓）
    success = result.returncode == 0 and info.get("matched", False)

    # 计算实际成交价：BUY 用 Making/Size（USDC花费/token数），SELL 用 Taking/Size
    # BUY 时 Taking=收到的token数(≈size)，Making=花费的USDC；之前误用 Taking/Size ≈ 1.0
    if success and size > 0 and info.get("making", 0) > 0:
        actual_price = round(info["making"] / size, 4)
        print(f"  📊 成交确认: Status={info['status']} | Making=${info['making']:.4f} | Taking=${info.get('taking', 0):.4f} | 实际价=${actual_price:.4f} (限价=${price})")
    else:
        actual_price = price  # 回退：解析失败时用限价

    if not success and result.returncode == 0:
        # returncode=0 但未 MATCHED（LIVE 挂单）
        print(f"  ⏳ 挂单未成交: Status={info.get('status')} | 不记录持仓")

    # 记录下注结果（使用实际成交价）
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "direction": direction,
        "token_id": token_id,
        "price": actual_price,
        "limit_price": price,
        "size": size,
        "amount": actual_price * size,
        "success": success,
        "output": output[:200],  # 截断输出
    }

    with open("logs/bets.jsonl", "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    # 如果下注成功，记录持仓（丰富字段供 position_monitor 使用）
    if success:
        position = {
            "token_id": token_id,
            "slug": slug,
            "direction": direction,
            "entry_price": actual_price,
            "size": size,
            "confidence": confidence,
            "ev": ev,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "closed": False,
        }
        # 从 entry_details 提取关键字段（供 monitor 的 EV 计算使用）
        if entry_details:
            for key in ("price_to_beat", "atr", "estimated_value", "diff_in_atr",
                         "base_rate", "p_win_final"):
                if key in entry_details:
                    position[key] = entry_details[key]
            # 兼容字段名
            if "atr" in entry_details:
                position["atr_val"] = entry_details["atr"]
            if "price_to_beat" in entry_details:
                position["ptb"] = entry_details["price_to_beat"]
            # P1: 反向token_id（供对冲使用）
            if "opposite_token_id" in entry_details:
                position["opposite_token_id"] = entry_details["opposite_token_id"]
        with open("logs/positions.jsonl", "a") as f:
            f.write(json.dumps(position) + "\n")

    return success, actual_price, size, output


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
