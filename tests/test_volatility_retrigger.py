#!/usr/bin/env python3
"""
波动重触发功能测试
模拟: 市场跳过 → 价格大幅移动 → 重触发分析
"""
import os
import sys
import time
import unittest
from collections import deque
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, mock_open, patch

# Mock 所有外部依赖
for mod in [
    "ai_trader", "ai_trader.polymarket_api", "ai_trader.clob_client",
    "ai_trader.binance_api", "ai_trader.binance_api.price_stream",
    "ai_trader.polymarket_ws", "ai_trader.polymarket_ws.poly_ws",
    "ai_trader.bayesian_engine", "ai_trader.indicators",
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
    def __init__(self, atr_val=45.0, direction="UP", p_hat=0.52, confidence=0.07):
        self.atr_val = atr_val
        self._direction = direction
        self._p_hat = p_hat
        self._confidence = confidence
        self.n_updates = 12

    def update(self, price, ptb):
        pass

    def get_direction_and_confidence(self):
        return self._direction, self._p_hat, self._confidence

    def get_summary(self):
        return {
            "direction": self._direction,
            "p_hat": self._p_hat,
            "confidence": self._confidence,
        }


# ═══════════════════════════════════════════════════════════
# 1. skipped_markets 状态管理测试
# ═══════════════════════════════════════════════════════════

class TestSkippedMarketsInit(unittest.TestCase):
    """MarketTracker 初始化包含 skipped_markets"""

    def test_has_skipped_markets(self):
        tracker = bot.MarketTracker()
        self.assertIsInstance(tracker.skipped_markets, dict)
        self.assertEqual(len(tracker.skipped_markets), 0)


class TestStartupRestore(unittest.TestCase):
    """启动时从 positions.jsonl 恢复最近 market，避免重启后重复入场。"""

    @patch.object(bot, "POSITIONS_FILE", "/tmp/test_positions.jsonl")
    @patch.object(bot.os.path, "exists", return_value=True)
    @patch.object(bot.time, "time", return_value=1_700_000_120)
    def test_restores_recent_open_slug(self, _, __):
        data = (
            '{"slug":"btc-updown-5m-1700000000","token_id":"tok1","direction":"UP","entry_price":0.45,"size":7,"entry_time":"2023-11-14T22:14:20+00:00","closed":false}\n'
        )
        with patch("builtins.open", mock_open(read_data=data)):
            tracker = bot.MarketTracker()

        self.assertIn("btc-updown-5m-1700000000", tracker.analyzed)
        self.assertIn("btc-updown-5m-1700000000", tracker.restored_slugs)
        self.assertIn("btc-updown-5m-1700000000", tracker.positions)

    @patch.object(bot, "POSITIONS_FILE", "/tmp/test_positions.jsonl")
    @patch.object(bot.os.path, "exists", return_value=True)
    @patch.object(bot.time, "time", return_value=1_700_000_360)
    def test_restores_recent_closed_slug_without_open_position(self, _, __):
        data = (
            '{"slug":"btc-updown-5m-1700000000","token_id":"tok1","direction":"UP","entry_price":0.45,"size":7,"entry_time":"2023-11-14T22:14:20+00:00","exit_time":"2023-11-14T22:19:40+00:00","closed":true}\n'
        )
        with patch("builtins.open", mock_open(read_data=data)):
            tracker = bot.MarketTracker()

        self.assertIn("btc-updown-5m-1700000000", tracker.analyzed)
        self.assertIn("btc-updown-5m-1700000000", tracker.restored_slugs)
        self.assertNotIn("btc-updown-5m-1700000000", tracker.positions)


class TestSkipRecording(unittest.TestCase):
    """晚期窗口跳过时记录到 skipped_markets"""

    def setUp(self):
        self.tracker = bot.MarketTracker()

    def test_low_conf_skip_records(self):
        """贝叶斯 conf < 0.15 跳过时应记录"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=105)

        self.tracker.tracked[slug] = market
        self.tracker.warmup_started.add(slug)
        self.tracker.warmup_data[slug] = [
            {"price": 70730, "gap": 21.7, "ts": time.time() - 30},
            {"price": 70725, "gap": 16.7, "ts": time.time() - 25},
            {"price": 70720, "gap": 11.7, "ts": time.time() - 20},
            {"price": 70715, "gap": 6.7, "ts": time.time() - 15},
        ]
        # 低置信度 updater
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=45, confidence=0.07, p_hat=0.52
        )

        # 运行 check_analysis_trigger
        self.tracker.check_analysis_trigger()

        # 低置信度不永久封锁，等待冷却后重试
        self.assertNotIn(slug, self.tracker.analyzed)

    def test_gap_cross_skip_records(self):
        """gap穿越 + 贝叶斯弱跳过时应记录"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=105)

        self.tracker.tracked[slug] = market
        self.tracker.warmup_started.add(slug)
        # 构造穿越零轴的 gap 数据（sign_changes >= 2）
        self.tracker.warmup_data[slug] = [
            {"price": 70710, "gap": 2.0, "ts": time.time() - 30},
            {"price": 70705, "gap": -3.0, "ts": time.time() - 25},
            {"price": 70712, "gap": 4.0, "ts": time.time() - 20},
            {"price": 70703, "gap": -5.0, "ts": time.time() - 15},
        ]
        # 有贝叶斯但置信度不够高
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=45, confidence=0.30, p_hat=0.55
        )

        self.tracker.check_analysis_trigger()

        # gap穿越不永久封锁，等待冷却后重试
        self.assertNotIn(slug, self.tracker.analyzed)

    def test_proceed_to_trade_clears_skip(self):
        """正常进入交易时不应留在 skipped_markets"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=105)

        self.tracker.tracked[slug] = market
        self.tracker.warmup_started.add(slug)
        self.tracker.warmup_data[slug] = [
            {"price": 70750, "gap": 42, "ts": time.time() - 30},
            {"price": 70755, "gap": 47, "ts": time.time() - 25},
            {"price": 70760, "gap": 52, "ts": time.time() - 20},
            {"price": 70765, "gap": 57, "ts": time.time() - 15},
        ]
        # 高置信度 → 应该进入 pending_trades
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=45, confidence=0.60, p_hat=0.70
        )
        self.tracker.token_cache[slug] = ("up_token", "down_token")

        # Mock analyze_and_trade 不真正执行
        with patch.object(self.tracker, "analyze_and_trade"):
            self.tracker.check_analysis_trigger()

        self.assertNotIn(slug, self.tracker.skipped_markets)
        self.assertIn(slug, self.tracker.analyzed)


# ═══════════════════════════════════════════════════════════
# 2. 波动检测逻辑测试
# ═══════════════════════════════════════════════════════════

class TestVolatilityDetection(unittest.TestCase):
    """波动重触发检测逻辑"""

    def setUp(self):
        self.tracker = bot.MarketTracker()

    def _setup_skipped_market(self, slug, coin, skip_price, atr_val=45.0,
                               skip_ago=20, elapsed=130):
        """设置一个已跳过的市场"""
        market = make_market(coin, slug, elapsed=elapsed)
        self.tracker.tracked[slug] = market
        self.tracker.warmup_started.add(slug)
        self.tracker.analyzed.add(slug)
        self.tracker.skipped_markets[slug] = {
            "skip_time": time.time() - skip_ago,
            "price_at_skip": skip_price,
            "reason": "low_conf",
            "reanalyze_count": 0,
        }
        self.tracker.bayesian_updaters[slug] = FakeUpdater(atr_val=atr_val)
        return market

    def test_large_move_triggers_reanalysis(self):
        """价格移动 >= 1.5 ATR 时触发重分析 → 同一轮晚期窗口重新处理

        波动检测 discard(slug) → 同一轮晚期窗口 reprocess →
        如果 conf 高 → 进入 pending_trades；如果仍低 → 跳过但不再记录 skip
        """
        slug = "btc-updown-5m-1700000000"
        # skip_price=70730, ATR=45 → 需要移动 67.5+
        self._setup_skipped_market(slug, "BTC", skip_price=70730, atr_val=45)
        # 价格跌到 70650 → 移动 80 > 67.5
        self.tracker.warmup_data[slug] = [
            {"price": 70730, "gap": 21.7, "ts": time.time() - 25},
            {"price": 70700, "gap": -8.5, "ts": time.time() - 20},
            {"price": 70670, "gap": -38.5, "ts": time.time() - 15},
            {"price": 70650, "gap": -58.5, "ts": time.time() - 3},
        ]
        # 重分析时贝叶斯已更新到高置信度
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=45, confidence=0.60, p_hat=0.70, direction="DOWN"
        )
        self.tracker.token_cache[slug] = ("up_token", "down_token")

        with patch.object(self.tracker, "analyze_and_trade") as mock_trade:
            self.tracker.check_analysis_trigger()
            # 波动触发 → 晚期窗口重新处理 → 进入 analyze_and_trade
            self.assertTrue(mock_trade.called)
            extra = mock_trade.call_args[0][2]
            self.assertTrue(extra.get("reanalyze"))

    def test_large_move_but_still_low_conf(self):
        """波动触发但贝叶斯仍低 → 跳过且不再记录skip（不循环）"""
        slug = "btc-updown-5m-1700000000"
        self._setup_skipped_market(slug, "BTC", skip_price=70730, atr_val=45)
        self.tracker.warmup_data[slug] = [
            {"price": 70730, "gap": 21.7, "ts": time.time() - 25},
            {"price": 70700, "gap": -8.5, "ts": time.time() - 20},
            {"price": 70670, "gap": -38.5, "ts": time.time() - 15},
            {"price": 70650, "gap": -58.5, "ts": time.time() - 3},
        ]
        # conf 仍然低
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=45, confidence=0.07, p_hat=0.53
        )

        self.tracker.check_analysis_trigger()

        # 波动触发 → 晚期窗口重新处理 → conf 仍低 → 不封锁，等待冷却重试
        self.assertNotIn(slug, self.tracker.analyzed)

    def test_small_move_no_trigger(self):
        """价格移动 < 1.5 ATR 时不触发"""
        slug = "btc-updown-5m-1700000000"
        self._setup_skipped_market(slug, "BTC", skip_price=70730, atr_val=45)
        # 价格只跌了 30 < 67.5
        self.tracker.warmup_data[slug] = [
            {"price": 70730, "gap": 21.7, "ts": time.time() - 25},
            {"price": 70710, "gap": 1.7, "ts": time.time() - 15},
            {"price": 70700, "gap": -8.5, "ts": time.time() - 3},
        ]

        self.tracker.check_analysis_trigger()

        # 仍在 analyzed 中
        self.assertIn(slug, self.tracker.analyzed)
        self.assertIn(slug, self.tracker.skipped_markets)
        self.assertEqual(self.tracker.skipped_markets[slug]["reanalyze_count"], 0)

    def test_cooldown_prevents_early_trigger(self):
        """跳过后 < 15秒不触发"""
        slug = "btc-updown-5m-1700000000"
        # skip_ago=5 → 只过了5秒
        self._setup_skipped_market(slug, "BTC", skip_price=70730, atr_val=45, skip_ago=5)
        self.tracker.warmup_data[slug] = [
            {"price": 70650, "gap": -58.5, "ts": time.time() - 1},
        ]

        self.tracker.check_analysis_trigger()

        self.assertIn(slug, self.tracker.analyzed)
        self.assertIn(slug, self.tracker.skipped_markets)
        self.assertEqual(self.tracker.skipped_markets[slug]["reanalyze_count"], 0)

    def test_max_reanalyze_limit(self):
        """已重分析过的市场不再触发"""
        slug = "btc-updown-5m-1700000000"
        self._setup_skipped_market(slug, "BTC", skip_price=70730, atr_val=45)
        self.tracker.skipped_markets[slug]["reanalyze_count"] = 1  # 已用完
        self.tracker.warmup_data[slug] = [
            {"price": 70600, "gap": -108.5, "ts": time.time() - 3},
        ]

        self.tracker.check_analysis_trigger()

        # 不应解锁
        self.assertIn(slug, self.tracker.analyzed)

    def test_no_trigger_if_position_exists(self):
        """已有持仓的市场不触发"""
        slug = "btc-updown-5m-1700000000"
        self._setup_skipped_market(slug, "BTC", skip_price=70730, atr_val=45)
        self.tracker.positions[slug] = MagicMock()  # 模拟已有持仓
        self.tracker.warmup_data[slug] = [
            {"price": 70650, "gap": -58.5, "ts": time.time() - 3},
        ]

        self.tracker.check_analysis_trigger()

        self.assertIn(slug, self.tracker.analyzed)

    def test_eth_smaller_atr(self):
        """ETH 的 ATR 更小，对应更小的触发阈值"""
        slug = "eth-updown-5m-1700000000"
        # ETH ATR=2.35 → 需要移动 3.525
        self._setup_skipped_market(slug, "ETH", skip_price=2186.68, atr_val=2.35)
        # 价格跌到 2182 → 移动 4.68 > 3.525
        self.tracker.warmup_data[slug] = [
            {"price": 2186.68, "gap": 0, "ts": time.time() - 25},
            {"price": 2184.00, "gap": -2.68, "ts": time.time() - 15},
            {"price": 2182.00, "gap": -4.68, "ts": time.time() - 3},
        ]
        # 波动触发后高置信度
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=2.35, confidence=0.55, p_hat=0.65, direction="DOWN"
        )
        self.tracker.token_cache[slug] = ("up_token", "down_token")

        with patch.object(self.tracker, "analyze_and_trade") as mock_trade:
            self.tracker.check_analysis_trigger()
            self.assertTrue(mock_trade.called)
            extra = mock_trade.call_args[0][2]
            self.assertTrue(extra.get("reanalyze"))


# ═══════════════════════════════════════════════════════════
# 3. 重分析后的完整流程测试
# ═══════════════════════════════════════════════════════════

class TestReanalysisFlow(unittest.TestCase):
    """重触发后的完整流程"""

    def setUp(self):
        self.tracker = bot.MarketTracker()

    def test_retrigger_then_reenter_late_window(self):
        """重触发后，下一轮循环进入晚期窗口重分析"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=135)
        self.tracker.tracked[slug] = market
        self.tracker.warmup_started.add(slug)
        self.tracker.token_cache[slug] = ("up_token", "down_token")

        # 先标记为跳过+已分析，且已被重触发（reanalyze_count=1, analyzed已discard）
        self.tracker.skipped_markets[slug] = {
            "skip_time": time.time() - 20,
            "price_at_skip": 70730,
            "reason": "low_conf",
            "reanalyze_count": 1,
            "move_atr": 1.8,
        }
        # analyzed 已被 discard（模拟波动重触发后的状态）
        # self.tracker.analyzed 不包含 slug

        self.tracker.warmup_data[slug] = [
            {"price": 70650, "gap": -58.5, "ts": time.time() - 3},
            {"price": 70645, "gap": -63.5, "ts": time.time() - 2},
            {"price": 70640, "gap": -68.5, "ts": time.time() - 1},
            {"price": 70638, "gap": -70.5, "ts": time.time()},
        ]
        # 高置信度（价格大幅移动后贝叶斯更新）
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=45, confidence=0.65, p_hat=0.72, direction="DOWN"
        )

        # Mock analyze_and_trade
        with patch.object(self.tracker, "analyze_and_trade") as mock_trade:
            self.tracker.check_analysis_trigger()

            # 应该调用 analyze_and_trade
            self.assertTrue(mock_trade.called)
            call_args = mock_trade.call_args
            extra_info = call_args[0][2]
            self.assertTrue(extra_info.get("reanalyze"))

        # analyzed 应被重新标记
        self.assertIn(slug, self.tracker.analyzed)
        # skipped_markets 应被清理
        self.assertNotIn(slug, self.tracker.skipped_markets)

    def test_retrigger_still_low_conf_no_second_skip(self):
        """重分析后仍低置信度 → 不再记录为 skipped（不会无限循环）"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=140)
        self.tracker.tracked[slug] = market
        self.tracker.warmup_started.add(slug)

        # 重触发状态
        self.tracker.skipped_markets[slug] = {
            "skip_time": time.time() - 25,
            "price_at_skip": 70730,
            "reason": "low_conf",
            "reanalyze_count": 1,
        }
        self.tracker.warmup_data[slug] = [
            {"price": 70650, "gap": -58.5, "ts": time.time() - 3},
            {"price": 70648, "gap": -60.5, "ts": time.time() - 2},
            {"price": 70647, "gap": -61.5, "ts": time.time() - 1},
            {"price": 70646, "gap": -62.5, "ts": time.time()},
        ]
        # 仍然低置信度
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=45, confidence=0.08, p_hat=0.53
        )

        self.tracker.check_analysis_trigger()

        # conf仍低不封锁，等待冷却重试
        self.assertNotIn(slug, self.tracker.analyzed)


class TestAnalyzeAndTradeSkipRecording(unittest.TestCase):
    """analyze_and_trade 中 should_bet=False 时记录 skip"""

    def setUp(self):
        self.tracker = bot.MarketTracker()

    @patch("auto_bot_v3.analyze_and_decide")
    @patch("auto_bot_v3.get_realtime_odds")
    @patch("auto_bot_v3.get_token_ids", return_value=("up_t", "down_t"))
    @patch("auto_bot_v3.clob_client")
    def test_should_bet_false_records_skip(self, mock_clob, mock_ids, mock_odds, mock_analyze):
        """analyze_and_decide 返回 should_bet=False 时记录 skip"""
        mock_odds.return_value = {"up_mid": 0.5, "down_mid": 0.5, "up_bid": 0.49, "down_bid": 0.49, "up_ask": 0.51, "down_ask": 0.51}
        mock_analyze.return_value = (False, "UP", 0.55, {
            "bet_reason": "EV too low",
            "current_price": 70700,
            "discount": 0.02,
            "estimated_value": 0.50,
            "diff_in_atr": 0.5,
        })

        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=105)
        self.tracker.ptb_cache[slug] = 70708
        extra_info = {"gap_trend": "扩大", "remaining_seconds": 195}

        self.tracker.analyze_and_trade(slug, market, extra_info)

        self.assertIn(slug, self.tracker.skipped_markets)
        skip = self.tracker.skipped_markets[slug]
        self.assertEqual(skip["price_at_skip"], 70700)
        self.assertIn("EV too low", skip["reason"])

    @patch("auto_bot_v3.analyze_and_decide")
    @patch("auto_bot_v3.get_realtime_odds")
    @patch("auto_bot_v3.get_token_ids", return_value=("up_t", "down_t"))
    @patch("auto_bot_v3.clob_client")
    def test_reanalyze_no_bet_clears_skip(self, mock_clob, mock_ids, mock_odds, mock_analyze):
        """重分析后仍 should_bet=False → 清理 skipped_markets（不再循环）"""
        mock_odds.return_value = {"up_mid": 0.5, "down_mid": 0.5, "up_bid": 0.49, "down_bid": 0.49, "up_ask": 0.51, "down_ask": 0.51}
        mock_analyze.return_value = (False, "DOWN", 0.45, {
            "bet_reason": "EV too low",
            "current_price": 70650,
        })

        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=135)
        self.tracker.ptb_cache[slug] = 70708
        # 标记为重分析
        self.tracker.skipped_markets[slug] = {
            "skip_time": time.time() - 30,
            "price_at_skip": 70730,
            "reason": "low_conf",
            "reanalyze_count": 1,
        }
        extra_info = {"gap_trend": "扩大", "remaining_seconds": 165, "reanalyze": True}

        self.tracker.analyze_and_trade(slug, market, extra_info)

        # 不应再在 skipped_markets 中
        self.assertNotIn(slug, self.tracker.skipped_markets)


class TestPtbFetchStrategy(unittest.TestCase):
    def setUp(self):
        bot._ptb_failures.clear()

    @patch("auto_bot_v3.subprocess.run")
    @patch("auto_bot_v3.get_price_to_beat_api", return_value=70182.430585)
    def test_get_ptb_multi_strategy_prefers_crypto_price_api(self, mock_api, mock_run):
        ptb = bot.get_ptb_multi_strategy("btc-updown-5m-1774328700")

        self.assertEqual(ptb, 70182.430585)
        mock_api.assert_called_once_with("btc-updown-5m-1774328700", timeout=3)
        mock_run.assert_not_called()
        self.assertEqual(bot._ptb_failures.get("BTC", 0), 0)

    @patch("auto_bot_v3.get_price_to_beat_api", return_value=None)
    @patch("auto_bot_v3.subprocess.run")
    def test_get_ptb_multi_strategy_falls_back_to_playwright(self, mock_run, mock_api):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="测试 slug: btc-updown-5m-1774328700\nPTB=70182.43, 耗时=1.0s\n",
            stderr="",
        )

        ptb = bot.get_ptb_multi_strategy("btc-updown-5m-1774328700")

        self.assertEqual(ptb, 70182.43)
        mock_api.assert_called_once_with("btc-updown-5m-1774328700", timeout=3)
        mock_run.assert_called_once()
        self.assertEqual(bot._ptb_failures.get("BTC", 0), 0)


class TestStrategyConfig(unittest.TestCase):
    def test_get_strategy_config_reads_timing_and_late_threshold_overrides(self):
        with patch.dict(os.environ, {
            "WARMUP_START_SECONDS": "8",
            "WARMUP_SAMPLE_INTERVAL_EARLY": "3",
            "WARMUP_SAMPLE_INTERVAL_LATE": "2",
            "EARLY_BET_START": "20",
            "EARLY_BET_END": "35",
            "EARLY_MIN_SAMPLES": "4",
            "LATE_BET_START": "90",
            "LATE_BET_END": "220",
            "LATE_MIN_UPDATES": "4",
            "LATE_LOW_CONF_THRESHOLD": "0.18",
            "LATE_GAP_CROSS_ALLOW_CONF": "0.65",
            "LATE_MATURE_SAMPLE_COUNT": "10",
            "LATE_REANALYZE_INTERVAL": "12",
            "MAX_TRADE_RETRIES": "6",
        }, clear=False):
            cfg = bot.get_strategy_config()

        self.assertEqual(cfg["warmup_start_seconds"], 8)
        self.assertEqual(cfg["warmup_sample_interval_early"], 3.0)
        self.assertEqual(cfg["warmup_sample_interval_late"], 2.0)
        self.assertEqual(cfg["early_bet_start"], 20)
        self.assertEqual(cfg["early_bet_end"], 35)
        self.assertEqual(cfg["early_min_samples"], 4)
        self.assertEqual(cfg["late_bet_start"], 90)
        self.assertEqual(cfg["late_bet_end"], 220)
        self.assertEqual(cfg["late_min_updates"], 4)
        self.assertEqual(cfg["late_low_conf_threshold"], 0.18)
        self.assertEqual(cfg["late_gap_cross_allow_conf"], 0.65)
        self.assertEqual(cfg["late_mature_sample_count"], 10)
        self.assertEqual(cfg["late_reanalyze_interval"], 12)
        self.assertEqual(cfg["max_trade_retries"], 6)


# ═══════════════════════════════════════════════════════════
# 4. Cleanup 测试
# ═══════════════════════════════════════════════════════════

class TestCleanup(unittest.TestCase):
    """cleanup 正确清理 skipped_markets 和 analyzed"""

    def test_cleanup_removes_old_data(self):
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1700000000"
        # 过期市场（结束超过120秒）
        end_time = datetime.now(timezone.utc) - timedelta(seconds=130)
        tracker.tracked[slug] = {"end_time": end_time.isoformat()}
        tracker.analyzed.add(slug)
        tracker.skipped_markets[slug] = {
            "skip_time": time.time() - 200,
            "price_at_skip": 70730,
            "reason": "low_conf",
            "reanalyze_count": 0,
        }

        tracker.cleanup()

        self.assertNotIn(slug, tracker.tracked)
        self.assertNotIn(slug, tracker.analyzed)
        self.assertNotIn(slug, tracker.skipped_markets)


# ═══════════════════════════════════════════════════════════
# 5. 边界条件测试
# ═══════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def setUp(self):
        self.tracker = bot.MarketTracker()

    def test_no_atr_no_crash(self):
        """atr_val 为 None 时不崩溃"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=130)
        self.tracker.tracked[slug] = market
        self.tracker.warmup_started.add(slug)
        self.tracker.analyzed.add(slug)
        self.tracker.skipped_markets[slug] = {
            "skip_time": time.time() - 20,
            "price_at_skip": 70730,
            "reason": "low_conf",
            "reanalyze_count": 0,
        }
        updater = FakeUpdater(atr_val=None)
        self.tracker.bayesian_updaters[slug] = updater
        self.tracker.warmup_data[slug] = [
            {"price": 70650, "gap": -58.5, "ts": time.time() - 3},
        ]

        # 不应崩溃，也不应触发
        self.tracker.check_analysis_trigger()
        self.assertIn(slug, self.tracker.analyzed)

    def test_empty_warmup_data_no_crash(self):
        """warmup_data 为空时不崩溃"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=130)
        self.tracker.tracked[slug] = market
        self.tracker.warmup_started.add(slug)
        self.tracker.analyzed.add(slug)
        self.tracker.skipped_markets[slug] = {
            "skip_time": time.time() - 20,
            "price_at_skip": 70730,
            "reason": "low_conf",
            "reanalyze_count": 0,
        }
        self.tracker.bayesian_updaters[slug] = FakeUpdater(atr_val=45)
        self.tracker.warmup_data[slug] = []

        self.tracker.check_analysis_trigger()
        self.assertIn(slug, self.tracker.analyzed)

    def test_env_var_overrides(self):
        """环境变量可覆盖默认参数: ATR_MULT=1.0 让更小的移动也触发"""
        slug = "btc-updown-5m-1700000000"
        market = make_market("BTC", slug, elapsed=130)
        self.tracker.tracked[slug] = market
        self.tracker.warmup_started.add(slug)
        self.tracker.analyzed.add(slug)
        self.tracker.skipped_markets[slug] = {
            "skip_time": time.time() - 20,
            "price_at_skip": 70730,
            "reason": "low_conf",
            "reanalyze_count": 0,
        }
        # 移动 50 < 1.5*45=67.5，但设 ATR_MULT=1.0 → 50 >= 45 → 触发
        # 触发后高置信度 → 进入 analyze_and_trade
        self.tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=45, confidence=0.60, p_hat=0.70, direction="DOWN"
        )
        self.tracker.token_cache[slug] = ("up_token", "down_token")
        self.tracker.warmup_data[slug] = [
            {"price": 70680, "gap": -28.5, "ts": time.time() - 3},
        ]

        with patch.dict(os.environ, {"REANALYZE_ATR_MULT": "1.0"}):
            with patch.object(self.tracker, "analyze_and_trade") as mock_trade:
                self.tracker.check_analysis_trigger()
                self.assertTrue(mock_trade.called)


# ═══════════════════════════════════════════════════════════
# 6. 完整场景模拟（复现日志中的BTC场景）
# ═══════════════════════════════════════════════════════════

class TestRealScenarioSimulation(unittest.TestCase):
    """复现日志: BTC PTB=70708, conf=0.07跳过, 然后价格跌到70650"""

    def test_btc_scenario(self):
        tracker = bot.MarketTracker()
        slug = "btc-updown-5m-1773902700"

        # 第一阶段: 晚期窗口 elapsed=100s, conf=0.07 → 跳过
        market = make_market("BTC", slug, elapsed=100)
        tracker.tracked[slug] = market
        tracker.warmup_started.add(slug)
        tracker.warmup_data[slug] = [
            {"price": 70730.26, "gap": 21.71, "ts": time.time() - 70},
            {"price": 70730.25, "gap": 21.70, "ts": time.time() - 65},
            {"price": 70722.18, "gap": 13.63, "ts": time.time() - 60},
            {"price": 70720.00, "gap": 11.45, "ts": time.time() - 55},
            {"price": 70700.01, "gap": -8.54, "ts": time.time() - 50},
            {"price": 70705.93, "gap": -2.62, "ts": time.time() - 45},
            {"price": 70703.04, "gap": -5.51, "ts": time.time() - 40},
            {"price": 70700.01, "gap": -8.54, "ts": time.time() - 35},
        ]
        tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=43.76, confidence=0.071, p_hat=0.5353, direction="UP"
        )

        tracker.check_analysis_trigger()

        # 验证: 跳过 + 记录
        self.assertIn(slug, tracker.skipped_markets)
        self.assertIn(slug, tracker.analyzed)
        self.assertEqual(tracker.skipped_markets[slug]["reason"], "low_conf")
        skip_price = tracker.skipped_markets[slug]["price_at_skip"]
        self.assertAlmostEqual(skip_price, 70700.01, delta=1)

        # 第二阶段: 价格大幅下跌到 70630（移动70 > 1.5*43.76=65.64）
        tracker.skipped_markets[slug]["skip_time"] = time.time() - 20
        tracker.warmup_data[slug].extend([
            {"price": 70680, "gap": -28.5, "ts": time.time() - 10},
            {"price": 70660, "gap": -48.5, "ts": time.time() - 5},
            {"price": 70630, "gap": -78.5, "ts": time.time() - 1},
        ])
        tracker.tracked[slug] = make_market("BTC", slug, elapsed=125)

        # 更新贝叶斯到高置信度（价格大幅移动后自然提升）
        tracker.bayesian_updaters[slug] = FakeUpdater(
            atr_val=43.76, confidence=0.55, p_hat=0.68, direction="DOWN"
        )
        tracker.token_cache[slug] = ("up_token", "down_token")

        # 波动触发 → 同一轮晚期窗口重新处理 → 高 conf → analyze_and_trade
        with patch.object(tracker, "analyze_and_trade") as mock_trade:
            tracker.check_analysis_trigger()
            self.assertTrue(mock_trade.called)
            extra = mock_trade.call_args[0][2]
            self.assertTrue(extra.get("reanalyze"))

        # 处理后: analyzed + skipped_markets 清理
        self.assertIn(slug, tracker.analyzed)
        self.assertNotIn(slug, tracker.skipped_markets)


if __name__ == "__main__":
    unittest.main()
