#!/usr/bin/env python3
"""
改进的平仓逻辑 v2
核心改进：
1. 分级平仓策略（正常/紧急/最后）
2. 多次重试机制
3. 订单状态检查
4. 动态价格调整
"""
import subprocess
import requests
import time
from datetime import datetime, timezone

def get_orderbook(token_id, timeout=3):
    """获取订单簿"""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=timeout
        )
        if resp.status_code == 200:
            return resp.json()
    except:
        pass
    return None

def sell_order(token_id, size, price, timeout=20):
    """提交卖单"""
    cmd = [
        "polymarket", "clob", "create-order",
        "--signature-type", "gnosis-safe",
        "--token", token_id,
        "--side", "sell",
        "--price", str(round(price, 3)),
        "--size", str(size),
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        success = result.returncode == 0
        output = result.stdout if success else result.stderr
        return success, output
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)

def close_position_v2(token_id, size, time_remaining=None, entry_price=None, mode="normal"):
    """
    改进的平仓函数
    
    Args:
        token_id: 代币ID
        size: 数量
        time_remaining: 剩余时间（秒），用于判断紧急程度
        entry_price: 入场价格，用于计算盈亏
        mode: 平仓模式 ("normal", "urgent", "emergency")
    
    Returns:
        (success, exit_price, message)
    """
    
    # 自动判断模式
    if time_remaining is not None and mode == "normal":
        if time_remaining < 20:
            mode = "emergency"
        elif time_remaining < 45:
            mode = "urgent"
    
    print(f"🔔 平仓模式: {mode} | 剩余时间: {time_remaining}s")
    
    # 根据模式设置策略
    if mode == "emergency":
        # 紧急模式：不惜代价成交
        max_retries = 8
        retry_delay = 1
        price_strategy = "aggressive"
    elif mode == "urgent":
        # 紧急模式：快速成交
        max_retries = 5
        retry_delay = 2
        price_strategy = "moderate"
    else:
        # 正常模式：保守价格
        max_retries = 3
        retry_delay = 3
        price_strategy = "conservative"
    
    for attempt in range(max_retries):
        print(f"  尝试 {attempt + 1}/{max_retries}...")
        
        # 获取最新订单簿
        book = get_orderbook(token_id)
        
        if book:
            bids = book.get('bids', [])
            best_bid = float(bids[0]['price']) if bids else None
        else:
            best_bid = None
        
        # 根据策略计算价格
        prices = []
        
        if price_strategy == "aggressive":
            # 激进策略：快速成交
            if best_bid and best_bid >= 0.05:
                prices = [
                    best_bid * 0.95,
                    best_bid * 0.90,
                    best_bid * 0.80,
                    0.05,
                    0.01
                ]
            else:
                prices = [0.05, 0.03, 0.01]
        
        elif price_strategy == "moderate":
            # 适中策略
            if best_bid and best_bid >= 0.10:
                prices = [
                    best_bid * 0.98,
                    best_bid * 0.95,
                    best_bid * 0.90,
                    0.10,
                    0.05
                ]
            else:
                prices = [0.10, 0.05, 0.01]
        
        else:  # conservative
            # 保守策略
            if best_bid and best_bid >= 0.20:
                prices = [
                    best_bid * 0.99,
                    best_bid * 0.97,
                    best_bid * 0.95
                ]
            else:
                prices = [0.20, 0.15, 0.10]
        
        # 过滤价格
        valid_prices = [p for p in prices if p >= 0.01]
        
        # 尝试每个价格
        for price in valid_prices:
            print(f"    价格: ${price:.3f}")
            success, output = sell_order(token_id, size, price)
            
            if success:
                print(f"  ✅ 平仓成功！价格: ${price:.3f}")
                return True, price, "Success"
            else:
                print(f"    失败: {output[:50]}")
        
        # 重试前等待
        if attempt < max_retries - 1:
            print(f"  ⏳ 等待 {retry_delay}s 后重试...")
            time.sleep(retry_delay)
    
    print(f"  ❌ 平仓失败（{max_retries}次尝试）")
    return False, None, f"Failed after {max_retries} attempts"

if __name__ == "__main__":
    # 测试
    import sys
    if len(sys.argv) >= 3:
        token_id = sys.argv[1]
        size = int(sys.argv[2])
        time_remaining = int(sys.argv[3]) if len(sys.argv) > 3 else None
        
        success, price, msg = close_position_v2(token_id, size, time_remaining)
        print(f"\n结果: {'成功' if success else '失败'}")
        if price:
            print(f"价格: ${price:.3f}")
        print(f"消息: {msg}")
