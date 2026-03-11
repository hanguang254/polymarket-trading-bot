"""
贝叶斯序贯更新引擎 v1.0

基于论文: Real-Time Bayesian Signal Processing Agent Decision Architecture

核心公式:
  log P(H|D) = log P(H) + Σ log P(D_k|H) - log Z

用途: 替代 auto_bot_v3.py 中简单的 gap 趋势分析，
     在预热期对每个价格采样点做后验概率更新，
     输出更准确的 p̂（真实概率估计）
"""
import math


class BayesianUpdater:
    """
    二元市场贝叶斯序贯更新器

    在 log-space 中累积更新，数值稳定:
        log P(UP|D1..Dt) = log P(UP) + Σ log P(Dk|UP)
        log P(DOWN|D1..Dt) = log P(DOWN) + Σ log P(Dk|DOWN)
        归一化后得到后验概率
    """

    def __init__(self, prior_up=0.5, atr_val=None):
        """
        Args:
            prior_up: UP 的先验概率（通常用市场赔率 up_odds）
            atr_val: ATR 值，用于将价格偏离标准化
        """
        self.prior_up = max(0.01, min(0.99, prior_up))
        self.atr_val = atr_val or 1.0
        self.log_posterior_up = math.log(self.prior_up)
        self.log_posterior_down = math.log(1 - self.prior_up)
        self.n_updates = 0
        self.samples = []  # 记录所有采样点

    def _likelihood(self, price, ptb):
        """
        计算似然 P(D|H)

        模型假设:
          - 如果 H=UP 为真，价格应倾向于 > PTB
          - 如果 H=DOWN 为真，价格应倾向于 < PTB
          - 偏离程度（ATR 倍数）决定似然强度

        使用 sigmoid 函数将偏离映射到似然:
          P(D|UP)  = sigmoid(k * (price - ptb) / atr)
          P(D|DOWN) = 1 - P(D|UP)

        k 控制似然的敏感度，k 越大对偏离越敏感
        """
        if self.atr_val <= 0:
            return 0.5, 0.5

        deviation = (price - ptb) / self.atr_val

        # Clamp: 限制单次偏离最大 ±3 ATR
        # 避免 BTC gap=-431 (12x ATR) 一步推到 sigmoid 饱和
        # 需要多次一致信号才能累积高置信度
        deviation = max(-3.0, min(3.0, deviation))

        # k=0.8: 降低敏感度
        # 3 ATR 偏离 → sigmoid(2.4) ≈ 0.92，留有余量
        # 1 ATR 偏离 → sigmoid(0.8) ≈ 0.69，温和信号
        k = 0.8
        z = k * deviation
        p_up = 1.0 / (1.0 + math.exp(-z))

        # 限制极端值，单次观测最多推到 0.85
        p_up = max(0.15, min(0.85, p_up))
        p_down = 1.0 - p_up

        return p_up, p_down

    def update(self, price, ptb):
        """
        用一个新的价格观测更新后验概率

        Args:
            price: 当前加密货币价格（来自 Binance）
            ptb: Price To Beat（来自 Polymarket）

        Returns:
            (posterior_up, posterior_down): 更新后的后验概率
        """
        likelihood_up, likelihood_down = self._likelihood(price, ptb)

        # log-space 累积（公式3: log P(H|D) = log P(H) + Σ log P(Dk|H)）
        self.log_posterior_up += math.log(likelihood_up)
        self.log_posterior_down += math.log(likelihood_down)

        self.n_updates += 1
        self.samples.append({
            "price": price,
            "ptb": ptb,
            "gap": round(price - ptb, 2),
            "likelihood_up": round(likelihood_up, 4),
        })

        return self.get_posterior()

    def get_posterior(self):
        """
        归一化并返回后验概率

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

        return round(p_up, 6), round(p_down, 6)

    def get_direction_and_confidence(self):
        """
        返回贝叶斯推断的方向和置信度

        Returns:
            (direction, p_hat, confidence):
              - direction: "UP" 或 "DOWN"
              - p_hat: 领先方向的后验概率（用于 EV 和 Kelly 计算）
              - confidence: 置信度 = |p_up - 0.5| * 2，范围 [0, 1]
        """
        p_up, p_down = self.get_posterior()

        if p_up >= p_down:
            direction = "UP"
            p_hat = p_up
        else:
            direction = "DOWN"
            p_hat = p_down

        # 置信度: 偏离 0.5 的程度
        confidence = abs(p_up - 0.5) * 2.0
        confidence = min(confidence, 1.0)

        return direction, p_hat, confidence

    def get_summary(self):
        """返回更新摘要（用于日志）"""
        p_up, p_down = self.get_posterior()
        direction, p_hat, confidence = self.get_direction_and_confidence()
        return {
            "direction": direction,
            "p_hat": round(p_hat, 4),
            "p_up": round(p_up, 4),
            "p_down": round(p_down, 4),
            "confidence": round(confidence, 4),
            "n_updates": self.n_updates,
            "prior_up": round(self.prior_up, 4),
        }
