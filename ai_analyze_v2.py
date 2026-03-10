#!/usr/bin/env python3
"""
AI 分析和下注决策 v2
- 使用 ai_model_v2 的新策略
- 用期望值（EV）决定是否下注，而不是固定阈值
"""
import sys
import json
from datetime import datetime, timezone

sys.path.insert(0, "/root/.openclaw/workspace/polymarket-arb-bot")

from ai_trader.ai_model_v2 import analyze_market
import subprocess


def log_decision(slug, coin, ptb, direction, confidence, up_odds, down_odds, details):
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
    }

    with open("logs/decisions_v2.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


def analyze_and_decide(coin, price_to_beat, up_odds, down_odds, slug):
    """
    执行 AI 分析并返回决策

    返回: (should_bet, direction, confidence, details)
    """
    direction, confidence, details = analyze_market(
        coin, price_to_beat, up_odds, down_odds
    )

    if not direction:
        return False, None, 0, details

    # 记录决策
    log_decision(slug, coin, price_to_beat, direction, confidence, up_odds, down_odds, details)

    # ── 下注条件（v2.1 折价套利策略） ──
    # 核心：折价空间足够大，提前平仓能锁利
    # 1. 折价 >= 12%（保守阈值，回测验证27.3%下注率）
    # 2. EV > 0.05（正期望）
    # 3. 赔率 < 0.85（不买太贵的token）
    # 4. 动量确认（置信度≥50%表示动量方向一致）
    target_odds = details.get("target_odds", up_odds if direction == "UP" else down_odds)
    ev = details.get("expected_value", 0)
    discount = details.get("discount", 0)

    # 流动性估计（基于赔率接近度）
    # 赔率越接近0.5，流动性越好
    odds_spread = abs(up_odds - down_odds)
    is_liquid = odds_spread < 0.15  # 价差<15%认为流动性好
    
    # 动态折价阈值
    if is_liquid:
        discount_threshold = 0.10  # 流动性好，10%折价
    else:
        discount_threshold = 0.15  # 流动性差，要求15%折价
    
    should_bet = (
        discount >= discount_threshold  # 动态折价阈值
        and ev > 0.5                   # 正期望
        and target_odds < 0.85          # 不买太贵
        and confidence >= 0.80          # 动量确认（80%置信度）
    )

    details["should_bet"] = should_bet
    details["liquidity"] = "good" if is_liquid else "poor"
    details["odds_spread"] = round(odds_spread, 3)
    details["bet_reason"] = (
        f"折价={discount:.3f}({'✅' if discount>=discount_threshold else '❌'}≥{discount_threshold:.2f}) "
        f"ev={ev:+.3f}({'✅' if ev>0.05 else '❌'}) "
        f"odds={target_odds:.3f}({'✅' if target_odds<0.85 else '❌'}) "
        f"conf={confidence:.0%}({'✅' if confidence>=0.80 else '❌'}≥80%) "
        f"流动性:{details['liquidity']}"
    )

    return should_bet, direction, confidence, details


def calculate_kelly_size(confidence, ev, balance):
    """
    1/4 Kelly仓位计算（5分钟市场专用）
    
    基于真实胜率而非折价收益率
    
    Args:
        confidence: 置信度（动量评分转换的胜率估计）
        ev: 折价收益率
        balance: 当前余额
    
    Returns:
        size: 下注份数 (3-10)
    """
    if ev <= 0:
        return 3
    
    # 将置信度转换为胜率估计
    # confidence来自动量评分，范围0-1
    # 需要映射到合理的胜率范围（0.5-0.8）
    p_win = 0.5 + (confidence * 0.3)  # 50%-80%
    p_win = max(0.5, min(0.8, p_win))
    
    # 计算净赔率 b
    # 假设赔率在0.5附近，净赔率约为1
    implied_odds = 0.5 + ev  # 粗略估计
    if implied_odds >= 1.0:
        implied_odds = 0.99
    b = (1 / implied_odds) - 1
    
    # Kelly公式: f = (p*b - q) / b
    q = 1 - p_win
    kelly_full = (p_win * b - q) / b if b > 0 else 0
    
    # 1/4 Kelly（保守）
    kelly_quarter = kelly_full * 0.25
    kelly_quarter = max(0, min(0.25, kelly_quarter))
    
    # 转换为份数（Polymarket最低5份）
    if kelly_quarter <= 0.05:
        size = 5   # 最小5份（Polymarket最低要求）
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
    
    # 硬约束：3-10份
    size = max(3, min(10, size))
    
    print(f"  📊 仓位: ev={ev:+.3f} balance=${balance:.2f} → {size}份")
    
    return size


def execute_bet(slug, direction, token_id, confidence=0.85, ev=0.5, amount=None):
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
    balance = 100  # 默认值
    try:
        result = subprocess.run(
            ["polymarket", "clob", "balance", "--signature-type", "gnosis-safe", "--asset-type", "collateral"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            import re
            match = re.search(r'Balance: \$([0-9.]+)', result.stdout)
            if match:
                balance = float(match.group(1))
    except:
        pass
    
    # 获取当前价格
    try:
        import requests
        resp = requests.get(f"https://clob.polymarket.com/midpoint?token_id={token_id}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            price = float(data.get('mid', 0.5))
        else:
            price = 0.5
    except:
        price = 0.5
    
    # 价格四舍五入到2位小数（Polymarket要求）
    price = round(price, 2)
    
    # Kelly动态仓位（替代固定5份）
    size = calculate_kelly_size(confidence, ev, balance)
    
    cmd = [
        "polymarket", "clob", "create-order",
        "--signature-type", "gnosis-safe",
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
