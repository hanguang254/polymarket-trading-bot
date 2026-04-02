"""
贝叶斯序贯更新引擎 v2.1

基于论文: Real-Time Bayesian Signal Processing Agent Decision Architecture

v2.1 改进:
  - 保留 Δprice 增量后验，继续抑制高自相关样本跑飞
  - 补充 gap/ATR/剩余时间 的状态信号，避免“已远离 PTB 但 conf 仍接近 0”
  - 持续强势翻面时执行软重置，减少早期错误方向的锚定

核心公式:
  log P(H|D) = log P(H) + Σ w_k · log P(D_k|H) - log Z
  其中 w_k = decay^k 为衰减权重
"""
import math


class BayesianUpdater:
    """
    二元市场贝叶斯序贯更新器（v2: 抗自相关）

    改进:
      1. 首次更新用 price vs PTB（水平信号）
      2. 后续更新用 Δprice（变化量信号），消除自相关
      3. 每次更新权重衰减 decay^n，避免重复信号累积
      4. 后验概率上限 0.90（单边）
    """

    SOFT_RESET_GAP_ATR = 0.60
    SOFT_RESET_STREAK = 3
    SOFT_RESET_SEED = 0.58
    GATE_STATE_WEIGHT_ALIGNED = 0.65
    GATE_STATE_WEIGHT_CONFLICT = 0.85

    def __init__(self, prior_up=0.5, atr_val=None):
        self.prior_up = max(0.01, min(0.99, prior_up))
        self.atr_real = atr_val is not None and atr_val > 0  # v14.1: ATR是否来自真实K线数据且有效
        self.atr_val = atr_val or 1.0
        self.log_posterior_up = math.log(self.prior_up)
        self.log_posterior_down = math.log(1 - self.prior_up)
        self.n_updates = 0
        self.last_price = None
        self.current_price = None
        self.current_ptb = None
        self.samples = []
        self.decay = 0.85  # 每次更新权重衰减15%
        self._flip_gap_side = 0
        self._flip_streak = 0
        self.reset_count = 0

    def set_prior_bias(self, p_up_bias):
        """v14.1: 用快速方向信号设置先验偏置，加速收敛

        只在n_updates==0时生效（首次更新前），避免覆盖已有证据。
        p_up_bias: float [0.35, 0.65]
        """
        if self.n_updates > 0:
            return  # 已有证据，不覆盖
        p_up_bias = max(0.35, min(0.65, p_up_bias))
        self.prior_up = p_up_bias
        self.log_posterior_up = math.log(p_up_bias)
        self.log_posterior_down = math.log(1.0 - p_up_bias)

    @staticmethod
    def _clip_prob(value):
        return max(0.07, min(0.93, value))

    @staticmethod
    def _direction_from_p_up(p_up):
        return "UP" if p_up >= 0.5 else "DOWN"

    @staticmethod
    def _sign_from_direction(direction):
        return 1 if direction == "UP" else -1

    def _signal_from_p_up(self, p_up):
        p_up = self._clip_prob(p_up)
        direction = self._direction_from_p_up(p_up)
        p_hat = p_up if direction == "UP" else (1.0 - p_up)
        confidence = min(abs(p_up - 0.5) * 2.0, 1.0)
        return {
            "direction": direction,
            "p_hat": p_hat,
            "p_up": p_up,
            "p_down": 1.0 - p_up,
            "confidence": confidence,
        }

    def _incremental_signal(self):
        p_up, _ = self.get_posterior()
        return self._signal_from_p_up(p_up)

    def _state_signal(self, remaining_seconds=None, price=None, ptb=None):
        price = self.current_price if price is None else price
        ptb = self.current_ptb if ptb is None else ptb

        if (
            remaining_seconds is None
            or remaining_seconds <= 0
            or price is None
            or ptb is None
            or self.atr_val <= 0
        ):
            return None

        sigma_per_min = self.atr_val / 1.5
        sigma_total = sigma_per_min * math.sqrt(remaining_seconds / 60.0)
        if sigma_total <= 0:
            return None

        gap = price - ptb
        z = abs(gap) / sigma_total
        base_p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        p_up = base_p if gap >= 0 else (1.0 - base_p)
        state = self._signal_from_p_up(p_up)
        state["gap"] = gap
        state["remaining_seconds"] = remaining_seconds
        return state

    def _gate_signal(self, remaining_seconds=None, price=None, ptb=None):
        incremental = self._incremental_signal()
        state = self._state_signal(
            remaining_seconds=remaining_seconds,
            price=price,
            ptb=ptb,
        )
        if not state:
            gate = dict(incremental)
            gate["source"] = "incremental_only"
            gate["conflict"] = False
            return gate, incremental, None

        aligned = state["direction"] == incremental["direction"]
        state_weight = (
            self.GATE_STATE_WEIGHT_ALIGNED
            if aligned else self.GATE_STATE_WEIGHT_CONFLICT
        )
        inc_weight = 1.0 - state_weight
        gate_p_up = (state["p_up"] * state_weight) + (incremental["p_up"] * inc_weight)
        gate = self._signal_from_p_up(gate_p_up)
        gate["source"] = "state_incremental_aligned" if aligned else "state_override"
        gate["conflict"] = not aligned
        return gate, incremental, state

    def _soft_reset(self, side):
        seed_up = self.SOFT_RESET_SEED if side > 0 else (1.0 - self.SOFT_RESET_SEED)
        self.log_posterior_up = math.log(seed_up)
        self.log_posterior_down = math.log(1.0 - seed_up)
        self.n_updates = 0
        self.last_price = None
        self._flip_gap_side = 0
        self._flip_streak = 0
        self.reset_count += 1

    def _maybe_soft_reset(self, gap):
        if self.atr_val <= 0 or self.n_updates < 2:
            self._flip_gap_side = 0
            self._flip_streak = 0
            return False

        gap_atr = abs(gap) / self.atr_val
        if gap_atr < self.SOFT_RESET_GAP_ATR:
            self._flip_gap_side = 0
            self._flip_streak = 0
            return False

        gap_side = 1 if gap >= 0 else -1
        raw_direction = self._incremental_signal()["direction"]
        raw_side = self._sign_from_direction(raw_direction)
        if gap_side == raw_side:
            self._flip_gap_side = 0
            self._flip_streak = 0
            return False

        if gap_side == self._flip_gap_side:
            self._flip_streak += 1
        else:
            self._flip_gap_side = gap_side
            self._flip_streak = 1

        if self._flip_streak < self.SOFT_RESET_STREAK:
            return False

        self._soft_reset(gap_side)
        return True

    def _likelihood(self, deviation):
        """
        计算似然 P(D|H)，输入为标准化偏离量

        使用 sigmoid: P(D|UP) = sigmoid(k * deviation)
        """
        if self.atr_val <= 0:
            return 0.5, 0.5

        deviation = max(-3.0, min(3.0, deviation))

        k = 0.8
        z = k * deviation
        p_up = 1.0 / (1.0 + math.exp(-z))
        p_up = max(0.15, min(0.85, p_up))
        p_down = 1.0 - p_up

        return p_up, p_down

    def update(self, price, ptb):
        """
        用一个新的价格观测更新后验概率

        第1次: 用 (price - ptb) / ATR（水平偏离，初始方向信号）
        第2次+: 用 (price - last_price) / ATR（变化量，独立增量信息）
        """
        self.current_price = price
        self.current_ptb = ptb
        gap = price - ptb
        reset_triggered = self._maybe_soft_reset(gap)

        if self.last_price is None:
            # 首次：水平偏离信号
            deviation = gap / self.atr_val
        else:
            # 后续：价格变化量（与PTB方向一致=正信号）
            delta = price - self.last_price
            deviation = delta / self.atr_val

        self.last_price = price

        likelihood_up, likelihood_down = self._likelihood(deviation)

        # 衰减权重：第n次更新权重 = decay^n
        weight = self.decay ** self.n_updates
        self.log_posterior_up += weight * math.log(likelihood_up)
        self.log_posterior_down += weight * math.log(likelihood_down)

        self.n_updates += 1
        self.samples.append({
            "price": price,
            "ptb": ptb,
            "gap": round(gap, 2),
            "deviation": round(deviation, 4),
            "weight": round(weight, 3),
            "likelihood_up": round(likelihood_up, 4),
            "reset": reset_triggered,
        })

        return self.get_posterior()

    def get_posterior(self):
        """
        归一化并返回后验概率（上限 0.90，防止自相关累积过高）

        Returns:
            (p_up, p_down)
        """
        # log-sum-exp 归一化（数值稳定）
        max_log = max(self.log_posterior_up, self.log_posterior_down)
        log_sum = max_log + math.log(
            math.exp(self.log_posterior_up - max_log) +
            math.exp(self.log_posterior_down - max_log)
        )

        p_up = math.exp(self.log_posterior_up - log_sum)
        p_down = math.exp(self.log_posterior_down - log_sum)

        # 后验上限 0.93: 衰减因子+Δprice已防跑飞，留出融合空间
        p_up = self._clip_prob(p_up)
        p_down = 1.0 - p_up

        return round(p_up, 6), round(p_down, 6)

    def get_direction_and_confidence(self):
        """
        返回原始增量后验的方向和置信度

        Returns:
            (direction, p_hat, confidence):
              - direction: "UP" 或 "DOWN"
              - p_hat: 领先方向的后验概率（原始增量信号）
              - confidence: 置信度 = |p_up - 0.5| * 2，范围 [0, 1]
        """
        signal = self._incremental_signal()
        return signal["direction"], signal["p_hat"], signal["confidence"]

    def get_summary(self, remaining_seconds=None):
        """返回更新摘要（用于日志/预筛），默认输出状态+增量融合后的 gate 信号。"""
        gate, incremental, state = self._gate_signal(remaining_seconds=remaining_seconds)
        return {
            "direction": gate["direction"],
            "p_hat": round(gate["p_hat"], 4),
            "p_up": round(gate["p_up"], 4),
            "p_down": round(gate["p_down"], 4),
            "confidence": round(gate["confidence"], 4),
            "source": gate["source"],
            "conflict": gate["conflict"],
            "incremental_direction": incremental["direction"],
            "incremental_p_hat": round(incremental["p_hat"], 4),
            "incremental_p_up": round(incremental["p_up"], 4),
            "incremental_confidence": round(incremental["confidence"], 4),
            "state_direction": state["direction"] if state else None,
            "state_p_hat": round(state["p_hat"], 4) if state else None,
            "state_p_up": round(state["p_up"], 4) if state else None,
            "state_confidence": round(state["confidence"], 4) if state else None,
            "gap": round(state["gap"], 2) if state else None,
            "remaining_seconds": round(state["remaining_seconds"], 2) if state else None,
            "n_updates": self.n_updates,
            "prior_up": round(self.prior_up, 4),
            "reset_count": self.reset_count,
        }
