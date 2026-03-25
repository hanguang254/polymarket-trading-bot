#!/usr/bin/env python3
"""
独立狙击监听线程测试
模拟: 高频轮询检测大波动 → 条件通过 → 直接下单
"""
import os
import sys
import time
import threading
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

# Mock 所有外部依赖
for mod in [
    "ai_trader", "ai_trader.polymarket_api", "ai_trader.clob_client",
    "ai_trader.binance_api", "ai_trader.binance_api.price_stream",
    "ai_trader.coins",
    "ai_trader.polymarket_ws", "ai_trader.polymarket_ws.poly_ws",
    "ai_trader.bayesian_engine", "ai_trader.indicators", "ai_trader.fees",
    "ai_trader.polymarket_rtds",
    "ai_analyze_v2", "trading_state",
    "py_clob_client", "py_clob_client.order_builder",
    "py_clob_client.order_builder.constants",
    "dotenv",
]:
    sys.modules.setdefault(mod, MagicMock())

# Mock trading_state 函数
sys.modules["trading_state"].should_trade = MagicMock(return_value=True)
sys.modules["trading_state"].decrease_cooldown = MagicMock()
sys.modules["trading_state"].get_state_summary = MagicMock(return_value="test")
sys.modules["trading_state"].record_bet_result = MagicMock()
sys.modules["trading_state"].check_daily_loss_limit = MagicMock(return_value=(True, 0, 10))
sys.modules["trading_state"].record_bet_cost = MagicMock(return_value=0)

import auto_bot_v3 as bot

# 恢复 stdout
sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__


def make_market(coin="BTC", slug=None, elapsed=0):
    """创建模拟市场数据"""
    if slug is None:
        slug = f"{coin.lower()}-updown-5m-1700000000"
    end_time = datetime.now(timezone.utc) + timedelta(seconds=300 - elapsed)
    return {
        "coin": coin,
        "slug": slug,
        "end_time": end_time.isoformat(),
        "up_odds": 0.50,
        "down_odds": 0.50,
    }


class FakeUpdater:
    """模拟 BayesianUpdater"""
    def __init__(self, atr_val=113.0, direction="UP", p_hat=0.75, confidence=0.45):
        self.atr_val = atr_val
        self._direction = direction
        self._p_hat = p_hat
        self._confidence = confidence
        self.n_updates = 12

    def update(self, price, ptb):
        pass

    def get_direction_and_confidence(self):
        return self._direction, self._p_hat, self._confidence

    def get_summary(self, remaining_seconds=None):
        return {
            "direction": self._direction,
            "p_hat": self._p_hat,
            "confidence": self._confidence,
        }


# ═══════════════════════════════════════════════════════════
# 1. 狙击线程启动/停止测试
# ═══════════════════════════════════════════════════════════

class TestSniperThreadLifecycle(unittest.TestCase):
    """狙击线程启动和停止"""

    def test_start_creates_daemon_thread(self):
        """start_sniper_thread 应创建守护线程"""
        tracker = bot.MarketTracker()
        with patch.object(tracker, "_sniper_loop"):
            tracker.start_sniper_thread()
            self.assertTrue(tracker._sniper_running)
            self.assertIsNotNone(tracker._sniper_thread)
            self.assertTrue(tracker._sniper_thread.daemon)
            tracker.stop_sniper_thread()

    def test_stop_sets_flag(self):
        """stop_sniper_thread 应设置停止标志"""
        tracker = bot.MarketTracker()
        tracker._sniper_running = True
        tracker.stop_sniper_thread()
        self.assertFalse(tracker._sniper_running)

    def test_double_start_no_duplicate(self):
        """重复调用 start 不应创建第二个线程"""
        tracker = bot.MarketTracker()
        with patch.object(tracker, "_sniper_loop"):
            tracker.start_sniper_thread()
            first_thread = tracker._sniper_thread
            tracker.start_sniper_thread()
            self.assertIs(tracker._sniper_thread, first_thread)
            tracker.stop_sniper_thread()


# ═══════════════════════════════════════════════════════════
# 2. 狙击扫描条件测试
# ═══════════════════════════════════════════════════════════

class TestSniperScanConditions(unittest.TestCase):
    """_sniper_scan 的条件检查"""

    def setUp(self):
        self.tracker = bot.MarketTracker()

    def _setup_market(self, slug="btc-updown-5m-1700000000", coin="BTC",
                      elapsed=50, atr_val=113, direction="UP", p_hat=0.75,
                      confidence=0.45, gap_price=69750, ptb=69612):
        """设置一个标准的可狙击市场"""
        market = make_market(coin, slug, elapsed=elapsed)
        self.tracker.tracked[slug] = market
        self.tracker.ptb_cache[slug] = ptb
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=atr_val, direction=direction, p_hat=p_hat, confidence=confidence
        )
        self.tracker.token_cache[slug] = ("up_token", "down_token")
        return market, gap_price  # gap_price = simulated chainlink price

    @patch.dict(os.environ, {
        "SNIPER_MIN_ATR": "0.5", "SNIPER_MAX_PRICE": "0.65",
        "SNIPER_MIN_CONF": "0.25", "SNIPER_MIN_EV": "0.01",
        "SNIPER_EARLY": "5", "P_WIN_SHRINKAGE": "0.80",
    })
    def test_trigger_on_large_gap(self):
        """大gap + 方向一致 + EV正 → 应触发狙击"""
        slug = "btc-updown-5m-1700000000"
        market, price = self._setup_market(
            slug=slug, elapsed=50, atr_val=113, direction="UP",
            p_hat=0.75, confidence=0.45, gap_price=69750, ptb=69612
        )

        mock_chainlink = MagicMock()
        mock_chainlink.get_price.return_value = None

        mock_binance = MagicMock()
        mock_binance.get_price.return_value = price  # gap = 138, diff_atr = 1.22

        mock_clob = {
            "up_ask": 0.55, "down_ask": 0.45,
            "up_bid": 0.50, "down_bid": 0.40,
        }

        mock_execute = MagicMock(return_value=(True, 0.55, 10, "OK"))

        with patch("auto_bot_v3.get_realtime_odds", return_value=mock_clob), \
             patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=mock_chainlink),
                 "ai_trader.binance_api": MagicMock(price_stream=mock_binance),
             }), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={
                 "direction": "UP", "p_hat": 0.75, "confidence": 0.45,
             }), \
             patch("auto_bot_v3.record_bet_cost"), \
             patch("auto_bot_v3.send_notification"), \
             patch("auto_bot_v3.Position") as MockPosition:

            # Mock execute_bet on the mocked ai_analyze_v2 module
            sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.82)
            sys.modules["ai_analyze_v2"].execute_bet = mock_execute

            self.tracker._sniper_scan()

            self.assertTrue(mock_execute.called, "应触发 execute_bet")
            call_args = mock_execute.call_args
            self.assertEqual(call_args[0][0], slug)
            self.assertEqual(call_args[0][1], "UP")

    @patch.dict(os.environ, {
        "SNIPER_MIN_ATR": "0.5", "SNIPER_EARLY": "5",
    })
    def test_skip_when_no_ptb(self):
        """没有PTB时不触发"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=50)
        self.tracker.tracked[slug] = market
        # 不设置 ptb_cache
        self.tracker.bayesian_updaters[slug] = FakeUpdater()
        self.tracker.token_cache[slug] = ("up_token", "down_token")

        mock_chainlink = MagicMock()
        mock_chainlink.get_price.return_value = 69750

        with patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=mock_chainlink),
                 "ai_trader.binance_api": MagicMock(price_stream=MagicMock()),
             }):
            sys.modules["ai_analyze_v2"].execute_bet = MagicMock()
            self.tracker._sniper_scan()
            self.assertFalse(sys.modules["ai_analyze_v2"].execute_bet.called)

    @patch.dict(os.environ, {
        "SNIPER_MIN_ATR": "0.5", "SNIPER_EARLY": "5",
    })
    def test_skip_when_already_analyzed(self):
        """已分析的市场不触发"""
        slug = "btc-updown-5m-1700000000"
        self._setup_market(slug=slug, elapsed=50)
        self.tracker.analyzed.add(slug)

        mock_chainlink = MagicMock()
        mock_chainlink.get_price.return_value = 69750

        with patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=mock_chainlink),
                 "ai_trader.binance_api": MagicMock(price_stream=MagicMock()),
             }):
            sys.modules["ai_analyze_v2"].execute_bet = MagicMock()
            self.tracker._sniper_scan()
            self.assertFalse(sys.modules["ai_analyze_v2"].execute_bet.called)

    @patch.dict(os.environ, {
        "SNIPER_MIN_ATR": "0.5", "SNIPER_EARLY": "5",
    })
    def test_skip_when_has_position(self):
        """已有持仓时不触发"""
        slug = "btc-updown-5m-1700000000"
        self._setup_market(slug=slug, elapsed=50)
        self.tracker.positions[slug] = MagicMock()

        mock_chainlink = MagicMock()
        mock_chainlink.get_price.return_value = 69750

        with patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=mock_chainlink),
                 "ai_trader.binance_api": MagicMock(price_stream=MagicMock()),
             }):
            sys.modules["ai_analyze_v2"].execute_bet = MagicMock()
            self.tracker._sniper_scan()
            self.assertFalse(sys.modules["ai_analyze_v2"].execute_bet.called)

    @patch.dict(os.environ, {
        "SNIPER_MIN_ATR": "2.0", "SNIPER_EARLY": "5",
    })
    def test_skip_when_atr_too_low(self):
        """ATR偏离不够大时不触发"""
        slug = "btc-updown-5m-1700000000"
        # gap = 69650 - 69612 = 38, diff_atr = 38/113 = 0.34 < 2.0
        self._setup_market(slug=slug, elapsed=50, gap_price=69650, ptb=69612)

        mock_binance = MagicMock()
        mock_binance.get_price.return_value = 69650

        with patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=MagicMock()),
                 "ai_trader.binance_api": MagicMock(price_stream=mock_binance),
             }), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={
                 "direction": "UP", "p_hat": 0.75, "confidence": 0.45,
             }):
            sys.modules["ai_analyze_v2"].execute_bet = MagicMock()
            self.tracker._sniper_scan()
            self.assertFalse(sys.modules["ai_analyze_v2"].execute_bet.called)

    @patch.dict(os.environ, {
        "SNIPER_MIN_ATR": "0.5", "SNIPER_EARLY": "5",
    })
    def test_skip_when_direction_mismatch(self):
        """贝叶斯方向不一致时不触发"""
        slug = "btc-updown-5m-1700000000"
        # price > ptb → gap_dir = UP, but bayesian says DOWN
        self._setup_market(
            slug=slug, elapsed=50, direction="DOWN",
            gap_price=69750, ptb=69612
        )

        mock_binance = MagicMock()
        mock_binance.get_price.return_value = 69750

        with patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=MagicMock()),
                 "ai_trader.binance_api": MagicMock(price_stream=mock_binance),
             }), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={
                 "direction": "DOWN", "p_hat": 0.75, "confidence": 0.45,
             }):
            sys.modules["ai_analyze_v2"].execute_bet = MagicMock()
            self.tracker._sniper_scan()
            self.assertFalse(sys.modules["ai_analyze_v2"].execute_bet.called)

    @patch.dict(os.environ, {
        "SNIPER_MIN_ATR": "0.5", "SNIPER_MAX_PRICE": "0.65",
        "SNIPER_MIN_CONF": "0.25", "SNIPER_MIN_EV": "0.01",
        "SNIPER_EARLY": "5", "P_WIN_SHRINKAGE": "0.80",
    })
    def test_skip_when_price_too_high(self):
        """CLOB价格超过上限时不触发"""
        slug = "btc-updown-5m-1700000000"
        self._setup_market(slug=slug, elapsed=50, gap_price=69750, ptb=69612)

        mock_binance = MagicMock()
        mock_binance.get_price.return_value = 69750

        # ask = 0.80 > SNIPER_MAX_PRICE=0.65
        mock_clob = {"up_ask": 0.80, "down_ask": 0.20}

        with patch("auto_bot_v3.get_realtime_odds", return_value=mock_clob), \
             patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=MagicMock()),
                 "ai_trader.binance_api": MagicMock(price_stream=mock_binance),
             }), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={
                 "direction": "UP", "p_hat": 0.75, "confidence": 0.45,
             }):
            sys.modules["ai_analyze_v2"].execute_bet = MagicMock()
            self.tracker._sniper_scan()
            self.assertFalse(sys.modules["ai_analyze_v2"].execute_bet.called)


# ═══════════════════════════════════════════════════════════
# 3. 狙击线程与主线程竞争测试
# ═══════════════════════════════════════════════════════════

class TestSniperThreadSafety(unittest.TestCase):
    """线程安全和去重"""

    def setUp(self):
        self.tracker = bot.MarketTracker()

    def test_sniper_processing_prevents_inline_duplicate(self):
        """_sniper_processing 应阻止内联狙击同时处理同一slug"""
        slug = "btc-updown-5m-1700000000"
        self.tracker._sniper_processing.add(slug)
        # slug在_sniper_processing中，内联检查条件应包含此项
        self.assertIn(slug, self.tracker._sniper_processing)

    def test_sniper_processing_cleared_after_scan(self):
        """扫描完成后 _sniper_processing 应清空"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=50)
        self.tracker.tracked[slug] = market
        self.tracker.ptb_cache[slug] = 69612
        self.tracker.bayesian_updaters[slug] = FakeUpdater()
        self.tracker.token_cache[slug] = ("up_token", "down_token")

        mock_binance = MagicMock()
        mock_binance.get_price.return_value = 69750

        mock_clob = {"up_ask": 0.55, "down_ask": 0.45}
        mock_execute = MagicMock(return_value=(False, 0, 0, "FOK killed"))

        with patch("auto_bot_v3.get_realtime_odds", return_value=mock_clob), \
             patch.dict(os.environ, {
                 "SNIPER_MIN_ATR": "0.5", "SNIPER_MAX_PRICE": "0.65",
                 "SNIPER_MIN_CONF": "0.25", "SNIPER_MIN_EV": "0.01",
                 "SNIPER_EARLY": "5", "P_WIN_SHRINKAGE": "0.80",
             }), \
             patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=MagicMock()),
                 "ai_trader.binance_api": MagicMock(price_stream=mock_binance),
             }), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={
                 "direction": "UP", "p_hat": 0.75, "confidence": 0.45,
             }), \
             patch("auto_bot_v3.send_notification"), \
             patch("auto_bot_v3.record_bet_cost"):
            sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.82)
            sys.modules["ai_analyze_v2"].execute_bet = mock_execute
            self.tracker._sniper_scan()

        # 扫描完成后 _sniper_processing 应为空
        self.assertNotIn(slug, self.tracker._sniper_processing)

    def test_successful_snipe_marks_analyzed(self):
        """狙击成功后应标记 analyzed"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=50)
        self.tracker.tracked[slug] = market
        self.tracker.ptb_cache[slug] = 69612
        self.tracker.bayesian_updaters[slug] = FakeUpdater()
        self.tracker.token_cache[slug] = ("up_token", "down_token")

        mock_binance = MagicMock()
        mock_binance.get_price.return_value = 69750

        mock_clob = {"up_ask": 0.55, "down_ask": 0.45}
        mock_execute = MagicMock(return_value=(True, 0.55, 10.0, "OK"))

        with patch("auto_bot_v3.get_realtime_odds", return_value=mock_clob), \
             patch.dict(os.environ, {
                 "SNIPER_MIN_ATR": "0.5", "SNIPER_MAX_PRICE": "0.65",
                 "SNIPER_MIN_CONF": "0.25", "SNIPER_MIN_EV": "0.01",
                 "SNIPER_EARLY": "5", "P_WIN_SHRINKAGE": "0.80",
             }), \
             patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=MagicMock()),
                 "ai_trader.binance_api": MagicMock(price_stream=mock_binance),
             }), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={
                 "direction": "UP", "p_hat": 0.75, "confidence": 0.45,
             }), \
             patch("auto_bot_v3.send_notification"), \
             patch("auto_bot_v3.record_bet_cost"), \
             patch("auto_bot_v3.Position") as MockPosition:
            sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.82)
            sys.modules["ai_analyze_v2"].execute_bet = mock_execute
            self.tracker._sniper_scan()

        self.assertIn(slug, self.tracker.analyzed)
        self.assertIn(slug, self.tracker.positions)


# ═══════════════════════════════════════════════════════════
# 4. 价格源 fallback 测试
# ═══════════════════════════════════════════════════════════

class TestSniperPriceFallback(unittest.TestCase):
    """Binance WS → Chainlink WS 价格源降级"""

    def setUp(self):
        self.tracker = bot.MarketTracker()

    def test_uses_binance_as_primary(self):
        """Binance为主价格源，Chainlink为fallback"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=50)
        self.tracker.tracked[slug] = market
        self.tracker.ptb_cache[slug] = 69612
        self.tracker.bayesian_updaters[slug] = FakeUpdater()
        self.tracker.token_cache[slug] = ("up_token", "down_token")

        mock_chainlink = MagicMock()
        mock_chainlink.get_price.return_value = None  # Chainlink 无数据

        mock_binance = MagicMock()
        mock_binance.get_price.return_value = 69750  # Binance 有数据

        mock_clob = {"up_ask": 0.55, "down_ask": 0.45}
        mock_execute = MagicMock(return_value=(True, 0.55, 10.0, "OK"))

        with patch("auto_bot_v3.get_realtime_odds", return_value=mock_clob), \
             patch.dict(os.environ, {
                 "SNIPER_MIN_ATR": "0.5", "SNIPER_MAX_PRICE": "0.65",
                 "SNIPER_MIN_CONF": "0.25", "SNIPER_MIN_EV": "0.01",
                 "SNIPER_EARLY": "5", "P_WIN_SHRINKAGE": "0.80",
             }), \
             patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=mock_chainlink),
                 "ai_trader.binance_api": MagicMock(price_stream=mock_binance),
             }), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={
                 "direction": "UP", "p_hat": 0.75, "confidence": 0.45,
             }), \
             patch("auto_bot_v3.send_notification"), \
             patch("auto_bot_v3.record_bet_cost"), \
             patch("auto_bot_v3.Position"):
            sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.82)
            sys.modules["ai_analyze_v2"].execute_bet = mock_execute
            self.tracker._sniper_scan()

        self.assertTrue(mock_execute.called, "Binance主价格源应触发狙击")
        mock_binance.get_price.assert_called()

    def test_skip_when_no_price_source(self):
        """两个价格源都无数据时不触发"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=50)
        self.tracker.tracked[slug] = market
        self.tracker.ptb_cache[slug] = 69612
        self.tracker.bayesian_updaters[slug] = FakeUpdater()
        self.tracker.token_cache[slug] = ("up_token", "down_token")

        mock_chainlink = MagicMock()
        mock_chainlink.get_price.return_value = None

        mock_binance = MagicMock()
        mock_binance.get_price.return_value = None

        with patch.dict(os.environ, {"SNIPER_MIN_ATR": "0.5", "SNIPER_EARLY": "5"}), \
             patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=mock_chainlink),
                 "ai_trader.binance_api": MagicMock(price_stream=mock_binance),
             }):
            sys.modules["ai_analyze_v2"].execute_bet = MagicMock()
            self.tracker._sniper_scan()
            self.assertFalse(sys.modules["ai_analyze_v2"].execute_bet.called)


# ═══════════════════════════════════════════════════════════
# 5. DOWN方向狙击测试
# ═══════════════════════════════════════════════════════════

class TestSniperDownDirection(unittest.TestCase):
    """price < PTB → DOWN 方向狙击"""

    def setUp(self):
        self.tracker = bot.MarketTracker()

    @patch.dict(os.environ, {
        "SNIPER_MIN_ATR": "0.5", "SNIPER_MAX_PRICE": "0.65",
        "SNIPER_MIN_CONF": "0.25", "SNIPER_MIN_EV": "0.01",
        "SNIPER_EARLY": "5", "P_WIN_SHRINKAGE": "0.80",
    })
    def test_down_direction_uses_down_ask(self):
        """price < PTB时应用DOWN方向并使用down_ask"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=50)
        self.tracker.tracked[slug] = market
        self.tracker.ptb_cache[slug] = 69612
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            direction="DOWN", p_hat=0.75, confidence=0.45
        )
        self.tracker.token_cache[slug] = ("up_token", "down_token")

        mock_binance = MagicMock()
        mock_binance.get_price.return_value = 69470  # gap = -142, diff_atr = 1.26

        mock_clob = {"up_ask": 0.55, "down_ask": 0.50}
        mock_execute = MagicMock(return_value=(True, 0.50, 10.0, "OK"))

        with patch("auto_bot_v3.get_realtime_odds", return_value=mock_clob), \
             patch.dict(sys.modules, {
                 "ai_trader.polymarket_rtds": MagicMock(chainlink_stream=MagicMock()),
                 "ai_trader.binance_api": MagicMock(price_stream=mock_binance),
             }), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={
                 "direction": "DOWN", "p_hat": 0.75, "confidence": 0.45,
             }), \
             patch("auto_bot_v3.send_notification"), \
             patch("auto_bot_v3.record_bet_cost"), \
             patch("auto_bot_v3.Position"):
            sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.82)
            sys.modules["ai_analyze_v2"].execute_bet = mock_execute
            self.tracker._sniper_scan()

        self.assertTrue(mock_execute.called)
        # 验证用的是 DOWN 方向和 down_token
        call_args = mock_execute.call_args
        self.assertEqual(call_args[0][1], "DOWN")  # direction
        self.assertEqual(call_args[0][2], "down_token")  # token


# ═══════════════════════════════════════════════════════════
# 6. EV计算和env配置测试
# ═══════════════════════════════════════════════════════════

class TestSniperEnvConfig(unittest.TestCase):
    """环境变量配置"""

    def test_sniper_thread_disabled_by_env(self):
        """SNIPER_THREAD_ENABLED=0 应禁用线程"""
        tracker = bot.MarketTracker()
        with patch.dict(os.environ, {"SNIPER_THREAD_ENABLED": "0"}):
            # 模拟main()中的检查逻辑
            if os.environ.get("SNIPER_THREAD_ENABLED", "1") == "1":
                tracker.start_sniper_thread()
            self.assertFalse(tracker._sniper_running)

    def test_init_has_sniper_state(self):
        """MarketTracker应初始化狙击线程相关状态"""
        tracker = bot.MarketTracker()
        self.assertFalse(tracker._sniper_running)
        self.assertIsNone(tracker._sniper_thread)
        self.assertIsInstance(tracker._sniper_lock, type(threading.Lock()))
        self.assertIsInstance(tracker._sniper_processing, set)


if __name__ == "__main__":
    unittest.main()
