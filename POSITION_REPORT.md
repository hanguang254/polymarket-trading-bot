# 持仓监控系统 (`position_monitor.py`) 功能逻辑报告

## 一、系统概览

**用途**：Polymarket 5分钟二元期权市场的持仓自动管理系统，负责止盈、止损、对冲、分批平仓和结算清理。

**运行方式**：`while True` 主循环，每 2 秒轮询一次所有未关闭持仓。

**数据源**：
| 数据 | 来源 |
|------|------|
| 持仓信息 | `logs/positions.jsonl`（本地文件） |
| Token 价格/订单簿 | Polymarket CLOB API |
| 加密货币实时价 | Binance API |
| PTB (Price To Beat) | 持仓记录中的 `ptb` 字段 |
| ATR (平均真实波幅) | Binance 1分钟K线计算 |
| 胜率查表 | `ai_trader.base_rate` 模块 |

**通知**：所有平仓事件通过 Telegram 推送。

---

## 二、辅助函数层

### 价格获取
| 函数 | 功能 |
|------|------|
| `get_market_price()` | Token 中间价（订单簿 midpoint → midpoint API → 单边 fallback） |
| `get_best_bid()` | 最佳买价 × 0.99（用于卖出，提高成交率） |
| `get_best_ask()` | 最佳卖价（用于买入对冲） |
| `get_current_crypto_price()` | BTC/ETH 实时价（Binance） |
| `get_atr_from_binance()` | 14周期1分钟 ATR |

### 量化计算
| 函数 | 功能 |
|------|------|
| `update_realtime_confidence()` | 贝叶斯置信度更新：方向正确 +boost，方向错误 -penalty，幅度按 ATR 倍数 |
| `should_stop_loss()` | 置信度 <60% 止损，60-70% 且 >30s 止损 |
| `calc_realtime_ev()` | **核心 EV 计算**：`EV = p_hat_now - entry_price`，p_hat 来自 base_rate 查表 + 方向校正 |
| `is_losing_direction()` | 二值判断：≤60s 时加密价格是否偏离 PTB |

### 交易执行
| 函数 | 功能 |
|------|------|
| `sell_position()` | 卖出（最多3次重试，检查 MATCHED 状态，未成交则取消挂单） |
| `buy_opposite_token()` | 买入反向 token（P1 对冲用，最多2次重试） |
| `sell_and_confirm()` | 挂单 + 等待确认成交（超时取消） |
| `sell_in_batches()` | 分3批卖出（50%/30%/20%，逐批降价） |
| `smart_sell_position()` | 流动性评分后选策略：≥8分流动性匹配，≥6分单价格，<6分返回 None |
| `try_sell_with_multiple_prices()` | 多价格梯度尝试（必输时更激进，最低到 $0.01） |

### 持仓管理
| 函数 | 功能 |
|------|------|
| `get_open_positions()` | 读取所有 `closed=False` 的持仓 |
| `close_position()` | 标记关闭 + 写回文件 + 记录到 base_rate 校准 |
| `update_position()` | 更新仓位大小（分批卖出后的剩余） |
| `cancel_all_orders()` | 取消某 token 所有活跃订单 |

---

## 三、主循环决策树（`monitor()` 函数）

每个持仓每轮执行以下决策流，**自上而下，命中即跳出**：

### 第0层：数据准备（行 836-931）

```
读取持仓 → 获取 token 价格 → 计算利润率 → 获取剩余时间
→ 获取 PTB/crypto 价格 → 判断 direction_correct（布尔）
→ 安全检查：方向✅ 但 token 跌 >10% → 降级为方向❌
```

### 第1层：市场已关闭（remaining ≤ 0）

```
remaining < -30s  →  自动清理：根据 crypto vs PTB 判断胜负，settle $1.00/$0.00
-30s ~ 0s         →  等待结算：每10秒打印状态，轮询 market closed API
                      market closed → 结算清理
```

### 第2层：P0 双曲贴现止盈（remaining > 90s）

```
条件：profit_rate ≥ 阈值 AND remaining > 90s AND best_bid > entry_price
阈值：P0_BASE_PROFIT × (1 + P0_HYPERBOLIC_K × remaining/60)
     = 15% × (1 + 0.15 × 时间因子)
     例：remaining=300s → 阈值 ≈ 26.25%
         remaining=120s → 阈值 ≈ 19.5%

动作：以 best_bid 卖出，标记 "P0早期止盈"
```

**设计意图**：离结算越远，paper profit 越不可靠，要求更高的利润率才止盈。

### 第3层：钻石手 EV 驱动持有（方向✅ + remaining ≤ 120s）

```
计算 EV = p_hat - entry_price（base_rate 查表）

EV > 0.03 或 ATR偏离 ≥ 1.0 或 EV=None  →  💎 持有等结算（continue）
EV 在 0~0.03（微弱信号）                 →  ⚠️ 放行到阶段3/4 精细策略
```

**设计意图**：避免 0.05 ATR 和 2.0 ATR 同等对待。微弱 EV 时不再盲目持有，让阶段3/4 的 EV 分层逻辑做更精细的决策。

### 第4层：方向正确通用持有（方向✅ + remaining > 60s）

```
计算 EV（仅用于日志，不改持有逻辑）
remaining ≤ 180s  →  打印 EV 日志
always             →  continue（持有）
```

**设计意图**：>60s 距离结算还远，价格有充分时间恢复，不需要精细退出。EV 日志用于观察信号质量。

### 第5层：预阶段止损（remaining > 180s + 方向❌）

```
获取 best_bid：

bid < $0.05  →  尝试 P1 对冲：
    有 opposite_token？
    opposite_ask < (1.00 - entry_price - 0.02)？（净利润 > $0.02）
    是 → 买入反向 token，锁定利润 = $1.00 - entry - hedge_price
    否 → 打印 "对冲不划算"，continue 等过期

bid > $0.01  →  渐进降价止损：
    折扣 = 1.0 - min(尝试次数//5 × 0.02, 0.10)
    以 best_bid × 折扣 卖出

无有效 bid  →  等过期结算
```

### 第6层：阶段1（120s < remaining ≤ 180s，方向❌）

```
获取 best_bid：

bid < $0.05  →  P1 对冲（与预阶段相同逻辑）
    对冲成功 → 锁定利润，continue
    对冲失败/不划算 → 等下轮或等过期

bid > $0.05  →  以 best_bid 直接卖出止损
```

### 第7层：阶段2（60s < remaining ≤ 120s，方向❌）

```
获取 best_bid（fallback: current_price × 0.95）

小仓 (size ≤ 5)：
    第1次：best_bid × 0.97 挂单确认（超时4秒）
    第2次：best_bid × 0.90 降价重试

大仓 (size > 5)：
    分3批卖出（50%@99% / 30%@97% / 20%@95%）
    部分成交 → update_position 更新剩余仓位，继续监控
```

### 第8层：阶段3（30s < remaining ≤ 60s）

```
计算 EV：

EV > 0  →  设最低价保护 = max(entry × 0.85, $0.15)
            先尝试 smart_sell（流动性评分策略）
            再尝试 try_sell_with_multiple_prices（激进梯度）
            但成交价不低于最低价

EV ≤ 0  →  无保护，全力平仓
```

**设计意图**：正 EV 时避免恐慌抛售（$0.01 地板价），设合理底价。

### 第9层：阶段4（0s < remaining ≤ 30s）

```
计算 EV：

EV > 0.05     →  💎 持有到结算（高确信度）
EV 0~0.05     →  温和清仓（底价 = max(entry×0.80, $0.15) → $0.10 → $0.05）
EV < 0 或 N/A →  地板价清仓（$0.10 → $0.05 → $0.02 → $0.01）
```

### 第10层：收尾

```
sold=True  →  close_position + 清理 close_attempts
attempted but not sold  →  close_attempts 计数 +1，每5次打印警告
```

---

## 四、决策树流程图

```
每个持仓 (每2秒)
│
├─ remaining ≤ 0 → 结算/清理
│
├─ P0 双曲止盈 (remaining>90 + 高利润)
│
├─ 钻石手 (方向✅ + ≤120s)
│   ├─ EV>0.03 or ATR≥1.0 or N/A → 持有 (continue)
│   └─ EV 0~0.03 → 放行 ↓
│
├─ 方向正确持有 (方向✅ + >60s) → 持有 + EV日志 (continue)
│
├─ 预阶段 (>180s + 方向❌)
│   ├─ bid<$0.05 → P1对冲 or 等过期
│   ├─ bid>$0.01 → 渐进降价止损
│   └─ 无bid → 等过期
│
├─ 阶段1 (120-180s + 方向❌)
│   ├─ bid<$0.05 → P1对冲 or 等过期
│   └─ bid>$0.05 → 直接止损
│
├─ 阶段2 (60-120s + 方向❌)
│   ├─ size≤5 → 挂单确认 → 降价重试
│   └─ size>5 → 分批卖出
│
├─ 阶段3 (30-60s)
│   ├─ EV>0 → 有底价保护的激进平仓
│   └─ EV≤0 → 无保护激进平仓
│
├─ 阶段4 (0-30s)
│   ├─ EV>0.05 → 持有到结算
│   ├─ EV 0~0.05 → 温和清仓
│   └─ EV<0 → 地板价清仓
│
└─ 收尾：close_position 或 attempts++
```

---

## 五、关键防御机制汇总

| 层级 | 机制 | 防御目标 |
|------|------|---------|
| P0 | 双曲贴现止盈 | 远期 paper profit 不可靠 |
| P1 | 反向 token 对冲 | bid 极低（<$0.05）时无法直接卖出 |
| P4 | EV 最低价保护 | 正 EV 时避免恐慌抛售 |
| 安全 | token价格矛盾检测 | PTB 数据错误 / 边界反转 |
| 执行 | 挂单确认 + 取消重试 | LIVE 挂单未成交（幽灵持仓） |
| 执行 | 分批出货 | 大仓位一次性卖出的滑点问题 |
| 执行 | 渐进降价 | 同一价格反复失败 |
| 清理 | 过期30秒自动关闭 | 结算后的残留持仓 |
| 记录 | `record_outcome` | 胜率校准数据回流 |

---

## 六、外部依赖

- **Polymarket CLOB CLI**：`polymarket clob create-order / cancel-all / balance`
- **Polymarket REST API**：订单簿、midpoint、market status
- **Binance API**：实时价格、K线（ATR 计算）
- **`ai_trader.base_rate`**：胜率查表 `get_base_rate(diff_in_atr)` + 结果记录 `record_outcome()`
- **Telegram Bot API**：推送通知
