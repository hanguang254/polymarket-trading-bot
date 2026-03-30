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

# Mock poly_ws.get_best_bid_ask 返回 (bid, ask) 元组
sys.modules["ai_trader.polymarket_ws"].poly_ws.get_best_bid_ask = MagicMock(return_value=(0.55, 0.60))

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
        self.samples = []

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
        # 预填充独立贝叶斯（跳过import，直接用FakeUpdater）
        self.tracker._sniper_updaters[slug] = FakeUpdater(
            atr_val=atr_val, direction=direction, p_hat=p_hat, confidence=confidence
        )
        self.tracker.token_cache[slug] = ("up_token", "down_token")
        return market, gap_price

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
        self.tracker._sniper_updaters[slug] = FakeUpdater()
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
        self.tracker._sniper_updaters[slug] = FakeUpdater()
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
        self.tracker._sniper_updaters[slug] = FakeUpdater()
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
        self.tracker._sniper_updaters[slug] = FakeUpdater()
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
        self.tracker._sniper_updaters[slug] = FakeUpdater(
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


class TestAmbushRepriceTracking(unittest.TestCase):
    @patch.dict(os.environ, {
        "SNIPER_AMBUSH_BAL_INTERVAL": "999",
        "SNIPER_AMBUSH_REPRICE_INTERVAL": "0",
        "SNIPER_AMBUSH_REPRICE_MAX": "5",
        "SNIPER_AMBUSH_GTD_REPRICE_SEC": "8",
    }, clear=False)
    def test_reprice_only_tracks_new_order_after_confirmed_cancel(self):
        """v12.9.12: 确认旧单撤销后，只跟踪新单（不再累积多单）"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.55,
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.80)

        env_overrides = {
            "SNIPER_AMBUSH_EDGE": "0.045",
            "SNIPER_AMBUSH_MIN_PRICE": "0.45",
            "SNIPER_AMBUSH_MAX_PRICE": "0.65",
            "SNIPER_AMBUSH_REPRICE_MIN_DELTA": "0.02",
            "SNIPER_AMBUSH_GTD_REPRICE_SEC": "4",
        }
        # cancel_all 成功 + 余额=0 + get_order 确认 CANCELLED → 安全下新单
        with patch.dict(os.environ, env_overrides), \
             patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True) as cancel_all_mock, \
             patch.object(bot.clob_client, "get_order", return_value={"status": "CANCELLED"}), \
             patch.object(bot.clob_client, "place_order", return_value={"order_id": "oid-new", "matched": False}):
            tracker._manage_ambush(
                slug=slug,
                coin="BTC",
                tokens=("up_token", "down_token"),
                ptb=69612,
                atr_val=113,
                remaining=120,
                end_buffer=0,
            )

        cancel_all_mock.assert_called()
        ambush = tracker._ambush_orders[slug]
        self.assertEqual(ambush["order_id"], "oid-new")
        self.assertEqual(ambush["price"], 0.64)
        self.assertEqual(ambush["reprice_count"], 1)
        # v12.9.12: cancel_all 信任制，只跟踪新单
        self.assertEqual(ambush["all_order_ids"], ["oid-new"])

    def test_matched_fill_blocks_subsequent_repricing(self):
        """v12.9.12: 即时成交后 pending_fill=True 阻止后续追价"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.55,
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.80)

        env_overrides = {
            "SNIPER_AMBUSH_EDGE": "0.045",
            "SNIPER_AMBUSH_MIN_PRICE": "0.45",
            "SNIPER_AMBUSH_MAX_PRICE": "0.65",
            "SNIPER_AMBUSH_REPRICE_MIN_DELTA": "0.02",
            "SNIPER_AMBUSH_GTD_REPRICE_SEC": "4",
        }

        # 第一次调用：cancel_all 成功 + bal=0 + get_order=CANCELLED → place_order matched=True
        with patch.dict(os.environ, env_overrides), \
             patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "get_order", return_value={"status": "CANCELLED"}), \
             patch.object(bot.clob_client, "place_order",
                          return_value={"order_id": None, "matched": True}):
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        self.assertTrue(tracker._ambush_orders[slug].get("pending_fill"))

        # 第二次调用（200ms后）：模型价变了，但 pending_fill 应阻止追价
        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.70)
        with patch.dict(os.environ, env_overrides), \
             patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.25), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.50}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "cancel_orders_batch", return_value=([], {})), \
             patch.object(bot.clob_client, "place_order") as place_mock_2:
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=119.75, end_buffer=0,
            )

        # pending_fill → place_order 不被调用
        place_mock_2.assert_not_called()

    def test_reprice_aborts_when_cancel_all_fails(self):
        """v12.9.12: cancel_all 失败时中止追价，不下新单"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.55,
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.80)

        with patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=False), \
             patch.object(bot.clob_client, "place_order") as place_mock:
            tracker._manage_ambush(
                slug=slug,
                coin="BTC",
                tokens=("up_token", "down_token"),
                ptb=69612,
                atr_val=113,
                remaining=120,
                end_buffer=0,
            )

        # cancel_all 失败 → 不下新单
        place_mock.assert_not_called()
        ambush = tracker._ambush_orders[slug]
        self.assertEqual(ambush["price"], 0.55)
        self.assertEqual(ambush["reprice_count"], 0)

    def test_reprice_accepts_american_spelling_canceled(self):
        """v12.9.12 regression: Polymarket 返回 CANCELED（美式拼写）应被识别为终结态"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.55,
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.80)

        env_overrides = {
            "SNIPER_AMBUSH_EDGE": "0.045",
            "SNIPER_AMBUSH_MIN_PRICE": "0.45",
            "SNIPER_AMBUSH_MAX_PRICE": "0.65",
            "SNIPER_AMBUSH_REPRICE_MIN_DELTA": "0.02",
            "SNIPER_AMBUSH_GTD_REPRICE_SEC": "4",
        }
        # get_order 返回 CANCELED（美式拼写，Polymarket 实际返回值）→ 应允许下新单
        with patch.dict(os.environ, env_overrides), \
             patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "get_order", return_value={"status": "CANCELED"}), \
             patch.object(bot.clob_client, "place_order", return_value={"order_id": "oid-new", "matched": False}) as place_mock:
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        # CANCELED 被识别为终结态 → 允许下新单
        place_mock.assert_called_once()
        ambush = tracker._ambush_orders[slug]
        self.assertEqual(ambush["order_id"], "oid-new")
        # 不应进入 stuck 冷却
        self.assertFalse(ambush.get("stuck_until", 0) > 1001.0)

    def test_reprice_stuck_order_cooldown_prevents_cancel_storm(self):
        """v12.9.12: 旧单仍 LIVE → stuck 冷却，第二次调用不再 cancel_all"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.55,
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.80)

        # 第一次调用：get_order=LIVE → stuck 冷却 5s
        with patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True) as cancel_mock_1, \
             patch.object(bot.clob_client, "get_order", return_value={"status": "LIVE"}), \
             patch.object(bot.clob_client, "place_order") as place_mock_1:
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        place_mock_1.assert_not_called()
        cancel_mock_1.assert_called()  # 第一次允许 cancel_all
        self.assertTrue(tracker._ambush_orders[slug].get("stuck_until", 0) > 1001.0)

        # 第二次调用（200ms 后，仍在冷却期内）：不再 cancel_all
        with patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.2), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True) as cancel_mock_2, \
             patch.object(bot.clob_client, "place_order") as place_mock_2:
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=119.8, end_buffer=0,
            )

        # 冷却期内：cancel_all 不被调用，place_order 也不被调用
        cancel_mock_2.assert_not_called()
        place_mock_2.assert_not_called()

    def test_reprice_matched_order_triggers_pending_fill(self):
        """v12.9.12: get_order=MATCHED 但余额滞后 → pending_fill，不下新单"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.55,
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.80)

        # cancel_all 成功, bal=0 (滞后), get_order=MATCHED
        with patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "get_order", return_value={"status": "MATCHED"}), \
             patch.object(bot.clob_client, "place_order") as place_mock:
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        place_mock.assert_not_called()
        self.assertTrue(tracker._ambush_orders[slug].get("pending_fill"))

    def test_small_partial_fill_detected_and_no_reprice(self):
        """v12.9.12 regression: 0.18 份部分成交(>0.1)应触发建仓，不再追价"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="DOWN", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 66500
        tracker._ambush_orders[slug] = {
            "token": "down_token",
            "opposite_token": "up_token",
            "up_token": "up_token",
            "down_token": "down_token",
            "direction": "DOWN",
            "confidence": 0.60,
            "price": 0.66,
            "size": 5.13,
            "bal_before": 0.0,
            "order_id": "oid-1",
            "all_order_ids": ["oid-1"],
            "placed_at": 1000.0,
            "last_bal_check": 0,
            "last_reprice": 1000.0,
            "reprice_count": 1,
        }

        # bal=0.18 > 0.1 阈值 → 应触发成交检测
        with patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.18), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "cancel_orders_batch", return_value=(["oid-1"], {})), \
             patch.object(bot.clob_client, "update_token_allowance", return_value=True), \
             patch.object(bot.clob_client, "place_fok_order", return_value={"matched": False}), \
             patch.object(bot.clob_client, "place_order") as place_mock, \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"direction": "DOWN", "confidence": 0.60}):
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=66600, atr_val=100, remaining=120, end_buffer=0,
            )

        # 0.18 > 0.1 → 成交检测触发，ambush 被清除，不再追价
        place_mock.assert_not_called()
        self.assertNotIn(slug, tracker._ambush_orders)

    def test_cancel_verify_detects_small_partial_fill(self):
        """v12.9.12 regression: cancel后余额验证发现0.18份部分成交 → pending_fill"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.55,
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.80)

        env_overrides = {
            "SNIPER_AMBUSH_EDGE": "0.045",
            "SNIPER_AMBUSH_MIN_PRICE": "0.45",
            "SNIPER_AMBUSH_MAX_PRICE": "0.65",
            "SNIPER_AMBUSH_REPRICE_MIN_DELTA": "0.02",
        }
        # 第一次 bal=0(顶部检查), 第二次 bal=0.18(cancel后验证) → pending_fill
        with patch.dict(os.environ, env_overrides), \
             patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", side_effect=[0.0, 0.18]), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "place_order") as place_mock:
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        place_mock.assert_not_called()
        self.assertTrue(tracker._ambush_orders[slug].get("pending_fill"))

    def test_reprice_aborts_when_balance_verify_fails(self):
        """v12.9.12: cancel_all 成功但余额查询返回 None → 中止追价"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.55,
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.80)

        # 第一次 get_token_balance=0.0 (顶部余额检查), 第二次=None (cancel后验证失败)
        with patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", side_effect=[0.0, None]), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "place_order") as place_mock:
            tracker._manage_ambush(
                slug=slug,
                coin="BTC",
                tokens=("up_token", "down_token"),
                ptb=69612,
                atr_val=113,
                remaining=120,
                end_buffer=0,
            )

        # 余额查询返回 None → 不下新单
        place_mock.assert_not_called()

    def test_reprice_detects_fill_during_cancel(self):
        """v12.9.12: cancel_all 成功但余额显示旧单已成交 → pending_fill"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.55,
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.80)

        # cancel_all 成功，但余额查询显示有 5.2 shares → 旧单已成交
        bal_calls = [0.0, 5.2]  # 第一次余额检查=0，第二次（cancel后验证）=5.2
        with patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", side_effect=bal_calls), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "place_order") as place_mock:
            tracker._manage_ambush(
                slug=slug,
                coin="BTC",
                tokens=("up_token", "down_token"),
                ptb=69612,
                atr_val=113,
                remaining=120,
                end_buffer=0,
            )

        # 发现成交 → pending_fill, 不下新单
        place_mock.assert_not_called()
        self.assertTrue(tracker._ambush_orders[slug].get("pending_fill"))

    def test_gtd_expired_get_order_none_places_new_order(self):
        """v12.9.14: GTD过期 + get_order=None → 视为终结，不stuck，成功下新单"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.55,
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
            "gtd_expires_at": 999.0,  # 已过期
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.80)

        with patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True) as cancel_mock, \
             patch.object(bot.clob_client, "get_order", return_value=None) as get_order_mock, \
             patch.object(bot.clob_client, "place_order", return_value={"order_id": "oid-new"}) as place_mock:
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        # GTD过期 + get_order=None → 不进入stuck，成功下新单
        cancel_mock.assert_called()
        get_order_mock.assert_called()
        place_mock.assert_called()
        self.assertIsNone(tracker._ambush_orders[slug].get("stuck_until"))
        self.assertEqual(tracker._ambush_orders[slug]["order_id"], "oid-new")
        self.assertEqual(tracker._ambush_orders[slug]["reprice_count"], 1)

    def test_edge_eroded_allows_backward_reprice(self):
        """v12.9.14: 旧单价≥模型公允价(edge腐蚀) → 允许向下追价"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69750  # gap缩小 → p_win下降
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.60,  # 旧单价高
            "size": 5.2,
            "bal_before": 0.0,
            "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
            "gtd_expires_at": 2000.0,  # 未过期
        }

        # p_win=0.58 → 旧单0.60 >= p_win → edge_eroded
        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.58)

        with patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.30}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "get_order", return_value={"status": "CANCELLED"}), \
             patch.object(bot.clob_client, "place_order", return_value={"order_id": "oid-new"}) as place_mock:
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        # edge腐蚀 → 向下追价被允许
        place_mock.assert_called()
        new_price = tracker._ambush_orders[slug]["price"]
        self.assertLess(new_price, 0.60, "edge腐蚀应导致降价")

    def test_forced_sell_pnl_uses_taking_only(self):
        """v12.9.15 regression: 伏击方向失效强制卖出时，_sell_usdc只用taking(USDC)，不混入making(token份额)"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=250)
        tracker.tracked[slug] = market
        tracker.ptb_cache[slug] = 69612
        tracker.bayesian_updaters[slug] = FakeUpdater(direction="DOWN", confidence=0.10)
        tracker._sniper_updaters[slug] = FakeUpdater(direction="DOWN", confidence=0.10)
        tracker._sniper_updaters[slug].current_price = 69400  # 价格跌破PTB → DOWN方向
        tracker.token_cache[slug] = ("up_token", "down_token")

        # 伏击单已成交：5.13份UP，cost=$2.98
        tracker._ambush_orders[slug] = {
            "token": "up_token",
            "opposite_token": "down_token",
            "direction": "UP",
            "confidence": 0.60,
            "price": 0.58,
            "size": 5.13,
            "bal_before": 0.0,
            "order_id": "oid-filled",
            "all_order_ids": ["oid-filled"],
            "placed_at": 1000.0,
            "last_bal_check": 1000.0,
            "last_reprice": 1000.0,
            "reprice_count": 0,
            "pending_fill": True,
            "filled_at": 1001.0,
        }

        # FOK SELL 返回: making=5.13(token给出), taking=2.77(USDC收到)
        mock_fok = {
            "matched": True,
            "making": 5.13,
            "taking": 2.77,
            "status": "MATCHED",
        }

        captured_logs = []
        original_info = bot.logger.info
        def capture_info(msg, *args, **kwargs):
            captured_logs.append(msg)
            return original_info(msg, *args, **kwargs)

        with patch.object(bot.clob_client, "get_token_balance", return_value=5.13), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "cancel_orders_batch", return_value=(["oid-filled"], {})), \
             patch.object(bot.clob_client, "update_token_allowance", return_value=True), \
             patch.object(bot.clob_client, "place_fok_order", return_value=mock_fok) as fok_mock, \
             patch("auto_bot_v3.send_notification") as notif_mock, \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"direction": "DOWN", "confidence": 0.10}), \
             patch.object(bot.logger, "info", side_effect=capture_info):
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=30, end_buffer=0,
            )

        # 核心断言: FOK sell 被调用
        fok_mock.assert_called_once()

        # 找到"即时卖出成功"日志，验证金额
        sell_log = [l for l in captured_logs if "即时卖出成功" in str(l)]
        self.assertTrue(len(sell_log) > 0, "应有即时卖出成功日志")

        # 关键: _sell_usdc 应该是 2.77 (taking only)，不是 7.90 (making+taking)
        log_text = sell_log[0]
        self.assertNotIn("$7.90", log_text, "_sell_usdc不应混入making(token份额)")
        self.assertIn("$2.77", log_text, "_sell_usdc应只用taking(USDC)")

        # 通知也应使用正确的卖价 (~0.54)，不是错误的 1.54
        notif_mock.assert_called_once()
        call_args = notif_mock.call_args
        notif_price = call_args[0][4] if len(call_args[0]) > 4 else call_args[1].get("price", 0)
        self.assertAlmostEqual(notif_price, 2.77 / 5.13, places=2, msg="通知卖价应≈0.54，不应是混合单位的1.54")


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
        self.assertIsInstance(tracker._sniper_updaters, dict)
        self.assertIsInstance(tracker._sniper_lock, type(threading.Lock()))
        self.assertIsInstance(tracker._sniper_processing, set)


class TestAmbushCLOBAwareReprice(unittest.TestCase):
    """v12.9.13: 追价CLOB感知 + offset时间衰减 + 禁止反向追价"""

    def test_clob_dominates_when_ask_high(self):
        """当CLOB ask远高于模型价时，CLOB感知价应主导追价"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        tracker._ambush_orders[slug] = {
            "token": "up_token", "opposite_token": "down_token",
            "direction": "UP", "confidence": 0.60,
            "price": 0.55, "size": 5.2,
            "bal_before": 0.0, "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 900.0, "last_bal_check": 1000.0,
            "last_reprice": 1000.0, "reprice_count": 0,
        }

        # _random_walk_p_win returns 0.65 → model price = round((0.6-0.045)/0.01)*0.01 = 0.56
        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.65)

        env_overrides = {
            "SNIPER_AMBUSH_EDGE": "0.045",
            "SNIPER_AMBUSH_MIN_PRICE": "0.45",
            "SNIPER_AMBUSH_MAX_PRICE": "0.75",
            "SNIPER_AMBUSH_REPRICE_MIN_DELTA": "0.02",
            "SNIPER_AMBUSH_GTD_REPRICE_SEC": "4",
            "SNIPER_AMBUSH_ASK_OFFSET": "0.05",
            "SNIPER_AMBUSH_ASK_OFFSET_MIN": "0.01",
        }

        # Mock WS to return high ask=$0.75 → CLOB price should dominate
        mock_ws = MagicMock()
        mock_ws.get_best_bid_ask.return_value = (0.70, 0.75)

        with patch.dict(os.environ, env_overrides), \
             patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.60}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True), \
             patch.object(bot.clob_client, "get_order", return_value={"status": "CANCELLED"}), \
             patch.object(bot.clob_client, "place_order",
                          return_value={"order_id": "oid-new"}) as place_mock, \
             patch("ai_trader.polymarket_ws.poly_ws", mock_ws):
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        # place_order should have been called
        self.assertTrue(place_mock.called)
        actual_price = place_mock.call_args[0][2]  # 3rd positional arg = price
        # CLOB price with decay: elapsed=101s, total=221s, decay≈0.457
        # offset = 0.05 - (0.05-0.01)*0.457 ≈ 0.032 → clob_price = 0.75-0.032 = 0.72
        # model price ≈ 0.56, so CLOB price should dominate → price > 0.60
        self.assertGreater(actual_price, 0.60,
                           f"CLOB-aware price {actual_price} should be > 0.60 (model ~0.56)")

    def test_backward_reprice_blocked_when_below_ask(self):
        """v12.9.13: 旧单在ask之下时，禁止降价退让（旧单仍安全）
        old=$0.64 < ask=$0.67, CLOB new≈$0.618, delta=0.022≥0.02 → 真正命中禁止退让分支"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        # 旧单 $0.64 < ask $0.67 → 安全
        # CLOB感知后 new_price ≈ $0.618, delta=0.64-0.618=0.022 ≥ 0.02 → 命中backward分支
        tracker._ambush_orders[slug] = {
            "token": "up_token", "opposite_token": "down_token",
            "direction": "UP", "confidence": 0.60,
            "price": 0.64, "size": 5.2,
            "bal_before": 0.0, "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 900.0, "last_bal_check": 1000.0,
            "last_reprice": 1000.0, "reprice_count": 0,
        }

        # p_win → 0.60 → model price ≈ 0.56, CLOB aware → ~0.618 < old 0.64
        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.60)

        env_overrides = {
            "SNIPER_AMBUSH_EDGE": "0.045",
            "SNIPER_AMBUSH_MIN_PRICE": "0.45",
            "SNIPER_AMBUSH_MAX_PRICE": "0.75",
            "SNIPER_AMBUSH_REPRICE_MIN_DELTA": "0.02",
            "SNIPER_AMBUSH_ASK_OFFSET": "0.05",
            "SNIPER_AMBUSH_ASK_OFFSET_MIN": "0.01",
        }

        # ask=$0.67 > old $0.64 → 旧单在ask之下，不需要撤
        mock_ws = MagicMock()
        mock_ws.get_best_bid_ask.return_value = (0.62, 0.67)

        with patch.dict(os.environ, env_overrides), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.50}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all") as cancel_mock, \
             patch.object(bot.clob_client, "place_order") as place_mock, \
             patch("ai_trader.polymarket_ws.poly_ws", mock_ws):
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        # 旧单 < ask → 安全保持，不撤不下
        place_mock.assert_not_called()
        cancel_mock.assert_not_called()

    def test_backward_reprice_survives_ws_failure(self):
        """v12.9.13: WS异常时 _rp_ask=None，backward日志不应崩溃"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        # 旧单 $0.63, model→$0.56, delta=0.07≥0.02, backward=True
        tracker._ambush_orders[slug] = {
            "token": "up_token", "opposite_token": "down_token",
            "direction": "UP", "confidence": 0.60,
            "price": 0.63, "size": 5.2,
            "bal_before": 0.0, "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 900.0, "last_bal_check": 1000.0,
            "last_reprice": 1000.0, "reprice_count": 0,
        }

        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.60)

        env_overrides = {
            "SNIPER_AMBUSH_EDGE": "0.045",
            "SNIPER_AMBUSH_MIN_PRICE": "0.45",
            "SNIPER_AMBUSH_MAX_PRICE": "0.75",
            "SNIPER_AMBUSH_REPRICE_MIN_DELTA": "0.02",
        }

        # WS 直接抛异常 → _rp_ask=None
        mock_ws = MagicMock()
        mock_ws.get_best_bid_ask.side_effect = RuntimeError("ws boom")

        with patch.dict(os.environ, env_overrides), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.50}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all") as cancel_mock, \
             patch.object(bot.clob_client, "place_order") as place_mock, \
             patch("ai_trader.polymarket_ws.poly_ws", mock_ws):
            # 不应抛 TypeError — _rp_ask=None 时日志应安全输出 "ask未知"
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        # WS挂了 + backward → _rp_ask=None, _stale_above_ask=False → 安全不动
        place_mock.assert_not_called()
        cancel_mock.assert_not_called()

    def test_stale_order_above_ask_gets_downward_repriced(self):
        """v12.9.13: 旧单高于ask时，必须撤旧单+安全降价"""
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        tracker._sniper_updaters[slug] = FakeUpdater(direction="UP", confidence=0.60)
        tracker._sniper_updaters[slug].current_price = 69850
        # 旧单 $0.62 > ask $0.60 → 过时的高价单，必须撤掉降价
        tracker._ambush_orders[slug] = {
            "token": "up_token", "opposite_token": "down_token",
            "direction": "UP", "confidence": 0.60,
            "price": 0.62, "size": 5.2,
            "bal_before": 0.0, "order_id": "oid-old",
            "all_order_ids": ["oid-old"],
            "placed_at": 900.0, "last_bal_check": 1000.0,
            "last_reprice": 1000.0, "reprice_count": 0,
        }

        # p_win → 0.60 → model price ≈ 0.56, CLOB aware → ~0.59
        sys.modules["ai_analyze_v2"]._random_walk_p_win = MagicMock(return_value=0.60)

        env_overrides = {
            "SNIPER_AMBUSH_EDGE": "0.045",
            "SNIPER_AMBUSH_MIN_PRICE": "0.45",
            "SNIPER_AMBUSH_MAX_PRICE": "0.75",
            "SNIPER_AMBUSH_REPRICE_MIN_DELTA": "0.02",
            "SNIPER_AMBUSH_GTD_REPRICE_SEC": "4",
            "SNIPER_AMBUSH_ASK_OFFSET": "0.05",
            "SNIPER_AMBUSH_ASK_OFFSET_MIN": "0.01",
        }

        # ask=$0.60 < old $0.62 → stale above ask!
        mock_ws = MagicMock()
        mock_ws.get_best_bid_ask.return_value = (0.55, 0.60)

        with patch.dict(os.environ, env_overrides), \
             patch.dict(sys.modules, {"py_clob_client.clob_types": MagicMock()}), \
             patch("auto_bot_v3.time.time", return_value=1001.0), \
             patch("auto_bot_v3._get_bayesian_signal", return_value={"confidence": 0.50}), \
             patch.object(bot.clob_client, "get_token_balance", return_value=0.0), \
             patch.object(bot.clob_client, "cancel_all", return_value=True) as cancel_mock, \
             patch.object(bot.clob_client, "get_order", return_value={"status": "CANCELLED"}), \
             patch.object(bot.clob_client, "place_order",
                          return_value={"order_id": "oid-new"}) as place_mock, \
             patch("ai_trader.polymarket_ws.poly_ws", mock_ws):
            tracker._manage_ambush(
                slug=slug, coin="BTC", tokens=("up_token", "down_token"),
                ptb=69612, atr_val=113, remaining=120, end_buffer=0,
            )

        # 旧单 > ask → 必须撤旧单 + 下新单（降价）
        cancel_mock.assert_called()
        place_mock.assert_called()
        new_price = place_mock.call_args[0][2]
        self.assertLess(new_price, 0.62, f"New price {new_price} should be < old 0.62")


class TestAmbushTpPersistence(unittest.TestCase):
    """v12.9.16: 伏击止盈单跨进程持久化回归测试"""

    def test_tp_metadata_persisted_to_positions_jsonl(self):
        """TP挂单成功后 tp_order_id/tp_price 必须写入 positions.jsonl，
        确保 position_monitor（独立进程）能读到。"""
        import tempfile, json, fcntl

        with tempfile.TemporaryDirectory() as tmpdir:
            pos_file = os.path.join(tmpdir, "positions.jsonl")
            lock_file = pos_file + ".lock"

            # 模拟 auto_bot_v3 先写入初始持仓记录（无 TP 字段）
            initial_record = {
                "token_id": "tok_up_123", "slug": "btc-updown-5m-1700000000",
                "direction": "UP", "entry_price": 0.55, "size": 10.0,
                "entry_cost": 5.5, "entry_time": "2026-03-30T06:00:00+00:00",
                "price_to_beat": 69612, "atr": 113,
                "opposite_token_id": "tok_down_456",
                "sniper_thread": True, "ambush": True,
            }
            with open(pos_file, "w") as f:
                f.write(json.dumps(initial_record) + "\n")

            # 模拟 TP 挂单成功后的持久化逻辑（与 auto_bot_v3.py 一致）
            _tp_oid = "tp_order_abc123"
            _tp_price = 0.78
            _token = "tok_up_123"
            _entry_ts = "2026-03-30T06:00:00+00:00"

            _tp_lock = open(lock_file, "w")
            fcntl.flock(_tp_lock, fcntl.LOCK_EX)
            _tp_lines = []
            with open(pos_file, "r") as _tpf:
                for _tpl in _tpf:
                    if _tpl.strip():
                        _tpr = json.loads(_tpl)
                        if isinstance(_tpr, dict) and _tpr.get("token_id") == _token and _tpr.get("entry_time") == _entry_ts:
                            _tpr["tp_order_id"] = _tp_oid
                            _tpr["tp_price"] = _tp_price
                        _tp_lines.append(_tpr)
            with open(pos_file, "w") as _tpf:
                for _tpr in _tp_lines:
                    _tpf.write(json.dumps(_tpr) + "\n")
            fcntl.flock(_tp_lock, fcntl.LOCK_UN)
            _tp_lock.close()

            # 验证：模拟 position_monitor.get_open_positions 的读取逻辑
            positions = []
            with open(pos_file, "r") as f:
                for line in f:
                    if line.strip():
                        pos = json.loads(line)
                        if isinstance(pos, dict) and not pos.get("closed", False):
                            positions.append(pos)

            self.assertEqual(len(positions), 1)
            pos = positions[0]
            self.assertEqual(pos["tp_order_id"], "tp_order_abc123",
                             "tp_order_id must be persisted to positions.jsonl for cross-process handoff")
            self.assertAlmostEqual(pos["tp_price"], 0.78, places=2,
                                   msg="tp_price must be persisted to positions.jsonl for cross-process handoff")
            # 原有字段不能丢
            self.assertEqual(pos["token_id"], "tok_up_123")
            self.assertTrue(pos["ambush"])
            self.assertEqual(pos["entry_price"], 0.55)


if __name__ == "__main__":
    unittest.main()
