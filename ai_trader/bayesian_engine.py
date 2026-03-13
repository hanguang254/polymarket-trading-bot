"""
贝叶斯序贯更新引擎 v2.0

基于论文: Real-Time Bayesian Signal Processing Agent Decision Architecture

v2.0 修复:
  - 使用价格变化增量(Δprice)而非价格水平，解决自相关问题
  - 加入信息衰减因子，后续重复信号权重递减
  - 后验上限 0.90，防止12个高自相关样本推出 p̂=0.97

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

    def __init__(self, prior_up=0.5, atr_val=None):
        self.prior_up = max(0.01, min(0.99, prior_up))
        self.atr_val = atr_val or 1.0
        self.log_posterior_up = math.log(self.prior_up)
        self.log_posterior_down = math.log(1 - self.prior_up)
        self.n_updates = 0
        self.last_price = None
        self.samples = []
        self.decay = 0.85  # 每次更新权重衰减15%

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
        if self.last_price is None:
            # 首次：水平偏离信号
            deviation = (price - ptb) / self.atr_val
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
            "gap": round(price - ptb, 2),
            "deviation": round(deviation, 4),
            "weight": round(weight, 3),
            "likelihood_up": round(likelihood_up, 4),
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
        p_up = max(0.07, min(0.93, p_up))
        p_down = 1.0 - p_up

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
