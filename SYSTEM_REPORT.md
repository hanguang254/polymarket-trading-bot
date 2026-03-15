# Polymarket Trading Bot v7.0 — 完整功能报告

## 一、系统概述

全自动 Polymarket 5 分钟加密货币二元市场交易机器人。核心策略为**EV 驱动套利**：使用随机游走概率模型（`Φ(|gap|/σ√t)`）检测 token 是否被低估，通过 EV 驱动的分阶段退出管理持仓。

**三大进程 + 看门狗：**
| 进程 | 文件 | 功能 |
|------|------|------|
| 主交易循环 | `auto_bot_v3.py` | 市场发现 → 预热观察 → 早期/晚期窗口下注 |
| 持仓监控 | `position_monitor.py` | 实时盯盘 → 4 阶段平仓 → 挂单对账入仓 |
| 自动领取 | `auto_redeem_v2.py` | 轮询 → 结算领取 |
| 看门狗 | `watchdog_v3.sh` | 监控三进程 → 自动重启 |

---

## 二、市场生命周期时间线（300 秒）

```
 0s ─────── 市场开始
 2s ─────── PTB 获取开始（Playwright → HTML → Gamma API 三级降级）
20s ─────── 预热扫描开始（贝叶斯序贯更新，每5秒采样）
40s ─────── PTB 获取截止
90s ─────── 早期下注窗口开启（低门槛，抢CLOB定价偏差）
95s ─────── 早期下注窗口关闭
100s ────── 晚期下注窗口开启（标准门槛）
160s ────── 晚期下注窗口关闭
     ─────── 实时盯盘开始（EV 驱动退出）
     ─────── 挂单对账循环（LIVE → 检测成交 → 入仓 → TG通知）
180s ────── 阶段1：流动性健康期（120-180s 剩余）
240s ────── 阶段2：流动性下降期（60-120s 剩余）
270s ────── 阶段3：流动性枯竭期（30-60s 剩余）
290s ────── 阶段4：最后机会（0-30s 剩余）
300s ────── 市场结束 → API 查询真实 outcome → 结算
```

---

## 三、模块详解

### 3.1 主交易循环 (`auto_bot_v3.py`)

**MarketTracker 类** 负责全流程管理：

#### 市场发现
- 调用 Gamma API 获取当前 5 分钟 BTC/ETH 市场
- Slug 格式：`btc-updown-5m-{timestamp}`
- 每 5 秒扫描新市场

#### PTB 获取（三级降级）
```
Playwright (6s) → HTML Scraper (10s) → Gamma API (3s)
```
- Playwright 连续失败 3 次后自动跳过，防止阻塞
- PTB 缓存到 `ptb_cache[slug]`

#### 预热观察（T+20s ~ T+100s）
- 每 5 秒采样一次 Binance 实时价格
- 计算 gap = `(当前价 - PTB) / PTB × 100%`
- 初始化 **贝叶斯序贯更新器**（`BayesianUpdater`）：
  - 先验概率 = 市场赔率
  - 每次采样更新后验：`log P(H|D) = log P(H) + Σ log P(Dk|H)`
  - 似然函数：`sigmoid(k × (price - ptb) / atr)`，k=2.5
- Gap 趋势分析：
  - **扩大**：偏离持续增大（下注信号强）
  - **缩小**：偏离在缩小（min_discount 提高到 18%）
  - **穿越**：方向反转（需贝叶斯 ≥ 60% 才允许下注）
  - **震荡**：无明显趋势（min_discount 提高到 15%）

#### 相关性暴露控制（P3）
```python
def get_correlated_exposure(direction, coin):
    # BTC/ETH 相关性 ~0.85
    # 已有同方向持仓 → 返回 0.5（Kelly 减半）
    # 无 → 返回 1.0
```

#### 熔断器
- 连续 5 次 API 失败 → 暂停 300 秒
- 自动恢复

---

### 3.2 AI 评分模型 (`ai_trader/ai_model_v2.py`)

**核心策略：折价套利（非方向预测）**

#### 输入
- Binance 30 根 1 分钟 K 线
- 当前价格、PTB、UP/DOWN 赔率

#### 分析流程

**1. 价格偏离度**
```
price_diff = 当前价 - PTB
diff_in_atr = |price_diff| / ATR
```
- `price > PTB` → 方向 = UP
- `price < PTB` → 方向 = DOWN

**2. Token 价值估算（diff_in_atr 查表）**

| ATR 倍数 | 估算价值 | 说明 |
|----------|---------|------|
| > 4.0 | 0.88 | 极大幅领先 |
| 3.0-4.0 | 0.83 | 大幅领先 |
| 2.0-3.0 | 0.75 | |
| 1.5-2.0 | 0.69 | |
| 1.0-1.5 | 0.63 | |
| 0.7-1.0 | 0.58 | |
| 0.5-0.7 | 0.55 | |
| < 0.5 | 0.51 | 平盘，不值得 |

**3. 折价空间**
```
discount = estimated_value - leading_odds
```
例：estimated_value=0.80, leading_odds=0.65 → discount=0.15（15% 利润空间）

**4. 辅助指标**
- **动量**：3 分钟趋势 + 1 分钟微趋势（确认 +15 分 / 反转 -8 分）
- **成交量**：放量 > 2x 加 5 分，> 1.5x 加 3 分

**5. 综合评分**
```
confidence = min(total_score / 80, 1.0)
```

---

### 3.3 Base Rate 校准 (`ai_trader/base_rate.py`) — P0

**协议来源：** Trading Desk Entry Protocol Filter 2
> "Before assigning probability, check historical base rate. If base rate is below 12%, reduce position size by 50%."

#### 保守先验表

| ATR 带 | 先验胜率 | 说明 |
|--------|---------|------|
| 0-0.7 | 0.50 | 几乎平盘，无优势 |
| 0.7-1.0 | 0.55 | |
| 1.0-1.5 | 0.60 | |
| 1.5-2.0 | 0.65 | |
| 2.0-3.0 | 0.72 | |
| 3.0-4.0 | 0.78 | |
| 4.0+ | 0.85 | |

#### 自动校准
- 每次平仓调用 `record_outcome()` → 写入 `logs/outcomes.jsonl`
- 每 50 条自动触发 `calibrate()`
- 每个 ATR 带样本 ≥ 30 时，实证胜率替代先验
- 内存缓存 60 秒 TTL

---

### 3.4 决策引擎 (`ai_analyze_v2.py`) — P0+P2

#### 下注条件（全部满足才下注）

| 条件 | 阈值 | 说明 |
|------|------|------|
| 折价空间 | `discount ≥ 动态阈值` | LMSR 流动性驱动：8%-20% |
| 严格 EV | `ev > 0.03` | 二元市场：`EV = p_win - price` |
| 赔率上限 | `target_odds < 0.85` | 不买太贵的 token |
| 置信度 | `confidence ≥ 0.65` | 动量确认 |

#### 严格概率 EV 公式（P2）

**旧公式**：`ev = discount / leading_odds`（收益率）
**新公式**：`ev = p_win - target_odds`（二元市场标准 EV）

- `p_win` 优先级：base_rate → 贝叶斯融合 → confidence 映射
- 贝叶斯融合（方向一致且置信度 > 30%）：
  ```
  p_win = base_rate × 0.4 + bayesian_p_hat × 0.6
  ```
- 交叉验证：`estimated_value > p_win + 0.15` → confidence × 0.85（高估警告）

#### Kelly 仓位计算

```
f* = (p - price) / (1 - price)    # 二元市场 Kelly 公式
f/4 = f* × 0.25 × kelly_reduction  # 1/4 Kelly + 缩减
```

**缩减因子叠加：**
- Base Rate < 0.55 → `kelly_reduction = 0.5`（无统计优势）
- 已有同方向持仓 → `correlation_factor = 0.5`（P3 相关性控制）
- 总缩减：`kelly_reduction × correlation_factor`

**仓位映射：**

| f/4 范围 | 份数 |
|----------|------|
| ≤ 0.10 | 5 |
| 0.10-0.15 | 7 |
| 0.15-0.20 | 8 |
| ≥ 0.20 | 10 |

**硬约束：** 5-10 份，余额 < $20 最多 10 份，< $50 最多余额 20%，≥ $50 最多余额 10%

#### 下注执行
- 买入价：订单簿 best_ask → midpoint + $0.01 → 拒绝默认值
- CLI 命令：`polymarket clob create-order --side buy --price X --size Y`
- **成交确认**：解析 CLI 输出的 `Status` / `Taking` 字段
  - 仅 `Status: MATCHED` 视为成功（`LIVE` 挂单不记录持仓）
  - 实际成交价 = `Taking / Size`（非限价单价格）
- 日志记录 `price`（实际成交价）+ `limit_price`（提交的限价）
- 持仓记录丰富字段：`ptb, atr_val, diff_in_atr, base_rate, p_win_final, estimated_value`

---

### 3.5 LMSR 流动性评估 (`ai_trader/lmsr_liquidity.py`)

**纯订单簿评分（不使用 LMSR 数学模型）**

#### 评分维度

| 维度 | 权重 | 满分条件 | 零分条件 |
|------|------|---------|---------|
| Spread | 35% | ≤ 0.01 | ≥ 0.10 |
| Depth | 35% | ≥ 200 份 | ≤ 20 份 |
| Slippage (5份) | 30% | 0% | ≥ 5% |

#### 动态折价阈值

| 流动性评分 | 折价阈值 | 说明 |
|-----------|---------|------|
| ≥ 0.8 | 8% | 极好，低门槛 |
| ≥ 0.6 | 10% | 良好 |
| ≥ 0.4 | 13% | 一般 |
| ≥ 0.2 | 16% | 较差 |
| < 0.2 | 20% | 极差，高门槛 |

额外补偿：`最终阈值 = 基础阈值 + slippage × 2.5`

---

### 3.6 贝叶斯序贯更新引擎 (`ai_trader/bayesian_engine.py`)

**Log-space 更新，数值稳定：**
```
log P(UP|D1..Dt) = log P(UP) + Σ log P(Dk|UP)
```

- 似然函数：`sigmoid(k × (price - ptb) / atr)`，k=2.5
- 衰减：旧采样权重随时间递减（`decay = 0.95^age`）
- 输出：`p_hat`（后验概率）、`direction`、`confidence`

---

### 3.7 持仓监控 (`position_monitor.py`) — P1+P4 + 挂单对账

每 2 秒轮询所有未关闭持仓。每轮还执行 `reconcile_pending_orders()` 对账挂单。

#### EV 驱动实时退出（P1）— 替代固定 % 阈值

**协议来源：** Trading Desk Exit Protocol
> "Do not exit before resolution unless EV flips negative."

| 旧规则 | 新规则（EV 驱动） | 说明 |
|--------|-----------------|------|
| 利润 ≥ 15% → 卖 | EV > 0 → **持有** | 协议核心：EV 正不退出 |
| 利润 ≥ 8% → 挂单 | EV < 0.02 且盈利 > 5% → 保护性挂单 | EV 接近零时锁利 |
| 亏损 ≤ -10% → 止损 | EV < -0.05 → 止损 | EV 显著负才止损 |
| — | EV < 0 → 卖出 | EV 翻负信号 |
| 趋势反转 + 亏损 > 3% | 保留作为辅助信号 | |

**实时 EV 计算（`calc_realtime_ev`）：**
```python
diff_in_atr = |crypto_price - ptb| / atr
raw_p = get_base_rate(diff_in_atr)        # 查表
p_hat_now = raw_p if 方向正确 else (1 - raw_p)
realtime_ev = p_hat_now - entry_price     # 二元 EV
```

Fallback：EV 无法计算时保留旧逻辑（15% 止盈 / -10% 止损）。

#### 4 阶段平仓策略

**阶段 1（180-120s 剩余）— 流动性健康期**
- EV > 0.05 → 推迟卖出，让正 EV 继续运行
- 必输 → 立即止损（不受 EV 保护）
- 利润 ≥ 10% 且 EV ≤ 0.05 → 止盈
- 必赢 → 高价卖出

**阶段 2（120-60s 剩余）— 流动性下降期**
- EV > 0.05 且非必输 → 推迟卖出
- 其他 → 挂单确认（size ≤ 5）或分批出货（size > 5）
- 分批策略：50% / 30% / 20% 梯度价格

**阶段 3（60-30s 剩余）— 流动性枯竭期 + EV 定价保护（P4）**
- EV > 0 → 设最低价 `max(entry_price × 0.85, $0.15)`，不恐慌抛售
- 智能平仓（流动性评分驱动）→ 多价格梯度策略
- 低于最低价的成交被拒绝，保留持仓

**阶段 4（30-0s 剩余）— 最后机会 + EV 分层（P4）**
- EV > 0.05 → **持有到结算**（不卖，等结算赔付）
- EV 0~0.05 → 温和底价 `[entry×0.80, $0.10, $0.05]`
- EV < 0 → 地板价 `[$0.10, $0.05, $0.02, $0.01]`

#### 平仓后结果记录（P0 闭环）
```python
close_position() → record_outcome(slug, direction, diff_in_atr, won)
```
每 50 条结果自动触发 `calibrate()` 更新实证胜率。

---

### 3.8 挂单对账系统 (`position_monitor.py` — `reconcile_pending_orders()`)

**问题**：CLI 下单返回 `Status: LIVE`（挂单未立即成交），不记录持仓。若后续链上成交，持仓丢失。

**解决方案**：
```
execute_bet() LIVE → 写入 pending_orders.jsonl
                          ↓
monitor 每轮调用 reconcile_pending_orders()
                          ↓
    ┌─ 已在 positions.jsonl? → RESOLVED_ALREADY
    ├─ 查 positions snapshot / token balance
    │   └─ filled_size ≥ PENDING_MIN_FILL → 入仓 + TG通知(⏰)
    └─ age ≥ PENDING_ORDER_TTL → cancel_all + TG通知(⌛)
```

**成交检测优先级**：
1. `polymarket data positions` snapshot（API 查链上持仓）
2. `polymarket clob balance` token 余额（fallback）

**TG 通知区分**：
| 场景 | 标题 | 内容 |
|------|------|------|
| 直接成交 | 🎯 Polymarket 下注成功 | 币种/方向/置信度/EV/价格×份数 |
| 挂单成交 | ⏰ Polymarket 挂单成交 | 同上 + 等待时间 + 价格来源 |
| 挂单过期 | ⌛ 挂单过期取消 | 币种/方向/限价/等待时间 |

---

### 3.9 交易状态管理 (`trading_state.py`)

| 功能 | 参数 | 说明 |
|------|------|------|
| 冷却期 | 失败后 3 期 | 连续失败后观望 |
| 日损限额 | `MAX_DAILY_LOSS` ($10) | 超限停止交易 |
| 胜率统计 | total/wins/losses | 持久化到 JSON |
| 状态文件 | `logs/trading_state.json` | 跨重启保持 |

---

### 3.10 自动领取 (`auto_redeem_v2.py`)

- 30 分钟轮询
- 查询 PROXY_WALLET + EOA_WALLET 持仓
- 筛选已结算条件（resolved / redeemable / outcome 非空）
- 执行 `polymarket ctf redeem --condition <ID>`
- neg-risk 市场用 `redeem-neg-risk`
- 每次间隔 2 秒，避免 rate limit
- Telegram 通知领取金额

---

## 四、P0-P4 优化实现总结

### P0：Base Rate 校准
- **文件**：`ai_trader/base_rate.py`（新建）
- **效果**：保守先验替代过度自信的 estimated_value，base_rate < 0.55 时 Kelly 减半
- **协议依据**：Filter 2 将命中率从 61% 提升到 74%

### P1：EV 驱动退出
- **文件**：`position_monitor.py`（实时监控 + 阶段 1-2 EV 守卫）
- **效果**：EV 正时不提前退出，避免"明明在赢却因为固定阈值卖掉"
- **协议依据**：Exit Protocol 核心原则 "Don't exit if EV positive"

### P2：严格概率 EV 公式
- **文件**：`ai_analyze_v2.py` + `ai_model_v2.py`
- **效果**：`EV = p_win - price`（标准二元期望值），替代 `discount / leading_odds`（收益率）
- **关联**：贝叶斯融合、交叉验证、Kelly 公式全部使用新 p_win

### P3：相关性暴露控制
- **文件**：`auto_bot_v3.py`
- **效果**：BTC/ETH 同方向持仓 → Kelly 减半，防止双倍风险敞口
- **协议依据**：相关性 ~0.85，同方向等于 double exposure

### P4：EV 定价保护
- **文件**：`position_monitor.py`（阶段 3-4）
- **效果**：EV 正时不用地板价恐慌抛售，阶段 4 EV > 0.05 直接持有到结算
- **协议依据**：Exit Protocol "Hyperbolic discounting: don't panic-sell winners"

---

## 五、风控体系

```
┌─────────────────────────────────────────────────────┐
│                    多层风控                          │
├─────────────────────────────────────────────────────┤
│ Layer 1: 进场过滤                                    │
│   ├─ 折价阈值（LMSR 动态：8%-20%）                   │
│   ├─ EV > 3%（严格概率公式）                         │
│   ├─ 赔率 < 0.85                                    │
│   ├─ 置信度 ≥ 65%                                   │
│   └─ Base Rate 校准（< 0.55 → Kelly 减半）           │
│                                                      │
│ Layer 2: 仓位控制                                    │
│   ├─ 1/4 Kelly（5 分钟市场不用 full Kelly）           │
│   ├─ 相关性缩减（同方向 × 0.5）                     │
│   ├─ Base Rate 缩减（弱优势 × 0.5）                 │
│   ├─ 硬约束：5-10 份                                │
│   └─ 余额比例约束（10%-20%）                        │
│                                                      │
│ Layer 3: 持仓管理                                    │
│   ├─ EV 驱动退出（正 EV 不卖）                      │
│   ├─ EV 定价保护（正 EV 不用地板价）                │
│   ├─ 4 阶段梯度平仓                                │
│   └─ 流动性自适应策略                               │
│                                                      │
│ Layer 4: 系统级保护                                  │
│   ├─ 最大持仓数：2（可配置）                        │
│   ├─ 日损限额：$10（可配置）                        │
│   ├─ 熔断器：5 次失败 → 300 秒暂停                  │
│   ├─ 冷却期：失败后 3 期观望                        │
│   └─ 余额检查：< $5 跳过                            │
└─────────────────────────────────────────────────────┘
```

---

## 六、数据流

```
Binance API ──→ K线+价格 ──→ ai_model_v2 ──→ 偏离度+折价+动量
                                   │
Polymarket Web ──→ PTB ────────────┤
                                   ▼
Gamma API ──→ 市场/赔率 ──→ ai_analyze_v2 ──→ 决策(BET/SKIP)
                                   │
CLOB API ──→ 订单簿 ──────────────┤
                                   │
贝叶斯引擎 ──→ p_hat ─────────────┤
                                   │
base_rate ──→ 先验/实证胜率 ───────┘
                                   │
                              ┌────▼────┐
                              │ 下注执行  │ → logs/positions.jsonl
                              └────┬────┘
                                   │
                              ┌────▼────┐
                              │ 持仓监控  │ → calc_realtime_ev()
                              └────┬────┘
                                   │
                              ┌────▼────┐
                              │  平仓    │ → record_outcome()
                              └────┬────┘       ↓
                                   │      logs/outcomes.jsonl
                              ┌────▼────┐       ↓
                              │ 自动领取  │  calibrate() → base_rates.json
                              └─────────┘       ↑
                                          (闭环学习)
```

---

## 七、文件结构

```
polymarket-trading-bot/
├── auto_bot_v3.py              # 主交易循环（入口）
├── ai_analyze_v2.py            # 决策引擎（EV + Kelly + 挂单记录）
├── position_monitor.py         # 持仓监控（EV 退出 + 4 阶段 + 挂单对账）
├── auto_redeem_v2.py           # 自动结算领取
├── trading_state.py            # 状态管理
├── watchdog_v3.sh              # 进程看门狗
├── ai_trader/
│   ├── ai_model_v2.py         # 折价套利评分模型
│   ├── base_rate.py           # Base Rate 校准（P0）
│   ├── bayesian_engine.py     # 贝叶斯序贯更新
│   ├── lmsr_liquidity.py      # 流动性评估
│   ├── binance_api.py         # Binance 数据
│   ├── polymarket_api.py      # Polymarket 数据
│   ├── playwright_ptb.py      # 浏览器 PTB 获取
│   └── indicators.py          # 技术指标（EMA/RSI/ATR/BB）
├── logs/
│   ├── polymarket-bot.log     # 运行日志
│   ├── monitor_YYYY-MM-DD.log # monitor 每日日志
│   ├── positions.jsonl        # 持仓记录
│   ├── bets.jsonl             # 下注记录
│   ├── decisions_v2.jsonl     # 决策记录
│   ├── closed_positions.jsonl # 已平仓记录
│   ├── trading_state.json     # 状态机
│   ├── outcomes.jsonl         # 交易结果（base_rate 学习）
│   ├── base_rates.json        # 实证胜率
│   ├── pre_orders.json        # 预挂单状态
│   └── pending_orders.jsonl   # LIVE 挂单追踪（对账用）
├── .env                        # 环境变量
└── .env.example                # 环境变量模板
```

---

## 八、环境变量

```bash
# 钱包（必填）
EOA_WALLET=0x...
PROXY_WALLET=0x...
SIGNATURE_TYPE=eoa

# 交易参数
MIN_BALANCE=5.0           # 最低余额要求
MAX_OPEN_POSITIONS=2      # 最大同时持仓数
MAX_DAILY_LOSS=10.0       # 日损限额（美元）

# 通知
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

---

## 九、外部依赖

| 服务 | 用途 |
|------|------|
| Binance API | K 线、实时价格、ATR 计算 |
| Gamma API | 市场数据、事件信息 |
| Polymarket CLOB | 订单簿、中间价、下单 |
| Polymarket CLI | 下单、查余额、redeem |
| Playwright | 浏览器自动化获取 PTB |
| Telegram | 交易通知推送 |

**Python 依赖：** requests, python-dotenv, playwright

---

## 十、关键设计决策

1. **折价套利 vs 方向预测**：历史数据显示 74.5% 的 5 分钟市场结束时偏离 < 0.01%，方向预测几乎无效。真正的盈利来自买入折价 token 后提前平仓。

2. **1/4 Kelly**：5 分钟市场波动剧烈，full Kelly 风险极高。论文注释："NEVER full Kelly on 5min markets!"

3. **EV 正不退出**：Trading Desk Exit Protocol 核心原则。避免因恐慌心理（hyperbolic discounting）在赢的时候提前卖出。

4. **Base Rate 闭环学习**：每次平仓自动记录结果，积累 30+ 样本后实证胜率替代先验，系统越交易越准。

5. **贝叶斯 + 折价融合**：纯折价分析提供静态快照，贝叶斯更新提供动态信心。两者加权融合（40:60）比单独使用更稳健。
