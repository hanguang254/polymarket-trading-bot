# AI 分析与下注决策系统 (`ai_analyze_v2.py`) 功能逻辑报告

## 一、系统概览

**用途**：Polymarket 5分钟二元期权市场的下注决策引擎，负责市场分析、EV 计算、下注条件判断、仓位计算和订单执行。

**核心策略**：折价套利 — 不预测方向，而是检测 token 是否被低估，买入后提前平仓锁定利润。

**核心发现**（基于 889 条历史数据）：
- 5分钟市场结束时价格回归 PTB（偏离缩小 91.7%）
- 74.5% 的市场结束偏离 <0.01%，方向预测几乎无效
- 真正利润来源：T+60s 买入折价 token → T+180s 提前平仓

**文件调用链**：
```
auto_bot_v3.py
  → ai_analyze_v2.py::analyze_and_decide()     ← 决策
    → ai_trader/ai_model_v2.py::analyze_market() ← 评分
    → ai_trader/base_rate.py::get_base_rate()     ← 胜率查表
    → ai_trader/lmsr_liquidity.py                 ← 流动性评估
  → ai_analyze_v2.py::execute_bet()              ← 执行下单
```

---

## 二、函数总览

| 函数 | 职责 |
|------|------|
| `analyze_and_decide()` | 主决策函数：调用评分模型 → 计算 EV → 判断是否下注 |
| `execute_bet()` | 执行下单：获取价格 → 安全检查 → Kelly 仓位 → CLI 下单 → 记录持仓 |
| `calculate_kelly_size()` | 1/4 Kelly 仓位计算（多层约束） |
| `check_bid_depth()` | P2 退出流动性检查（bid-side 总量） |
| `log_decision()` | 记录每次决策到 `logs/decisions_v2.jsonl` |

---

## 三、决策流程 — `analyze_and_decide()`

### 第1步：市场评分（`analyze_market()`）

**输入**：`coin`, `price_to_beat`, `up_odds`, `down_odds`

**数据采集**：
```
Binance 1分钟 K线 × 30 根 + 实时价格
```

#### 核心指标1：价格偏离度

```
price_diff = current_price - PTB
diff_in_atr = |price_diff| / ATR(14)

方向判断：
  price > PTB → direction = "UP"，买 UP token
  price < PTB → direction = "DOWN"，买 DOWN token
```

#### 核心指标2：Token 估值与折价

根据 ATR 偏离倍数查表估值：

| ATR 偏离 | estimated_value | 含义 |
|----------|----------------|------|
| > 4.0 | 0.88 | 极大幅领先 |
| > 3.0 | 0.83 | 大幅领先 |
| > 2.0 | 0.75 | 明显领先 |
| > 1.5 | 0.69 | 中等领先 |
| > 1.0 | 0.63 | 轻度领先 |
| > 0.7 | 0.58 | 微弱领先 |
| > 0.5 | 0.55 | 边界 |
| ≤ 0.5 | 0.51 | 几乎平盘 |

```
discount = estimated_value - leading_odds
```

例：estimated_value=0.75, leading_odds=0.55 → discount=0.20（20% 折价空间）

#### 辅助指标：动量评分

| 信号 | 条件 | 分数 |
|------|------|------|
| 3分钟动量确认 | 方向与偏离一致 | +10 |
| 3分钟动量反向 | 方向与偏离相反 | -5 |
| 1分钟微观确认 | 方向与偏离一致 | +5 |
| 1分钟微观反向 | 方向与偏离相反 | -3 |

#### 辅助指标：成交量

| 成交量比 | 分数 |
|----------|------|
| > 2.0 倍均量 | +5 |
| > 1.5 倍均量 | +3 |
| ≤ 1.5 | 0 |

#### 综合评分

```
discount_score: 0-60 分（按 discount 分档）
total_score = discount_score + momentum_bonus + vol_bonus
confidence = total_score / 80（归一化到 0~1）
```

**返回**：`(direction, confidence, details)`

---

### 第2步：Base Rate 校准（P0）

```python
base_rate = get_base_rate(diff_in_atr)
```

从 `logs/base_rates.json` 按 ATR 分段查表，保守先验 0.50-0.85。30+ 样本后自动校准。

```
base_rate < 0.55 → kelly_reduction = 0.5（无统计优势，仓位减半）
```

---

### 第3步：LMSR 流动性评估

**有 LMSR 数据时**：
```
estimate_lmsr_b(token_id) → {b, liquidity_score, spread, slippage_5, best_ask}
get_dynamic_discount_threshold(liquidity_info) → 8%-20%
```

**无 LMSR 数据时（fallback）**：
```
odds_spread = |up_odds - down_odds|
spread < 0.15 → threshold = 10%
spread ≥ 0.15 → threshold = 15%
```

**Gap 趋势覆盖**（只能提高，不能降低）：
```
震荡 → max(threshold, 15%)
缩小 → max(threshold, 18%)
```

---

### 第4步：胜率计算与贝叶斯融合（P2）

```
p_win 初始值 = base_rate
```

**贝叶斯融合**（如果有预热数据）：

| 条件 | 融合方式 | 置信度调整 |
|------|----------|-----------|
| 贝叶斯方向一致 + conf > 0.3 | `p_win = base_rate×0.4 + p_hat×0.6` | `conf = conf×0.4 + bayesian_conf×0.6` |
| 贝叶斯方向相反 | 不融合 | `conf × 0.5` |
| 贝叶斯 conf ≤ 0.3 | 不融合 | 不变 |

**交叉验证**：
```
estimated_value > p_win + 0.15 → confidence × 0.85（高估警告）
```

---

### 第5步：无效率信号（#7）

```
inefficiency = p_win - realtime_best_ask
```

当 `inefficiency > 0.10`（市场明显错价）：
```
discount_threshold -= 0.02（最低到 6%）
```

**含义**：如果真实胜率远高于市场卖价，说明市场低估了这个 token，降低入场门槛。

---

### 第6步：EV 计算与下注判断

```
EV = p_win - target_odds（严格二元 EV）
```

**四个必须同时满足的下注条件**：

```python
should_bet = (
    discount >= discount_threshold   # 1. 折价空间足够
    and ev > 0.03                    # 2. 期望值 > 3%
    and target_odds < 0.85           # 3. 买入赔率不能太贵
    and confidence >= 0.65           # 4. 动量置信度 ≥ 65%
)
```

### 决策流程图

```
analyze_market()
  → direction, confidence, discount, estimated_value, diff_in_atr
    │
    ├─ Base Rate 查表 → base_rate, kelly_reduction
    │
    ├─ LMSR 流动性 → discount_threshold (8%-20%)
    │   └─ Gap 趋势覆盖 → max(threshold, 15%/18%)
    │
    ├─ p_win = base_rate
    │   └─ 贝叶斯融合 → p_win = 0.4×base + 0.6×bayesian
    │       └─ 交叉验证 → confidence × 0.85 if overestimated
    │
    ├─ 无效率信号 → threshold - 0.02 if p_win - ask > 10%
    │
    ├─ EV = p_win - target_odds
    │
    └─ 四条件判断：
        ├─ discount ≥ threshold?
        ├─ EV > 0.03?
        ├─ target_odds < 0.85?
        └─ confidence ≥ 65%?
        → should_bet = all true
```

---

## 四、执行流程 — `execute_bet()`

### 前置安全检查（5 道门槛）

```
execute_bet() 调用前（在 auto_bot_v3.py 中）：
  ① 贝叶斯 conf < 15% → SKIP
  ② Gap 穿越 + conf < 60% → SKIP
  ③ 持仓数 ≥ MAX_OPEN_POSITIONS → SKIP
  ④ 冷却期中 → SKIP

execute_bet() 内部：
  ⑤ 余额 < MIN_BALANCE ($5) → SKIP_NO_BALANCE
  ⑥ 订单簿 + midpoint 都获取失败 → SKIP_NO_PRICE
  ⑦ 实际买入价 ≥ $0.85 → SKIP_PRICE_TOO_HIGH
  ⑧ bid_depth < 5 (P2) → SKIP_NO_EXIT_LIQUIDITY
```

### 价格获取

```
优先：订单簿 best_ask（吃单成交）
回退：midpoint + $0.01 滑点
失败：跳过下注
上限：price ≥ 0.85 → 跳过
```

### Kelly 仓位计算 — `calculate_kelly_size()`

**公式**：
```
f* = (p - price) / (1 - price)    ← 二元市场 Kelly
实际 = f* × 0.25 × kelly_reduction ← 1/4 Kelly + 缩减
```

**胜率 p 的优先级**：
```
p_win（base_rate + 贝叶斯融合） > p_hat（贝叶斯后验） > confidence 映射
上限：p ≤ 0.85
```

**份数映射**：

| kelly_quarter | 份数 |
|--------------|------|
| ≤ 0.10 | 5 |
| 0.10 ~ 0.15 | 7 |
| 0.15 ~ 0.20 | 8 |
| ≥ 0.20 | 10 |

**多层约束叠加**：

```
┌─ Kelly 缩减因子
│   ├─ base_rate < 0.55 → × 0.5
│   └─ 同方向持仓 → × 0.5（相关性控制）
│
├─ 余额约束
│   ├─ balance < $20 → max 10 份
│   ├─ balance < $50 → max 20% of balance
│   └─ balance ≥ $50 → max 10% of balance
│
├─ P3 流动性上限
│   └─ size ≤ bid_depth × 50%
│
└─ 硬约束：5 ~ 10 份
```

### 下单与成交确认

```
polymarket clob create-order --side buy --price <best_ask> --size <kelly_size>

成交判断：
  Status = MATCHED → 成功，记录持仓
  Status = LIVE → 挂单未成交，不记录持仓

实际成交价：
  BUY 时用 Making / Size（USDC 花费 / token 数）
  解析失败 → 回退用限价
```

### 持仓记录

成功成交后写入 `logs/positions.jsonl`，包含供 `position_monitor.py` 使用的丰富字段：

```json
{
  "token_id": "...",
  "slug": "btc-updown-5m-...",
  "direction": "UP",
  "entry_price": 0.65,        // 实际成交价
  "size": 7,
  "confidence": 0.78,
  "ev": 0.05,
  "entry_time": "2026-...",
  "closed": false,
  "ptb": 83500.00,            // Price To Beat
  "atr_val": 45.2,            // ATR（monitor 的 EV 计算用）
  "diff_in_atr": 1.52,        // ATR 偏离倍数
  "base_rate": 0.62,          // 胜率先验
  "p_win_final": 0.68,        // 融合后胜率
  "opposite_token_id": "..."  // P1 对冲用反向 token
}
```

---

## 五、完整决策管线图

```
                    ┌──────────────────────────────┐
                    │   auto_bot_v3.py 预过滤      │
                    │  ① 贝叶斯 conf < 15% → SKIP │
                    │  ② Gap 穿越 + 弱 → SKIP     │
                    └──────────┬───────────────────┘
                               ▼
              ┌─────────────────────────────────────┐
              │   analyze_and_decide() 决策         │
              │                                     │
              │  analyze_market()                    │
              │    → direction, confidence           │
              │    → discount, estimated_value       │
              │    → diff_in_atr, target_odds        │
              │                                     │
              │  Base Rate → base_rate, kelly_red    │
              │  LMSR → discount_threshold (8-20%)   │
              │  贝叶斯融合 → p_win                  │
              │  交叉验证 → conf 调整                │
              │  无效率信号 → threshold 调整          │
              │                                     │
              │  EV = p_win - target_odds            │
              │                                     │
              │  ┌─ discount ≥ threshold? ─┐        │
              │  ├─ EV > 0.03?             │→ BET?  │
              │  ├─ target_odds < 0.85?    │        │
              │  └─ confidence ≥ 65%?     ─┘        │
              └──────────┬──────────────────────────┘
                         ▼ should_bet = True
              ┌─────────────────────────────────────┐
              │   auto_bot_v3.py 执行前检查          │
              │  ③ 持仓数 ≥ MAX → SKIP             │
              │  ④ 冷却期 → SKIP                   │
              │  相关性控制 → kelly × 0.5            │
              └──────────┬──────────────────────────┘
                         ▼
              ┌─────────────────────────────────────┐
              │   execute_bet() 执行                 │
              │                                     │
              │  ⑤ 余额 < $5 → SKIP               │
              │  ⑥ 价格获取失败 → SKIP             │
              │  ⑦ price ≥ $0.85 → SKIP            │
              │  ⑧ bid_depth < 5 → SKIP            │
              │                                     │
              │  Kelly 仓位计算                      │
              │    f* = (p-price)/(1-price)          │
              │    size = f*/4 × reduction           │
              │    约束: balance, P3, 5-10份         │
              │                                     │
              │  CLI 下单 → MATCHED?                 │
              │    是 → 记录持仓到 positions.jsonl   │
              │    否 → 不记录                       │
              └─────────────────────────────────────┘
```

---

## 六、日志与输出

| 文件 | 内容 |
|------|------|
| `logs/decisions_v2.jsonl` | 每次决策记录（BET/SKIP + 全部指标） |
| `logs/bets.jsonl` | 每次下单记录（成功/失败 + 价格/份数） |
| `logs/positions.jsonl` | 成交后持仓记录（供 position_monitor 使用） |

---

## 七、外部依赖

| 模块 | 功能 |
|------|------|
| `ai_trader/ai_model_v2.py` | 评分模型：ATR 偏离 → 估值 → 折价 → 动量 |
| `ai_trader/base_rate.py` | Base Rate 查表 + 自动校准 |
| `ai_trader/lmsr_liquidity.py` | LMSR 流动性评估 → 动态折价阈值 |
| `ai_trader/bayesian_engine.py` | 贝叶斯后验（由 auto_bot_v3 预热后传入） |
| `ai_trader/binance_api.py` | Binance K线 + 实时价格 |
| `ai_trader/indicators.py` | 技术指标（EMA, RSI, ATR） |
| `position_monitor.py` | `parse_order_output()` 解析 CLI 成交状态 |
| Polymarket CLOB API | 订单簿、midpoint |
| Polymarket CLI | `create-order`（下单）、`balance`（余额） |
