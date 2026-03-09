# 基于流动性分析的智能平仓方案

## 一、核心思路

**问题：** 当前平仓策略是盲目尝试多个价格，不考虑订单簿的实际流动性

**解决方案：** 先获取订单簿流动性，根据实际情况选择最优平仓价格

---

## 二、流动性数据结构

### 2.1 订单簿数据（Polymarket CLOB API）

```json
{
  "bids": [
    {"price": "0.85", "size": "100"},
    {"price": "0.84", "size": "50"},
    {"price": "0.83", "size": "200"},
    {"price": "0.80", "size": "500"},
    ...
  ],
  "asks": [...]
}
```

### 2.2 流动性分析指标

```python
流动性指标 = {
    "best_bid": 0.85,              # 最佳买价
    "total_liquidity": 850,        # 总流动性（所有买单数量）
    "depth": 10,                   # 价格深度（买单层数）
    "cumulative_liquidity": [      # 累计流动性
        {"price": 0.85, "cum_size": 100},
        {"price": 0.84, "cum_size": 150},
        {"price": 0.83, "cum_size": 350},
        {"price": 0.80, "cum_size": 850},
    ],
    "liquidity_score": 8.5,        # 流动性评分（0-10）
}
```

---

## 三、智能平仓策略

### 3.1 策略一：流动性匹配

**适用场景：** 订单簿流动性充足

**逻辑：**
```
1. 获取订单簿买单列表
2. 计算累计流动性
3. 找到满足条件的最高价格：
   - 累计流动性 >= 需要卖出的数量
   - 价格 >= 最佳买价 × 95%
4. 在该价格下单
```

**示例：**
```
需要卖出：10份
订单簿：
  $0.85 × 5份  (累计5)
  $0.84 × 3份  (累计8)
  $0.83 × 5份  (累计13) ✅ 满足条件
  $0.80 × 20份 (累计33)

选择价格：$0.83
原因：这是累计流动性>=10的最高价格
```

### 3.2 策略二：分批平仓

**适用场景：** 单个价格点流动性不足，但总流动性充足

**逻辑：**
```
1. 分析订单簿，找到流动性最好的前3个价格
2. 按流动性比例分配卖出数量
3. 同时在多个价格下单
```

**示例：**
```
需要卖出：20份
订单簿：
  $0.85 × 8份
  $0.84 × 6份
  $0.83 × 10份

分批策略：
  - $0.85 卖出 8份
  - $0.84 卖出 6份
  - $0.83 卖出 6份
```

### 3.3 策略三：流动性评分

**适用场景：** 判断订单簿健康度，选择平仓模式

**流动性评分公式：**
```python
score = (
    best_bid权重 × 4 +
    总流动性权重 × 3 +
    价格深度权重 × 2 +
    价差权重 × 1
) / 10

best_bid权重 = min(best_bid / 0.5, 1.0)  # 最佳买价越高越好
总流动性权重 = min(total_liquidity / 100, 1.0)  # 总量越大越好
价格深度权重 = min(depth / 10, 1.0)  # 层数越多越好
价差权重 = 1 - abs(best_bid - best_ask) / best_bid  # 价差越小越好
```

**根据评分选择策略：**
```
评分 >= 8.0: 优秀 → 使用保守价格（99%）
评分 6.0-8.0: 良好 → 使用适中价格（97%）
评分 4.0-6.0: 一般 → 使用激进价格（95%）
评分 < 4.0: 很差 → 使用极端价格（90%或市价）
```

---

## 四、完整执行流程

### 4.1 流程图

```
开始平仓
    ↓
获取订单簿数据
    ↓
分析流动性
    ↓
计算流动性评分
    ↓
    ├─ 评分 >= 8.0 → 策略一：流动性匹配
    │                  ├─ 找到最优价格
    │                  └─ 单次下单
    │
    ├─ 评分 6.0-8.0 → 策略一或策略二
    │                  ├─ 判断单价格流动性
    │                  └─ 选择单次或分批
    │
    └─ 评分 < 6.0 → 策略三：激进平仓
                       ├─ 使用多价格梯度
                       └─ 快速成交优先
```

### 4.2 代码伪逻辑

```python
def smart_close_position(token_id, size):
    # 1. 获取订单簿
    orderbook = get_orderbook(token_id)
    
    # 2. 分析流动性
    liquidity = analyze_liquidity(orderbook, size)
    
    # 3. 计算评分
    score = calculate_liquidity_score(liquidity)
    
    # 4. 选择策略
    if score >= 8.0:
        # 优秀：流动性匹配
        price = find_optimal_price(liquidity, size)
        return sell_at_price(token_id, size, price)
    
    elif score >= 6.0:
        # 良好：判断是否需要分批
        if liquidity['best_bid_size'] >= size:
            # 单价格流动性充足
            price = liquidity['best_bid'] * 0.99
            return sell_at_price(token_id, size, price)
        else:
            # 需要分批
            return sell_in_batches(token_id, size, liquidity)
    
    else:
        # 一般/很差：使用激进策略
        return aggressive_sell(token_id, size, liquidity)
```

---

## 五、关键函数设计

### 5.1 获取订单簿

```python
def get_orderbook(token_id):
    """获取完整订单簿"""
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    resp = requests.get(url, timeout=5)
    return resp.json()
```

### 5.2 分析流动性

```python
def analyze_liquidity(orderbook, target_size):
    """分析订单簿流动性"""
    bids = orderbook.get('bids', [])
    
    if not bids:
        return {"score": 0, "best_bid": None}
    
    # 计算累计流动性
    cumulative = []
    total = 0
    for bid in bids:
        price = float(bid['price'])
        size = float(bid['size'])
        total += size
        cumulative.append({
            'price': price,
            'size': size,
            'cumulative': total
        })
    
    best_bid = float(bids[0]['price'])
    
    return {
        'best_bid': best_bid,
        'best_bid_size': float(bids[0]['size']),
        'total_liquidity': total,
        'depth': len(bids),
        'cumulative': cumulative,
        'target_size': target_size
    }
```

### 5.3 找到最优价格

```python
def find_optimal_price(liquidity, target_size):
    """找到满足流动性要求的最高价格"""
    cumulative = liquidity['cumulative']
    best_bid = liquidity['best_bid']
    
    # 找到累计流动性 >= target_size 的最高价格
    for level in cumulative:
        if level['cumulative'] >= target_size:
            # 确保价格不低于 best_bid * 95%
            if level['price'] >= best_bid * 0.95:
                return level['price']
    
    # 如果没找到，使用最佳买价的95%
    return best_bid * 0.95
```

### 5.4 计算流动性评分

```python
def calculate_liquidity_score(liquidity):
    """计算流动性评分（0-10）"""
    best_bid = liquidity.get('best_bid', 0)
    total_liquidity = liquidity.get('total_liquidity', 0)
    depth = liquidity.get('depth', 0)
    target_size = liquidity.get('target_size', 10)
    
    # 各项权重评分
    bid_score = min(best_bid / 0.5, 1.0) * 4  # 最佳买价
    liquidity_score = min(total_liquidity / target_size / 10, 1.0) * 3  # 总流动性
    depth_score = min(depth / 10, 1.0) * 2  # 价格深度
    
    # 检查是否有足够流动性
    if total_liquidity >= target_size:
        coverage_score = 1.0
    else:
        coverage_score = total_liquidity / target_size
    
    total_score = bid_score + liquidity_score + depth_score + coverage_score
    
    return round(total_score, 2)
```

---

## 六、优势分析

### 6.1 vs 当前方案

| 对比项 | 当前方案 | 流动性方案 |
|--------|----------|------------|
| 价格选择 | 盲目尝试 | 基于实际流动性 |
| 成交概率 | 依赖重试 | 选择有流动性的价格 |
| 平仓速度 | 可能很慢 | 更快（避免无效尝试） |
| 价格优化 | 固定梯度 | 动态选择最优价格 |
| 流动性感知 | ❌ | ✅ |

### 6.2 预期效果

1. **提高成交速度**
   - 避免在无流动性的价格点浪费时间
   - 直接选择有买单的价格

2. **优化成交价格**
   - 在流动性充足时，选择更高的价格
   - 在流动性不足时，及时降价

3. **减少失败率**
   - 基于实际流动性，而不是盲目尝试
   - 分批平仓应对流动性不足

4. **智能决策**
   - 根据订单簿健康度选择策略
   - 动态调整平仓方式

---

## 七、实施建议

### 7.1 分阶段实施

**阶段一：流动性分析**
- 实现订单簿获取
- 实现流动性分析函数
- 实现评分系统

**阶段二：策略集成**
- 将流动性分析集成到现有平仓逻辑
- 保留原有重试机制作为兜底

**阶段三：优化调整**
- 根据实际效果调整评分公式
- 优化价格选择算法

### 7.2 风险控制

1. **API失败处理**
   - 如果无法获取订单簿，回退到原有策略
   - 设置超时时间（3秒）

2. **数据验证**
   - 验证订单簿数据的有效性
   - 过滤异常价格

3. **兜底机制**
   - 如果流动性分析失败，使用原有多价格策略
   - 保留激进平仓作为最后手段

---

## 八、示例场景

### 场景一：流动性充足

```
订单簿：
  $0.85 × 50份
  $0.84 × 30份
  $0.83 × 100份

需要卖出：10份
流动性评分：9.2（优秀）

策略：流动性匹配
选择价格：$0.85
原因：最佳买价流动性充足（50 > 10）
预期：快速成交，价格最优
```

### 场景二：流动性一般

```
订单簿：
  $0.75 × 5份
  $0.74 × 3份
  $0.73 × 8份
  $0.70 × 20份

需要卖出：10份
流动性评分：6.5（良好）

策略：分批平仓
执行：
  - $0.75 卖出 5份
  - $0.74 卖出 3份
  - $0.73 卖出 2份
预期：分3次成交，价格较好
```

### 场景三：流动性很差

```
订单簿：
  $0.15 × 2份
  $0.10 × 5份
  $0.05 × 10份

需要卖出：10份
流动性评分：3.2（很差）

策略：激进平仓
执行：使用多价格梯度 [0.15, 0.10, 0.05, 0.01]
预期：快速成交，价格较差但避免无法平仓
```

---

## 九、总结

**核心优势：**
- ✅ 基于实际流动性，而不是盲目尝试
- ✅ 动态选择最优价格
- ✅ 智能判断订单簿健康度
- ✅ 分批平仓应对流动性不足
- ✅ 保留兜底机制，确保成功率

**实施难度：** 中等
**预期收益：** 提高平仓速度和价格，降低失败率

**建议：** 先实现流动性分析，与现有策略并行测试，验证效果后再完全替换
