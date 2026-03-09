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

    should_bet = (
        discount >= 0.10       # 折价空间≥10%
        and ev > 0.05          # 正期望
        and target_odds < 0.85 # 不买太贵
        and confidence >= 0.50 # 动量确认
    )

    details["should_bet"] = should_bet
    details["bet_reason"] = (
        f"折价={discount:.3f}({'✅' if discount>=0.10 else '❌'}≥0.10) "
        f"ev={ev:+.3f}({'✅' if ev>0.05 else '❌'}) "
        f"odds={target_odds:.3f}({'✅' if target_odds<0.85 else '❌'}) "
        f"conf={confidence:.0%}({'✅' if confidence>=0.50 else '❌'}≥50%)"
    )

    return should_bet, direction, confidence, details


def calculate_kelly_size(confidence, ev, balance):
    """
    折价策略仓位计算（基于折价空间而非胜率）
    
    折价越大 → 安全边际越高 → 仓位越大
    
    Args:
        confidence: 置信度（折价策略下代表折价评分）
        ev: 折价收益率（discount/odds）
        balance: 当前余额
    
    Returns:
        size: 下注份数 (3-10)
    """
    if ev <= 0:
        return 3
    
    # 基于折价收益率分配仓位
    # ev = discount / odds（折价相对于买入价的比率）
    if ev >= 0.30:
        size = 10  # 30%+折价，极好机会
    elif ev >= 0.20:
        size = 8   # 20%折价
    elif ev >= 0.15:
        size = 7   # 15%折价
    elif ev >= 0.10:
        size = 5   # 10%折价，正常
    elif ev >= 0.05:
        size = 3   # 5%折价，最小仓位
    else:
        size = 3
    
    # 安全约束：根据余额调整
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
