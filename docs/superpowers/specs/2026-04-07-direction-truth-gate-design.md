# Direction Truth Gate — 设计文档

**版本**: v15.0
**日期**: 2026-04-07
**作者**: brainstorming session
**状态**: 待 review → 待 implementation plan

---

## 1. 背景与动机

### 1.1 现状
当前 Polymarket 交易 bot 的方向判断分散在三个独立位置：

| 策略 | 当前方向来源 | 文件 |
|---|---|---|
| 做市挂单 / 双边伏击 | `get_fast_direction()` 加权投票 | [auto_bot_v3.py:1483](../../../auto_bot_v3.py#L1483) |
| Sniper 抢入 | `get_fast_direction()` + 贝叶斯 prior | [auto_bot_v3.py:1965](../../../auto_bot_v3.py#L1965) |
| 采样期 prior 注入 | `get_fast_direction()` | [auto_bot_v3.py:2415](../../../auto_bot_v3.py#L2415) |
| Endgame 30s 狙击 | Chainlink 直读 + EV 模型 | [position_monitor.py](../../../position_monitor.py)（具体行号待 plan 阶段定位）|
| EV 止损退出 | CL 主导 + CL-BN skew 拦截 | [position_monitor.py:4574](../../../position_monitor.py#L4574) |

`get_fast_direction()` 内部是 3 信号 **加权连续投票**（权重 0.40/0.35/0.25）：
- Binance OFI（10s 订单流失衡）
- BN-CL Cross Lead（Binance 价 vs Chainlink 价 / ATR）
- Polymarket OBI（5 档对盘深度失衡）

### 1.2 问题
1. **加权投票可被单信号支配**：Binance OFI 权重 0.40，理论上可独自决定方向，即使 BN-CL Cross Lead 反对
2. **多策略各自判方向，没有"真值"概念**：做市信任 fast_direction，Endgame 信任 Chainlink，Sniper 信任 fast_direction + 贝叶斯，互相不交叉验证
3. **Chainlink 的方向判断没有独立预言机做交叉验证**：Pyth 已接入但只用于 fallback，没有用于"双预言机一致性"检测

### 1.3 目标
建立一个**策略决策前置闸门**：
- 在所有"开仓"类动作之前调用
- 用 3 个独立源（CL + BN + Pyth）做交叉验证
- 输出"真方向"或"无共识 → 拦截"
- EV 退出 / 止损路径**不受影响**（保持现有逻辑）

---

## 2. 范围

### 2.1 In Scope

| 接入点 | 模式 | 文件 |
|---|---|---|
| 做市 / 双边伏击 ambush gate | balanced (2/3) | [auto_bot_v3.py:1483](../../../auto_bot_v3.py#L1483) |
| Sniper thread prior seeding | strict (3/3) | [auto_bot_v3.py:1965](../../../auto_bot_v3.py#L1965) |
| 采样期 prior 注入 | balanced (2/3) | [auto_bot_v3.py:2415](../../../auto_bot_v3.py#L2415) |
| Endgame 入场 | strict (3/3) | [position_monitor.py](../../../position_monitor.py) |

### 2.2 Out of Scope（明确不动）
- EV 止损退出逻辑（已有 `EV_SKEW_BLOCK_ATR` 机制，自成体系）
- 普通价格止损 / 硬止损 / breakeven
- `get_fast_direction()` 本身（保留作为 prior_bias 二级精化）
- LMSR / 流动性 / 仓位计算
- Chainlink / Pyth / Binance 数据源本身（只读消费）

---

## 3. 架构

### 3.1 模块分层

```
┌─────────────────────────────────────────────────────────┐
│  ai_trader/gbm_p_up.py  (NEW, ~25 lines, 无状态)        │
│                                                         │
│  def gbm_p_up(price, strike, atr, remaining_sec):       │
│      """P(price_T > strike) via Gaussian erf"""         │
│      sigma_per_min = atr / EV_ATR_SIGMA_RATIO  # 1.5    │
│      sigma_total = sigma_per_min * sqrt(rem/60)         │
│      gap = price - strike                               │
│      z = abs(gap) / sigma_total                         │
│      base = 0.5*(1 + erf(z/sqrt(2)))                    │
│      return base if gap >= 0 else 1 - base              │
└─────────────────────────────────────────────────────────┘
                ↑                          ↑
                │ 复用                      │ 复用
                │                          │
┌───────────────┴────────────┐  ┌──────────┴───────────────┐
│ bayesian_engine.py         │  │ direction_truth_gate.py  │
│ （5 行重构）                │  │ （NEW，~150 行）          │
│                            │  │                          │
│ _state_signal() 内部        │  │ check_direction_truth(   │
│   gbm_p_up(...) 替换原公式  │  │   coin, strike,          │
│                            │  │   deadline_ts, atr,      │
│ ✅ 语义不变                 │  │   strict=False)          │
│ ✅ 单测保护                 │  │   → DirectionTruth       │
└────────────────────────────┘  └──────────────────────────┘
                                          ↑
                                          │ 调用方
            ┌─────────────────────────────┴────────────────────┐
            │                                                  │
            ↓                                                  ↓
┌──────────────────────┐                          ┌─────────────────────┐
│ auto_bot_v3.py       │                          │ position_monitor.py │
│   :1483 (做市/伏击)  │                          │   Endgame 入场点    │
│   :1965 (Sniper)     │                          │   strict=True       │
│   :2415 (sampling)   │                          │                     │
│   strict=False (做市)│                          │                     │
│   strict=True (狙击) │                          │                     │
└──────────────────────┘                          └─────────────────────┘
```

### 3.2 文件清单

| 文件 | 行数预估 | 状态 | 职责 |
|---|---|---|---|
| `ai_trader/gbm_p_up.py` | ~25 | NEW | 无状态 GBM 闭式解，唯一真值源 |
| `ai_trader/direction_truth_gate.py` | ~150 | NEW | 三源拉数据 → 投票 → 聚合 → 返回 DirectionTruth |
| `ai_trader/bayesian_engine.py` | -10 +5 | MODIFY | `_state_signal` 内部改用 helper（不改外部行为）|
| `auto_bot_v3.py` | +20 | MODIFY | 在 1483/1965/2415 三处加闸门检查 |
| `position_monitor.py` | +10 | MODIFY | Endgame 入场处加 `strict=True` 闸门 |
| `tests/test_gbm_p_up.py` | NEW | NEW | gbm 数学单测 + bayesian 重构等价性测试 |
| `tests/test_direction_truth_gate.py` | NEW | NEW | 5 类投票状态单测 |
| `.env.example` | +10 | MODIFY | 新增 `DIR_TRUTH_*` 配置 |

### 3.3 为什么这样分层（Approach B）

考虑过另外两种实现路径：
- **Approach A（闭门造车）**：在 `direction_truth_gate.py` 内嵌 GBM 公式，不动 `bayesian_engine.py`。优点零侵入；缺点公式存两份，未来调 sigma 公式漏改一处即方向判错。
- **Approach C（拿 BayesianUpdater 当工具）**：每次闸门调用 new 3 个 throwaway BayesianUpdater 实例。缺点：BayesianUpdater 是带状态的序贯更新器（衰减权重、soft reset），用一次就扔等于把跑车当自行车骑。

**选 Approach B 的核心理由**：
1. **单一真值源**：GBM 公式只在 `gbm_p_up.py` 一处，未来调整 sigma 比率/erf 实现都只改一处
2. **复用 .env 现有常量**：`EV_ATR_SIGMA_RATIO=1.5` 已经存在并被 bayesian_engine 使用，零新增魔法数字
3. **重构风险可控**：`_state_signal` 是私有方法，5 行重构 + bit-identical 等价性单测保护

---

## 4. 公共 API

### 4.1 类型定义

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class SourceVote:
    source: str            # "CL" | "BN" | "Pyth"
    price: Optional[float]
    p_up: Optional[float]  # P(end_T > strike)，由 gbm_p_up() 算出
    vote: Optional[str]    # "UP" | "DOWN" | "NEUTRAL" | None(stale)
    stale: bool
    age_sec: float
    error: Optional[str] = None

@dataclass
class DirectionTruth:
    direction: Optional[str]      # "UP"/"DOWN"/None；None = 闸门拦截
    confidence: float             # [0,1]，agreeing 源的 p_up 均值映射
    block_reason: Optional[str]   # 拦截原因（用于日志）
    votes: dict[str, SourceVote]  # 三源详情
    mode: str                     # "strict" | "balanced" | "degraded" | "blocked"
    n_alive_sources: int          # 0..3
    n_agreeing: int               # 同意主方向的源数
```

### 4.2 入口函数

```python
def check_direction_truth(
    coin: str,
    strike: float,
    deadline_ts: float,           # 合约 resolve 的 unix 秒
    atr_val: float,
    *,
    strict: bool = False,         # True for Sniper/Endgame, False for 做市
    now_ts: Optional[float] = None,  # 注入用于单测
) -> DirectionTruth:
    """三源 GBM 隐含概率交叉验证 + 2/3 多数决闸门。"""
```

---

## 5. 行为规范

### 5.1 单源投票逻辑

每个源（CL/BN/Pyth）执行 4 步：

1. **取价 + 取年龄**：从对应 stream 拿 `(price, last_update_ts)`，算 `age = now - last_update_ts`
2. **stale 检查**：`age > STALE_THRESHOLDS[source]` → 返回 stale 票
3. **GBM 算概率**：`p_up = gbm_p_up(price, strike, atr_val, deadline_ts - now_ts)`
4. **投票判定**：
   - `p_up >= 0.55` → vote = "UP"
   - `p_up <= 0.45` → vote = "DOWN"
   - 否则 → vote = "NEUTRAL"（弃权，不计入任何一边）

### 5.2 聚合决策表（核心规约）

| n_alive | UP票 | DOWN票 | NEUTRAL票 | strict 模式 | balanced 模式 |
|:-:|:-:|:-:|:-:|:-:|:-:|
| 3 | 3 | 0 | 0 | ✅ UP | ✅ UP |
| 3 | 2 | 0 | 1 | ❌ 拦 | ✅ UP（2/3 多数）|
| 3 | 2 | 1 | 0 | ❌ 拦 | ✅ UP（2/3 多数）|
| 3 | 1 | 1 | 1 | ❌ 拦 | ❌ 拦 |
| 3 | 0 | 0 | 3 | ❌ 拦 | ❌ 拦 |
| 2 | 2 | 0 | 0 | ✅ UP（degraded）| ✅ UP（degraded）|
| 2 | 1 | 1 | 0 | ❌ 拦 | ❌ 拦 |
| 2 | 1 | 0 | 1 | ❌ 拦 | ❌ 拦 |
| ≤1 | * | * | * | ❌ 拦 | ❌ 拦 |

> **DOWN 方向的所有行对称存在**：把上表所有"UP票/DOWN票"列对调、决策中的 UP 改 DOWN 即可，不再赘述。

**规约伪代码**：
```python
if n_alive < 2:
    return BLOCK("not enough live sources")

if strict:
    required = n_alive   # 严格模式：所有活源必须同意
else:
    required = 2         # 平衡模式：固定 2 票门槛

if max(up_votes_count, down_votes_count) >= required:
    return DECIDE(direction)
else:
    return BLOCK("no consensus")
```

### 5.3 Confidence 计算

```python
agreeing = [v for v in alive if v.vote == direction]
mean_p = mean(v.p_up for v in agreeing)
confidence = abs(mean_p - 0.5) * 2.0       # 映射到 [0,1]
confidence *= DIR_TRUTH_SHRINKAGE           # .env 默认 0.85
```

**为什么 shrinkage 不复用 `P_WIN_SHRINKAGE=0.80`**：
- `P_WIN_SHRINKAGE` 是策略侧 EV 计算的收缩
- `DIR_TRUTH_SHRINKAGE` 是闸门 confidence 的收缩
- 复用会出现"改一个影响两个"的耦合，独立配置避免双重打折

### 5.4 Stale 检测

| 源 | 默认阈值 | 理由 |
|---|---|---|
| Chainlink | 30s | Chainlink heartbeat 大约 30s 一次 |
| Pyth | 30s | Pyth Hermes SSE 大约相同量级 |
| Binance | 5s | trade tape 应是亚秒级，5s 没数据说明断流 |

可由 .env 覆盖：`DIR_TRUTH_STALE_CL_SEC` / `DIR_TRUTH_STALE_BN_SEC` / `DIR_TRUTH_STALE_PYTH_SEC`

### 5.5 关键设计权衡

| 决策 | 选择 | 理由 |
|---|---|---|
| 闸门 vs `fast_direction.py` 的关系 | **共存**：闸门在前，fast_direction 在后 | 闸门管"能不能开"，fast_direction 管"以多少 prior 开"。职责正交。 |
| stale 时是否能 fallback 到 fast_direction | **不允许** | 否则 shadow 模式没法测真实拦截率 |
| 是否缓存 gate 结果 | **不缓存（v1）** | 调用频次低，erf 微秒级，提早优化是 evil |
| Shadow 模式默认 | **开**：`DIR_TRUTH_SHADOW=1` | 灰度发布安全默认 |

---

## 6. 调用方接入

### 6.1 做市路径（balanced）

```python
# auto_bot_v3.py:1483 (ambush gate) — 新增
truth = check_direction_truth(coin, strike, deadline_ts, atr_val, strict=False)
log_gate_decision(truth, call_site="ambush_1483")

if truth.direction is None:
    if not SHADOW_MODE:
        continue            # 真拦截

# 老的 fast_direction 逻辑保留作为二级精化
_fast = get_fast_direction(coin, ptb, atr_val, ...)
```

类似改动应用于 :1965 和 :2415。

### 6.2 Sniper / Endgame 路径（strict）

```python
truth = check_direction_truth(coin, strike, deadline_ts, atr_val, strict=True)
if truth.direction is None and not SHADOW_MODE:
    continue
```

---

## 7. 配置（.env）

```bash
# ── 17. 方向真值闸门（Direction Truth Gate, v15.0）──
# 闸门总开关：1=启用，0=完全跳过（回退老逻辑）
DIR_TRUTH_GATE_ENABLED=1
# Shadow 模式：1=只记日志不真拦截（默认开，灰度首选）
DIR_TRUTH_SHADOW=1

# 单源 stale 阈值（秒）
DIR_TRUTH_STALE_CL_SEC=30
DIR_TRUTH_STALE_BN_SEC=5
DIR_TRUTH_STALE_PYTH_SEC=30

# 单源投票概率阈值（GBM 算出 p_up 后比这俩判 UP/DOWN/NEUTRAL）
DIR_TRUTH_VOTE_UP_P=0.55
DIR_TRUTH_VOTE_DOWN_P=0.45

# 闸门 confidence 收缩系数（独立于 P_WIN_SHRINKAGE）
DIR_TRUTH_SHRINKAGE=0.85
```

---

## 8. 日志（`logs/dir_truth_gate.jsonl`）

每次闸门调用一行 JSON：

```json
{
  "ts": 1712486400.123,
  "coin": "BTC",
  "strike": 100000.0,
  "deadline_in": 87.3,
  "atr": 80.5,
  "call_site": "ambush_1483",
  "strict": false,
  "votes": {
    "CL":   {"price": 100023.1, "p_up": 0.612, "vote": "UP", "stale": false, "age_sec": 0.4},
    "BN":   {"price": 100027.5, "p_up": 0.625, "vote": "UP", "stale": false, "age_sec": 0.1},
    "Pyth": {"price": 100018.2, "p_up": 0.589, "vote": "UP", "stale": false, "age_sec": 1.2}
  },
  "decision": "UP",
  "confidence": 0.205,
  "mode": "balanced",
  "n_alive": 3,
  "n_agreeing": 3,
  "shadow": true,
  "would_have_blocked": false
}
```

---

## 9. 灰度发布计划

```
┌────────────┬──────────────────────────────────────────────┬──────────────┐
│  阶段       │  动作                                         │  回退         │
├────────────┼──────────────────────────────────────────────┼──────────────┤
│  0. 合并    │  PR 合并，shadow=1 默认开                     │  revert PR    │
│  1. 观察    │  跑 24-48h，收集 dir_truth_gate.jsonl        │  ENABLED=0    │
│  2. 分析    │  脚本汇总：拦截率、和实际亏损的相关性          │  无操作       │
│  3. 切实盘  │  shadow=0，闸门开始真拦截                     │  shadow=1     │
│  4. 调参    │  根据数据调 VOTE_UP_P / SHRINKAGE              │  改回默认     │
└────────────┴──────────────────────────────────────────────┴──────────────┘
```

**判断阶段 3 切实盘的标准**：
1. shadow 期间拦截率 < 30%（避免过度阻断）
2. 被拦截的样本中 ≥ 60% 实际是亏损单（证明判别力）
3. 没有"批量误杀"——单合约连续 5+ 次拦截要人工 review

---

## 10. 测试策略

| 测试 | 文件 | 覆盖什么 |
|---|---|---|
| GBM 数学单测 | `tests/test_gbm_p_up.py` | z=0→0.5、对称性、极端值、bayesian_engine 重构前后 bit-identical |
| 聚合逻辑单测 | `tests/test_direction_truth_gate.py` | 决策表 9 行全覆盖 |
| Stale 检测单测 | 同上 | 各源 stale 阈值边界、混合 stale 状态 |
| Shadow 集成观察 | 灰度阶段 1-2 | 24h 在线观察 |

---

## 11. 待 writing-plans 阶段澄清的开放问题

1. **Endgame 入场具体在 [position_monitor.py](../../../position_monitor.py) 哪一行**？需要 grep 定位
2. **`chainlink_stream` / `binance_api` / `pyth_stream` 是否暴露 `last_update_ts`**？如果没有，得在各自模块加 `get_age()` 访问器
3. **是否需要复用 `binance_api.get_tick_momentum()` 的 trade tape 缓存**？v1 不需要，记入 future-work

---

## 12. 不在本期范围（future work）

- 第 4 路源（Polymarket 自家盘口微价）
- 闸门结果缓存（如热点合约）
- 时间衰减阈值（截止越近、阈值越低）
- 闸门和 EV 退出共享 stale 检测

---

## 13. Changelog

- **2026-04-07** v0.1: 初稿，brainstorming 完成 3 节验收
- **2026-04-07** v0.2: 实施 + code-review 修复
  - **实施差异 vs spec**:
    - Endgame 入场点找到的是 `auto_bot_v3.py:_try_endgame_entry()`（NOT position_monitor.py，§2.1 推测有误）
    - 三源 stream（CL/BN/Pyth）都已暴露 `get_snapshot(coin)` 返回 `{price, age_ms, last_update_ts}`，不需要新加 accessor（§11 开放问题已解）
  - **代码审查修复**（P0/P1/P2）:
    - **P0-1**: Binance 单例导入名错（`binance_stream` → `price_stream`）。修了 + 把 import 提到 try 外让 ImportError 大声失败而不是被吞
    - **P0-2**: 加了 `DirectionTruth.is_blocked` 属性 — `mode == "disabled"` 时正确 fall through，不再当成 block。4 个调用点全部改用 `is_blocked`
    - **P1-1**: Sniper / Sampling 的 prior-seeding 站点现在检查 `is_shadow_mode()`，shadow 模式下不抑制 prior bias
    - **P1-3**: JSONL 日志写入用 `threading.Lock` 串行化
    - **P2-1**: `n_alive=2` strict 模式现在标 `"degraded"`（与 §5.2 第 6 行对齐）
    - **P2-2**: 用符号化 call_site 标签（`ambush_gate`/`endgame_entry`/`sniper_prior_seed`/`sampling_prior_seed`）代替行号
    - **P2-4**: `SourceVote.__post_init__` 强制不变量（stale=False ⟹ vote ∈ {UP,DOWN,NEUTRAL} 且 p_up 是浮点数）
    - **P2-7**: `gbm_p_up` 在 NaN/Inf 输入时返回 0.5
    - **P2-11**: confidence 截断到 `[0, 1]`
  - **P1-2 信息披露（故意的行为变更，不是 bug）**:
    - 重构前 `bayesian_engine._state_signal` 硬编码 `sigma_per_min = atr / 1.5`
    - 重构后调 `gbm_p_up`，从 env 读 `EV_ATR_SIGMA_RATIO`（默认 1.5）
    - 这意味着 `bayesian_engine` 现在和 `position_monitor.py` 共享这个 env 变量
    - 默认值 (=1.5) 时 bit-identical（由 `test_state_signal_matches_inline_gbm_formula_pre_refactor` 验证，该测试现已显式 pin `EV_ATR_SIGMA_RATIO=1.5`）
    - **运维 action**: 如果生产 .env 里 `EV_ATR_SIGMA_RATIO != 1.5`，本 PR 会静默改变 `bayesian_engine._state_signal` 输出。部署前要确认
  - **测试统计**: gbm_p_up=11, direction_truth_gate=24（含 4 个 P0 regression 测试）, bayesian_engine=3（含 1 个 equivalence）。共 38 个新测试全绿。完整套件 110 通过 vs 基线 74 通过，同 18 个 pre-existing Linux-only 失败
