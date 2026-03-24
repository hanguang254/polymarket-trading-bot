#!/usr/bin/env python3
"""
position_monitor.py 全面测试
覆盖: 纯逻辑函数 + 带mock的交易函数 + 文件IO函数
"""
import json
import importlib.util
import math
import os
import sys
import tempfile
import unittest
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

# 在 import position_monitor 之前 mock 掉需要外部依赖的模块
# 这样 position_monitor 在 import 时不会真正连接外部服务
ROOT = Path(__file__).resolve().parents[1]
_fees_spec = importlib.util.spec_from_file_location(
    "test_position_monitor_full_fees",
    ROOT / "ai_trader" / "fees.py",
)
_fees_module = importlib.util.module_from_spec(_fees_spec)
assert _fees_spec.loader is not None
_fees_spec.loader.exec_module(_fees_module)

sys.modules.setdefault("ai_trader", MagicMock())
sys.modules.setdefault("ai_trader.polymarket_api", MagicMock())
sys.modules.setdefault("ai_trader.clob_client", MagicMock())
sys.modules.setdefault("ai_trader.binance_api", MagicMock())
sys.modules.setdefault("ai_trader.binance_api.price_stream", MagicMock())
sys.modules.setdefault("ai_trader.pyth_api", MagicMock())
sys.modules.setdefault("ai_trader.polymarket_ws", MagicMock())
sys.modules.setdefault("ai_trader.polymarket_ws.poly_ws", MagicMock())
sys.modules.setdefault("ai_trader.polymarket_rtds", MagicMock())
sys.modules.setdefault("ai_trader.coins", MagicMock())
sys.modules.setdefault("ai_trader.base_rate", MagicMock())
sys.modules.setdefault("ai_trader.fees", _fees_module)
sys.modules.setdefault("py_clob_client", MagicMock())
sys.modules.setdefault("py_clob_client.order_builder", MagicMock())
sys.modules.setdefault("py_clob_client.order_builder.constants", MagicMock(BUY="BUY", SELL="SELL"))
sys.modules.setdefault("dotenv", MagicMock())
sys.modules.setdefault("trading_state", MagicMock())

import position_monitor as pm

# 恢复 stdout/stderr（position_monitor 会替换成 _TeeWriter）
sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__


# ═══════════════════════════════════════════════════════════
# 1. 纯函数测试（无外部依赖）
# ═══════════════════════════════════════════════════════════

class TestCalcProximityThreshold(unittest.TestCase):
    """calc_proximity_threshold: 随时间衰减的 proximity zone 宽度"""

    def test_above_180s_returns_full_buffer(self):
        self.assertEqual(pm.calc_proximity_threshold(200), pm.PTB_PROXIMITY_ATR)
        self.assertEqual(pm.calc_proximity_threshold(300), pm.PTB_PROXIMITY_ATR)

    def test_at_180s_boundary(self):
        self.assertEqual(pm.calc_proximity_threshold(180), 0.3 + 0.4 * (180 - 60) / 120)

    def test_linear_decay_between_60_and_180(self):
        # 120s: 中间值
        expected = 0.3 + 0.4 * (120 - 60) / 120
        self.assertAlmostEqual(pm.calc_proximity_threshold(120), expected)
        # 60s 边界
        self.assertAlmostEqual(pm.calc_proximity_threshold(61), 0.3 + 0.4 * 1 / 120, places=4)

    def test_below_60s_returns_minimum(self):
        self.assertEqual(pm.calc_proximity_threshold(59), 0.15)
        self.assertEqual(pm.calc_proximity_threshold(30), 0.15)
        self.assertEqual(pm.calc_proximity_threshold(0), 0.15)


class TestAtrDecayArming(unittest.TestCase):
    """ATR衰减止损进入待确认状态的条件"""

    def test_armed_when_atr_collapses_and_position_is_losing(self):
        self.assertTrue(pm.should_arm_atr_decay_exit(0.08, 2.1, -0.49))

    def test_not_armed_when_atr_not_low_enough(self):
        self.assertFalse(pm.should_arm_atr_decay_exit(pm.ATR_DECAY_EXIT_THRESHOLD + 0.01, 2.1, -0.49))

    def test_not_armed_when_loss_is_too_small(self):
        self.assertFalse(pm.should_arm_atr_decay_exit(0.08, 2.1, -0.01))

    def test_not_armed_when_entry_atr_missing(self):
        self.assertFalse(pm.should_arm_atr_decay_exit(0.08, None, -0.49))


class TestProximityGuardRelease(unittest.TestCase):
    """proximity 保护在不同亏损级别下的释放条件"""

    def test_extreme_loss_releases_immediately(self):
        released, extreme_stop, threshold = pm.should_release_proximity_guard(-0.51, 130, 1, 0.25)
        self.assertTrue(released)
        self.assertEqual(extreme_stop, pm.PTB_PROXIMITY_EXTREME_STOP)
        self.assertEqual(threshold, 0)

    def test_hard_stop_drawdown_still_needs_confirmation(self):
        released, _, threshold = pm.should_release_proximity_guard(-0.30, 130, 1, 0.25)
        self.assertFalse(released)
        self.assertEqual(threshold, 2)

    def test_hard_stop_drawdown_releases_after_two_false_reads(self):
        released, _, threshold = pm.should_release_proximity_guard(-0.30, 130, 2, 0.25)
        self.assertTrue(released)
        self.assertEqual(threshold, 2)

    def test_regular_drawdown_requires_higher_streak(self):
        released, _, threshold = pm.should_release_proximity_guard(-0.20, 130, 3, 0.50)
        self.assertFalse(released)
        self.assertEqual(threshold, 4)

    def test_ratio_can_release_guard(self):
        released, _, threshold = pm.should_release_proximity_guard(-0.20, 130, 1, 0.75)
        self.assertTrue(released)
        self.assertEqual(threshold, 4)


class TestDirectionDowngrade(unittest.TestCase):
    """方向降级默认关闭，只有显式启用才允许把底层正确方向改成错误。"""

    def test_disabled_by_default(self):
        self.assertFalse(pm.should_direction_downgrade(True, 120, 0.10, -0.30))

    def test_enabled_and_meets_thresholds(self):
        with patch.object(pm, "ENABLE_DIRECTION_DOWNGRADE", True):
            self.assertTrue(pm.should_direction_downgrade(True, 120, 0.10, -0.30))

    def test_enabled_but_atr_not_low_enough(self):
        with patch.object(pm, "ENABLE_DIRECTION_DOWNGRADE", True):
            self.assertFalse(pm.should_direction_downgrade(True, 120, 0.20, -0.30))

    def test_enabled_but_remaining_too_low(self):
        with patch.object(pm, "ENABLE_DIRECTION_DOWNGRADE", True):
            self.assertFalse(pm.should_direction_downgrade(True, 45, 0.10, -0.30))


class TestDirectionFlipConfirms(unittest.TestCase):
    """方向翻转确认轮数在低ATR且时间充裕时更保守。"""

    def test_large_atr_flips_fast(self):
        self.assertEqual(pm.get_direction_flip_required_confirms(1.6, 150), 1)

    def test_medium_atr_uses_two_confirms(self):
        self.assertEqual(pm.get_direction_flip_required_confirms(1.1, 150), 2)

    def test_small_atr_and_long_time_need_four_confirms(self):
        self.assertEqual(pm.get_direction_flip_required_confirms(0.3, 150), 4)

    def test_small_atr_midgame_need_three_confirms(self):
        self.assertEqual(pm.get_direction_flip_required_confirms(0.3, 90), 3)

    def test_final_minute_relaxes_to_two_confirms(self):
        self.assertEqual(pm.get_direction_flip_required_confirms(0.3, 45), 2)


class TestTailOracleState(unittest.TestCase):
    """尾盘 oracle 状态机: median 去 spike + hysteresis + 连续确认。"""

    def test_warming_state_requires_minimum_samples(self):
        state = pm.classify_tail_oracle_state(
            "DOWN",
            100.0,
            1.0,
            [100.4, 100.6],
            20,
            wrong_streak=2,
        )
        self.assertTrue(state["active"])
        self.assertEqual(state["state"], "warming")
        self.assertTrue(state["effective_direction_correct"])
        self.assertEqual(state["wrong_streak"], 0)

    def test_single_tail_spike_is_filtered(self):
        state = pm.classify_tail_oracle_state(
            "DOWN",
            100.0,
            1.0,
            [99.8, 99.7, 100.9],
            20,
            wrong_streak=0,
        )
        self.assertEqual(state["state"], "correct")
        self.assertTrue(state["effective_direction_correct"])
        self.assertFalse(state["wrong_signal"])

    def test_final_30s_needs_three_confirms(self):
        state = pm.classify_tail_oracle_state(
            "DOWN",
            100.0,
            1.0,
            [100.4, 100.6, 100.8, 100.7, 100.9],
            25,
            wrong_streak=1,
        )
        self.assertEqual(state["required_confirms"], 3)
        self.assertEqual(state["state"], "wrong_pending")
        self.assertTrue(state["effective_direction_correct"])
        self.assertFalse(state["raw_direction_correct"])
        self.assertEqual(state["wrong_streak"], 2)

    def test_final_30s_confirms_after_third_read(self):
        state = pm.classify_tail_oracle_state(
            "DOWN",
            100.0,
            1.0,
            [100.4, 100.6, 100.8, 100.7, 100.9],
            25,
            wrong_streak=2,
        )
        self.assertEqual(state["state"], "wrong_confirmed")
        self.assertFalse(state["effective_direction_correct"])
        self.assertEqual(state["wrong_streak"], 3)

    def test_30_to_60s_confirms_faster(self):
        state = pm.classify_tail_oracle_state(
            "DOWN",
            100.0,
            1.0,
            [100.4, 100.6, 100.8, 100.7, 100.9],
            45,
            wrong_streak=1,
        )
        self.assertEqual(state["required_confirms"], 2)
        self.assertEqual(state["state"], "wrong_confirmed")
        self.assertFalse(state["effective_direction_correct"])

    def test_noise_decays_existing_wrong_streak(self):
        state = pm.classify_tail_oracle_state(
            "DOWN",
            100.0,
            1.0,
            [99.95, 100.05, 100.00, 99.98, 100.02],
            20,
            wrong_streak=2,
        )
        self.assertEqual(state["state"], "noise")
        self.assertEqual(state["wrong_streak"], 1)
        self.assertTrue(state["effective_direction_correct"])


class TestComputeP0ProfitThreshold(unittest.TestCase):
    """compute_p0_profit_threshold: 双曲贴现止盈阈值"""

    def test_basic_calculation(self):
        # remaining=60s, base=0.15, k=0.15
        # time_factor = 60/60 = 1.0
        # threshold = 0.15 * (1.0 + 0.15 * 1.0) = 0.1725
        result = pm.compute_p0_profit_threshold(60, 0.15, 0.15)
        self.assertAlmostEqual(result, 0.1725, places=4)

    def test_zero_remaining(self):
        result = pm.compute_p0_profit_threshold(0, 0.15, 0.15)
        self.assertAlmostEqual(result, 0.15, places=4)

    def test_large_remaining_high_threshold(self):
        # remaining=300s → time_factor=5.0 → 0.15*(1+0.15*5) = 0.2625
        result = pm.compute_p0_profit_threshold(300, 0.15, 0.15)
        self.assertAlmostEqual(result, 0.2625, places=4)

    def test_max_possible_profit_cap(self):
        # entry_price=0.90 → max_possible = (1.0-0.90)/0.90 ≈ 0.1111
        # cap = 0.1111 * 0.80 ≈ 0.0889
        # 不加 cap: 0.15*(1+0.15*5) = 0.2625 → 被cap限制
        result = pm.compute_p0_profit_threshold(300, 0.15, 0.15, entry_price=0.90)
        max_cap = ((1.0 - 0.90) / 0.90) * 0.80
        self.assertAlmostEqual(result, max_cap, places=4)

    def test_no_cap_when_entry_price_low(self):
        # entry_price=0.30 → max_possible = 2.333, cap=1.867
        # threshold = 0.15*(1+0.15*1)=0.1725 → 不被cap限制
        result = pm.compute_p0_profit_threshold(60, 0.15, 0.15, entry_price=0.30)
        self.assertAlmostEqual(result, 0.1725, places=4)

    def test_entry_price_none_or_zero(self):
        result1 = pm.compute_p0_profit_threshold(60, 0.15, 0.15, entry_price=None)
        result2 = pm.compute_p0_profit_threshold(60, 0.15, 0.15, entry_price=0)
        self.assertAlmostEqual(result1, 0.1725, places=4)
        self.assertAlmostEqual(result2, 0.1725, places=4)


class TestUpdateRealtimeConfidence(unittest.TestCase):
    """update_realtime_confidence: 贝叶斯置信度更新"""

    def test_up_direction_correct_boosts(self):
        # UP + price > ptb → boost
        result = pm.update_realtime_confidence(0.80, "UP", 100100, 100000, 50)
        self.assertGreater(result, 0.80)

    def test_up_direction_wrong_penalizes(self):
        # UP + price < ptb → penalty
        result = pm.update_realtime_confidence(0.80, "UP", 99900, 100000, 50)
        self.assertLess(result, 0.80)

    def test_down_direction_correct_boosts(self):
        # DOWN + price < ptb → boost
        result = pm.update_realtime_confidence(0.80, "DOWN", 99900, 100000, 50)
        self.assertGreater(result, 0.80)

    def test_down_direction_wrong_penalizes(self):
        # DOWN + price > ptb → penalty
        result = pm.update_realtime_confidence(0.80, "DOWN", 100100, 100000, 50)
        self.assertLess(result, 0.80)

    def test_max_confidence_cap(self):
        # 极大偏离，boost不超过0.99
        result = pm.update_realtime_confidence(0.95, "UP", 101000, 100000, 50)
        self.assertLessEqual(result, 0.99)

    def test_min_confidence_floor(self):
        # 极大反向偏离，penalty不低于0.1
        result = pm.update_realtime_confidence(0.20, "UP", 99000, 100000, 50)
        self.assertGreaterEqual(result, 0.1)

    def test_missing_inputs_returns_initial(self):
        self.assertEqual(pm.update_realtime_confidence(0.80, "UP", None, 100000, 50), 0.80)
        self.assertEqual(pm.update_realtime_confidence(0.80, "UP", 100000, None, 50), 0.80)
        self.assertEqual(pm.update_realtime_confidence(0.80, "UP", 100000, 100000, None), 0.80)
        self.assertEqual(pm.update_realtime_confidence(0.80, "UP", 100000, 100000, 0), 0.80)


class TestIsLosingDirection(unittest.TestCase):
    """is_losing_direction: 最后60秒方向判断"""

    def test_up_losing(self):
        self.assertTrue(pm.is_losing_direction("UP", 99000, 100000, 50))

    def test_up_winning(self):
        self.assertFalse(pm.is_losing_direction("UP", 101000, 100000, 50))

    def test_down_losing(self):
        self.assertTrue(pm.is_losing_direction("DOWN", 101000, 100000, 50))

    def test_down_winning(self):
        self.assertFalse(pm.is_losing_direction("DOWN", 99000, 100000, 50))

    def test_remaining_above_60_always_false(self):
        self.assertFalse(pm.is_losing_direction("UP", 99000, 100000, 61))
        self.assertFalse(pm.is_losing_direction("DOWN", 101000, 100000, 100))

    def test_missing_prices(self):
        self.assertFalse(pm.is_losing_direction("UP", None, 100000, 30))
        self.assertFalse(pm.is_losing_direction("UP", 100000, None, 30))


class TestShouldAttemptStopLoss(unittest.TestCase):

    def test_false_direction_triggers(self):
        self.assertTrue(pm.should_attempt_stop_loss(False))

    def test_true_direction_no_trigger(self):
        self.assertFalse(pm.should_attempt_stop_loss(True))

    def test_none_direction_no_trigger(self):
        self.assertFalse(pm.should_attempt_stop_loss(None))


class TestShouldStopLoss(unittest.TestCase):
    """should_stop_loss: 提前止损判断"""

    def test_too_early_no_stop(self):
        # elapsed < 30 → False
        self.assertFalse(pm.should_stop_loss("UP", 99000, 100000, 20))

    def test_up_wrong_direction_fallback(self):
        # 无ATR, UP但价格<PTB → True
        self.assertTrue(pm.should_stop_loss("UP", 99000, 100000, 60))

    def test_up_right_direction_fallback(self):
        # 无ATR, UP且价格>PTB → False
        self.assertFalse(pm.should_stop_loss("UP", 101000, 100000, 60))

    def test_with_atr_low_confidence_stop(self):
        # 大偏离反向 → 置信度降到<0.60 → True
        result = pm.should_stop_loss("UP", 99500, 100000, 60, initial_confidence=0.65, atr_val=50)
        # diff=500, atr=50, diff_in_atr=10, penalty=min(10*0.12, 0.5)=0.5
        # updated_conf = 0.65 - 0.5 = 0.15 < 0.60 → True
        self.assertTrue(result)

    def test_with_atr_high_confidence_hold(self):
        # 方向正确 → 置信度>0.70 → False
        result = pm.should_stop_loss("UP", 100200, 100000, 60, initial_confidence=0.85, atr_val=50)
        self.assertFalse(result)

    def test_missing_prices(self):
        self.assertFalse(pm.should_stop_loss("UP", None, 100000, 60))
        self.assertFalse(pm.should_stop_loss("UP", 100000, None, 60))


class TestGetMarketEndTime(unittest.TestCase):
    """get_market_end_time: slug解析"""

    def test_valid_slug(self):
        self.assertEqual(pm.get_market_end_time("btc-updown-5m-1700000000"), 1700000300)

    def test_invalid_slug(self):
        self.assertIsNone(pm.get_market_end_time("bad-slug"))
        self.assertIsNone(pm.get_market_end_time(""))

    def test_eth_slug(self):
        self.assertEqual(pm.get_market_end_time("eth-updown-5m-1700000000"), 1700000300)


class TestCoerceTimestamp(unittest.TestCase):
    """_coerce_timestamp: 各种格式转Unix时间戳"""

    def test_int(self):
        self.assertEqual(pm._coerce_timestamp(1700000000), 1700000000)

    def test_float(self):
        self.assertEqual(pm._coerce_timestamp(1700000000.5), 1700000000)

    def test_string_digits(self):
        self.assertEqual(pm._coerce_timestamp("1700000000"), 1700000000)

    def test_iso_string(self):
        result = pm._coerce_timestamp("2024-01-01T00:00:00+00:00")
        expected = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(result, expected)

    def test_iso_z_suffix(self):
        result = pm._coerce_timestamp("2024-01-01T00:00:00Z")
        expected = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(result, expected)

    def test_none(self):
        self.assertIsNone(pm._coerce_timestamp(None))

    def test_empty_string(self):
        self.assertIsNone(pm._coerce_timestamp(""))

    def test_zero(self):
        self.assertIsNone(pm._coerce_timestamp(0))

    def test_negative(self):
        self.assertIsNone(pm._coerce_timestamp(-1))

    def test_garbage_string(self):
        self.assertIsNone(pm._coerce_timestamp("not-a-timestamp"))


class TestResolveMarketEndTime(unittest.TestCase):
    """resolve_market_end_time: 多级回退"""

    def test_from_slug(self):
        self.assertEqual(
            pm.resolve_market_end_time("btc-updown-5m-1700000000", None, None),
            1700000300,
        )

    def test_from_position_dict(self):
        pos = {"end_timestamp": 1700000500}
        self.assertEqual(
            pm.resolve_market_end_time("bad-slug", None, pos),
            1700000500,
        )

    def test_from_entry_time(self):
        entry = "2024-01-01T00:00:00+00:00"
        expected = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()) + 300
        self.assertEqual(
            pm.resolve_market_end_time("bad-slug", entry, None),
            expected,
        )

    def test_all_fail(self):
        self.assertIsNone(pm.resolve_market_end_time("bad-slug", None, None))


class TestCalcRealtimeEv(unittest.TestCase):
    """calc_realtime_ev: 理论EV计算"""

    def test_missing_inputs(self):
        ev, p, d = pm.calc_realtime_ev("UP", None, 100000, 50, 0.50)
        self.assertIsNone(ev)

    def test_zero_atr(self):
        ev, p, d = pm.calc_realtime_ev("UP", 100000, 100000, 0, 0.50)
        self.assertIsNone(ev)

    def test_returns_three_values(self):
        # calc_realtime_ev imports get_base_rate inside the function via ai_trader.base_rate
        # which is already mocked at module level, so it'll use fallback path
        ev, p, d = pm.calc_realtime_ev("UP", 100200, 100000, 50, 0.50)
        self.assertIsNotNone(ev)
        self.assertIsNotNone(p)
        self.assertIsNotNone(d)
        # diff_in_atr = 200/50 = 4.0
        self.assertAlmostEqual(d, 4.0, places=2)


class TestFindOptimalPrice(unittest.TestCase):
    """find_optimal_price: 流动性匹配"""

    def test_none_liquidity(self):
        self.assertIsNone(pm.find_optimal_price(None, 10))

    def test_sufficient_liquidity(self):
        liquidity = {
            "best_bid": 0.50,
            "cumulative": [
                {"price": 0.50, "size": 5, "cumulative": 5},
                {"price": 0.49, "size": 10, "cumulative": 15},
                {"price": 0.48, "size": 20, "cumulative": 35},
            ],
        }
        result = pm.find_optimal_price(liquidity, 10)
        # cumulative >= 10 的第一个: price=0.49, cum=15
        self.assertEqual(result, 0.49)

    def test_insufficient_liquidity(self):
        liquidity = {
            "best_bid": 0.50,
            "cumulative": [
                {"price": 0.50, "size": 3, "cumulative": 3},
            ],
        }
        result = pm.find_optimal_price(liquidity, 100)
        # 没有满足的 → best_bid * 0.95
        self.assertAlmostEqual(result, 0.50 * 0.95, places=4)


# ═══════════════════════════════════════════════════════════
# 2. 文件IO测试（使用临时文件）
# ═══════════════════════════════════════════════════════════

class TestPositionsFileIO(unittest.TestCase):
    """get_open_positions / close_position / update_position"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.positions_file = os.path.join(self.tmpdir, "positions.jsonl")
        self._orig = pm.POSITIONS_FILE
        pm.POSITIONS_FILE = self.positions_file

    def tearDown(self):
        pm.POSITIONS_FILE = self._orig
        if os.path.exists(self.positions_file):
            os.remove(self.positions_file)
        os.rmdir(self.tmpdir)

    def _write_positions(self, positions):
        with open(self.positions_file, "w") as f:
            for pos in positions:
                f.write(json.dumps(pos) + "\n")

    def test_get_open_positions_empty(self):
        self.assertEqual(pm.get_open_positions(), [])

    def test_get_open_positions_filters_closed(self):
        self._write_positions([
            {"token_id": "a", "closed": False, "entry_time": "t1"},
            {"token_id": "b", "closed": True, "entry_time": "t2"},
            {"token_id": "c", "entry_time": "t3"},  # no closed field = open
        ])
        result = pm.get_open_positions()
        self.assertEqual(len(result), 2)
        ids = [p["token_id"] for p in result]
        self.assertIn("a", ids)
        self.assertIn("c", ids)

    def test_get_open_positions_handles_corrupt_lines(self):
        with open(self.positions_file, "w") as f:
            f.write('{"token_id": "a", "closed": false}\n')
            f.write("not json\n")
            f.write('{"token_id": "b", "closed": false}\n')
        result = pm.get_open_positions()
        self.assertEqual(len(result), 2)

    def test_close_position(self):
        pos = {"token_id": "a", "entry_time": "t1", "entry_price": 0.50, "size": 10, "slug": "test"}
        self._write_positions([pos, {"token_id": "b", "entry_time": "t2", "closed": False}])

        pm.close_position(pos, 0.80)

        all_pos = []
        with open(self.positions_file) as f:
            for line in f:
                all_pos.append(json.loads(line))
        self.assertEqual(len(all_pos), 2)
        closed = [p for p in all_pos if p["token_id"] == "a"][0]
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["exit_price"], 0.80)
        self.assertIn("exit_time", closed)

    def test_update_position_new_size(self):
        pos = {"token_id": "a", "entry_time": "t1", "size": 10}
        self._write_positions([pos])

        updated = pm.update_position(pos, new_size=8)
        self.assertTrue(updated)

        with open(self.positions_file) as f:
            result = json.loads(f.readline())
        self.assertEqual(result["size"], 8)
        self.assertIn("updated_time", result)

    def test_update_position_partial_exit(self):
        pos = {"token_id": "a", "entry_time": "t1", "size": 10}
        self._write_positions([pos])

        partial = {"time": "now", "price": 0.60, "size": 3, "label": "test"}
        updated = pm.update_position(pos, partial_exit=partial)
        self.assertTrue(updated)

        with open(self.positions_file) as f:
            result = json.loads(f.readline())
        self.assertEqual(len(result["partial_exits"]), 1)
        self.assertEqual(result["last_partial_exit"]["price"], 0.60)

    def test_update_position_not_found(self):
        pos = {"token_id": "nonexistent", "entry_time": "t99"}
        self._write_positions([{"token_id": "a", "entry_time": "t1"}])
        updated = pm.update_position(pos, new_size=5)
        self.assertFalse(updated)


# ═══════════════════════════════════════════════════════════
# 3. Mock测试（模拟外部依赖）
# ═══════════════════════════════════════════════════════════

class TestGetMarketPrice(unittest.TestCase):
    """get_market_price: 多源融合（WS → SDK → midpoint → LTP → 单边）"""

    @patch.object(pm, "_poly_ws")
    @patch.object(pm, "clob_client")
    def test_ws_price_preferred(self, mock_clob, mock_ws):
        mock_ws.get_best_bid_ask.return_value = (0.45, 0.55)
        result = pm.get_market_price("token1")
        self.assertAlmostEqual(result, 0.50, places=4)

    @patch.object(pm, "_poly_ws")
    @patch.object(pm, "clob_client")
    @patch.object(pm, "normalize_orderbook")
    def test_sdk_fallback(self, mock_norm, mock_clob, mock_ws):
        mock_ws.get_best_bid_ask.side_effect = Exception("WS down")
        book = MagicMock()
        book.bids = [MagicMock(price="0.48", size="10")]
        book.asks = [MagicMock(price="0.52", size="10")]
        mock_clob.get_orderbook.return_value = book
        mock_norm.return_value = (
            [{"price": "0.48", "size": "10"}],
            [{"price": "0.52", "size": "10"}],
        )
        result = pm.get_market_price("token1")
        self.assertAlmostEqual(result, 0.50, places=4)

    @patch.object(pm, "_poly_ws")
    @patch.object(pm, "clob_client")
    def test_midpoint_fallback(self, mock_clob, mock_ws):
        mock_ws.get_best_bid_ask.return_value = (None, None)
        mock_clob.get_orderbook.return_value = None
        mock_clob.get_midpoint.return_value = 0.55
        result = pm.get_market_price("token1")
        self.assertEqual(result, 0.55)

    @patch.object(pm, "_poly_ws")
    @patch.object(pm, "clob_client")
    def test_ltp_fallback(self, mock_clob, mock_ws):
        mock_ws.get_best_bid_ask.return_value = (None, None)
        mock_clob.get_orderbook.return_value = None
        mock_clob.get_midpoint.return_value = None
        mock_clob.get_last_trade_price.return_value = 0.60
        result = pm.get_market_price("token1")
        self.assertEqual(result, 0.60)

    @patch.object(pm, "_poly_ws")
    @patch.object(pm, "clob_client")
    def test_all_fail_returns_none(self, mock_clob, mock_ws):
        mock_ws.get_best_bid_ask.return_value = (None, None)
        mock_clob.get_orderbook.return_value = None
        mock_clob.get_midpoint.return_value = None
        mock_clob.get_last_trade_price.return_value = None
        result = pm.get_market_price("token1")
        self.assertIsNone(result)


class TestGetBestBid(unittest.TestCase):
    """get_best_bid: 止盈卖出价（含0.99折扣）"""

    @patch.object(pm, "_poly_ws")
    def test_ws_bid_with_discount(self, mock_ws):
        mock_ws.get_best_bid.return_value = 0.50
        result = pm.get_best_bid("token1")
        self.assertAlmostEqual(result, 0.50 * 0.99, places=4)

    @patch.object(pm, "_poly_ws")
    @patch.object(pm, "clob_client")
    def test_ws_too_low_falls_to_sdk(self, mock_clob, mock_ws):
        mock_ws.get_best_bid.return_value = 0.01  # < 0.02
        mock_clob.get_orderbook.return_value = None
        mock_clob.get_last_trade_price.return_value = 0.40
        result = pm.get_best_bid("token1")
        # fallback to LTP - SLIPPAGE
        expected = max(round(0.40 - pm.SLIPPAGE, 2), 0.01)
        self.assertEqual(result, expected)


class TestGetBestAsk(unittest.TestCase):
    """get_best_ask: 抄底买入价"""

    @patch.object(pm, "_poly_ws")
    def test_ws_ask(self, mock_ws):
        mock_ws.get_best_ask.return_value = 0.55
        result = pm.get_best_ask("token1")
        self.assertEqual(result, 0.55)

    @patch.object(pm, "_poly_ws")
    @patch.object(pm, "clob_client")
    def test_ws_too_high_falls_to_ltp(self, mock_clob, mock_ws):
        mock_ws.get_best_ask.return_value = 0.96  # >= 0.95
        mock_clob.get_orderbook.return_value = None
        mock_clob.get_last_trade_price.return_value = 0.55
        result = pm.get_best_ask("token1")
        expected = min(round(0.55 + pm.SLIPPAGE, 2), 0.99)
        self.assertEqual(result, expected)


class TestCheckAndAdjustSize(unittest.TestCase):
    """_check_and_adjust_size: 链上余额校验"""

    def setUp(self):
        pm._balance_cache.clear()

    @patch.object(pm, "get_token_balance", return_value=10.0)
    def test_balance_sufficient(self, _):
        result = pm._check_and_adjust_size("token1", 10.0)
        self.assertEqual(result, 10.0)

    @patch.object(pm, "get_token_balance", return_value=0.0)
    def test_balance_zero(self, _):
        result = pm._check_and_adjust_size("token1", 10.0)
        self.assertEqual(result, 0)

    @patch.object(pm, "get_token_balance", return_value=7.55)
    def test_balance_less_than_size(self, _):
        result = pm._check_and_adjust_size("token1", 10.0)
        # math.floor(7.55 * 100) / 100 = 7.55
        self.assertEqual(result, 7.55)

    @patch.object(pm, "get_token_balance", return_value=None)
    def test_balance_query_fail(self, _):
        result = pm._check_and_adjust_size("token1", 10.0)
        self.assertIsNone(result)

    @patch.object(pm, "get_token_balance", return_value=5.0)
    def test_uses_cache(self, mock_balance):
        import time as _time
        pm._balance_cache["token1"] = (8.0, _time.time())
        result = pm._check_and_adjust_size("token1", 10.0)
        # 应该用缓存的8.0，不调用get_token_balance
        self.assertEqual(result, 8.0)
        mock_balance.assert_not_called()


class TestSellPosition(unittest.TestCase):
    """sell_position: FOK卖出+降价重试"""

    @patch.object(pm, "_check_and_adjust_size", return_value=10.0)
    @patch.object(pm, "clob_client")
    def test_success_first_try(self, mock_clob, _):
        mock_clob.place_fok_order.return_value = {
            "matched": True,
            "status": "MATCHED",
            "taking": 5.0,
            "making": 0,
            "raw": "ok",
            "elapsed_ms": 100,
        }
        success, output, actual = pm.sell_position("token1", 10, 0.50)
        self.assertTrue(success)
        self.assertAlmostEqual(actual, 0.50, places=4)

    @patch.object(pm, "_check_and_adjust_size", return_value=10.0)
    @patch.object(pm, "clob_client")
    def test_success_first_try_deducts_sell_fee_when_fee_enabled(self, mock_clob, _):
        mock_clob.get_fee_rate_bps.return_value = 2500
        mock_clob.place_fok_order.return_value = {
            "matched": True,
            "status": "MATCHED",
            "taking": 5.0,
            "making": 0,
            "raw": "ok",
            "elapsed_ms": 100,
        }
        success, output, actual = pm.sell_position("token1", 10, 0.50)
        self.assertTrue(success)
        self.assertAlmostEqual(actual, 0.4922, places=4)

    @patch.object(pm, "_check_and_adjust_size", return_value=0)
    @patch.object(pm, "clob_client")
    def test_zero_balance(self, mock_clob, _):
        success, output, actual = pm.sell_position("token1", 10, 0.50)
        self.assertFalse(success)
        self.assertEqual(output, "NO_BALANCE")
        mock_clob.place_fok_order.assert_not_called()

    @patch.object(pm, "_check_and_adjust_size", return_value=10.0)
    @patch.object(pm, "clob_client")
    def test_retry_with_lower_price(self, mock_clob, _):
        mock_clob.place_fok_order.side_effect = [
            {"matched": False, "status": "LIVE", "taking": 0, "making": 0, "raw": "", "elapsed_ms": 50},
            {"matched": False, "status": "LIVE", "taking": 0, "making": 0, "raw": "", "elapsed_ms": 50},
            {"matched": True, "status": "MATCHED", "taking": 4.8, "making": 0, "raw": "ok", "elapsed_ms": 50},
        ]
        success, output, actual = pm.sell_position("token1", 10, 0.50, max_retries=3)
        self.assertTrue(success)
        # 第三次: price = 0.50 - 0.01 - 0.01 = 0.48
        self.assertAlmostEqual(actual, 0.48, places=4)


class TestMarketSellImmediate(unittest.TestCase):
    """market_sell_immediate: 止损专用市价卖"""

    @patch.object(pm, "cancel_all_orders")
    @patch.object(pm, "_check_and_adjust_size", return_value=10.0)
    @patch.object(pm, "clob_client")
    def test_fok_success(self, mock_clob, _, __):
        mock_clob.place_fok_order.return_value = {
            "matched": True,
            "status": "MATCHED",
            "taking": 3.0,
            "making": 0,
            "raw": "ok",
            "elapsed_ms": 80,
        }
        success, actual = pm.market_sell_immediate("token1", 10, price=0.30)
        self.assertTrue(success)
        self.assertAlmostEqual(actual, 0.30, places=4)

    @patch.object(pm, "cancel_all_orders")
    @patch.object(pm, "_check_and_adjust_size", return_value=0)
    @patch.object(pm, "clob_client")
    def test_no_balance(self, mock_clob, _, __):
        success, actual = pm.market_sell_immediate("token1", 10)
        self.assertFalse(success)
        self.assertEqual(actual, "NO_BALANCE")

    @patch.object(pm, "cancel_all_orders")
    @patch.object(pm, "_check_and_adjust_size", return_value=10.0)
    @patch.object(pm, "_estimate_ghost_price", return_value=0.30)
    @patch.object(pm, "clob_client")
    def test_ghost_fill_detection(self, mock_clob, _, mock_check, __):
        # FOK says ERROR, but balance is now 0 → ghost fill
        mock_clob.place_fok_order.return_value = {
            "matched": False,
            "status": "ERROR",
            "taking": 0,
            "making": 0,
            "raw": "",
            "error": "timeout",
            "elapsed_ms": 5000,
        }
        # Second call to _check_and_adjust_size returns 0 (balance cleared)
        mock_check.side_effect = [10.0, 0]
        success, actual = pm.market_sell_immediate("token1", 10, price=0.30, skip_cancel=True)
        self.assertTrue(success)

    @patch.object(pm, "cancel_all_orders")
    @patch.object(pm, "_check_and_adjust_size", return_value=10.0)
    @patch.object(pm, "_get_fresh_exit_quote")
    @patch.object(pm, "clob_client")
    def test_refreshes_quote_after_request_exception(self, mock_clob, mock_quote, _, __):
        mock_quote.side_effect = [
            {"limit_price": 0.30, "best_bid": 0.30, "bid_depth": 20, "source": "ws_book", "snapshot_age_ms": 25},
            {"limit_price": 0.27, "best_bid": 0.27, "bid_depth": 20, "source": "sdk_book", "snapshot_age_ms": None},
        ]
        mock_clob.place_fok_order.side_effect = [
            {"matched": False, "status": "ERROR", "taking": 0, "making": 0, "raw": "", "error": "Request exception!", "elapsed_ms": 300},
            {"matched": True, "status": "MATCHED", "taking": 2.7, "making": 0, "raw": "ok", "elapsed_ms": 90},
        ]

        success, actual = pm.market_sell_immediate("token1", 10, skip_cancel=True, max_retries=2)

        self.assertTrue(success)
        self.assertAlmostEqual(actual, 0.27, places=4)
        self.assertEqual(mock_clob.place_fok_order.call_args_list[0].args[2], 0.30)
        self.assertEqual(mock_clob.place_fok_order.call_args_list[1].args[2], 0.27)

    @patch.object(pm, "cancel_all_orders")
    @patch.object(pm, "_estimate_ghost_price")
    @patch.object(pm, "_check_and_adjust_size", return_value=10.0)
    @patch.object(pm, "_get_fresh_exit_quote", return_value={"limit_price": 0.30, "best_bid": 0.30, "bid_depth": 7, "source": "ws_book", "snapshot_age_ms": 15})
    @patch.object(pm, "clob_client")
    def test_explicit_fok_kill_skips_ghost_estimate(self, mock_clob, _, __, mock_ghost, ___):
        mock_clob.place_fok_order.return_value = {
            "matched": False,
            "status": "ERROR",
            "taking": 0,
            "making": 0,
            "raw": "",
            "error": "order couldn't be fully filled. FOK orders are fully filled or killed.",
            "elapsed_ms": 120,
        }

        success, actual = pm.market_sell_immediate("token1", 10, skip_cancel=True, max_retries=1)

        self.assertFalse(success)
        self.assertIsNone(actual)
        mock_ghost.assert_not_called()


class TestAnalyzeLiquidity(unittest.TestCase):
    """analyze_liquidity: 订单簿流动性分析"""

    @patch.object(pm, "_get_orderbook_bids_asks")
    def test_normal_book(self, mock_book):
        mock_book.return_value = (
            [
                {"price": "0.50", "size": "10"},
                {"price": "0.49", "size": "20"},
                {"price": "0.48", "size": "30"},
            ],
            [],
        )
        result = pm.analyze_liquidity("token1", 10)
        self.assertIsNotNone(result)
        self.assertEqual(result["best_bid"], 0.50)
        self.assertEqual(result["total_liquidity"], 60)
        self.assertEqual(result["depth"], 3)
        self.assertGreater(result["score"], 0)

    @patch.object(pm, "_get_orderbook_bids_asks")
    def test_empty_book(self, mock_book):
        mock_book.return_value = ([], [])
        result = pm.analyze_liquidity("token1", 10)
        self.assertIsNone(result)


class TestSellAndConfirm(unittest.TestCase):
    """sell_and_confirm: FOK卖出+NO_BALANCE处理+地板价兜底"""

    @patch.object(pm, "_check_and_adjust_size", return_value=10.0)
    @patch.object(pm, "clob_client")
    def test_success(self, mock_clob, _):
        mock_clob.place_fok_order.return_value = {
            "matched": True,
            "status": "MATCHED",
            "taking": 5.0,
            "making": 0,
            "elapsed_ms": 50,
        }
        success, result = pm.sell_and_confirm("token1", 10, 0.50)
        self.assertTrue(success)
        self.assertAlmostEqual(result, 0.50, places=4)

    @patch.object(pm, "_check_and_adjust_size", return_value=0)
    def test_no_balance(self, _):
        success, result = pm.sell_and_confirm("token1", 10, 0.50)
        self.assertFalse(success)
        self.assertEqual(result, "NO_BALANCE")

    @patch.object(pm, "_check_and_adjust_size", return_value=10.0)
    @patch.object(pm, "clob_client")
    def test_floor_price_fallback(self, mock_clob, _):
        # 原价失败 → 地板价$0.01成交
        mock_clob.place_fok_order.side_effect = [
            {"matched": False, "status": "LIVE", "taking": 0, "making": 0, "raw": "", "elapsed_ms": 50},
            {"matched": True, "status": "MATCHED", "taking": 1.5, "making": 0, "elapsed_ms": 50},
        ]
        success, result = pm.sell_and_confirm("token1", 10, 0.50)
        self.assertTrue(success)
        self.assertAlmostEqual(result, 0.15, places=4)  # 1.5/10


class TestBuyOppositeToken(unittest.TestCase):
    """buy_opposite_token: 对冲买入"""

    @patch.object(pm, "clob_client")
    def test_success_first_try(self, mock_clob):
        mock_clob.place_fok_order.return_value = {
            "matched": True,
            "status": "MATCHED",
            "making": 5.0,
            "taking": 0,
            "raw": "ok",
            "elapsed_ms": 80,
        }
        success, output, actual = pm.buy_opposite_token("token1", 10, 0.50)
        self.assertTrue(success)
        self.assertAlmostEqual(actual, 0.50, places=4)

    @patch.object(pm, "clob_client")
    def test_retry_with_higher_price(self, mock_clob):
        mock_clob.place_fok_order.side_effect = [
            {"matched": False, "status": "LIVE", "making": 0, "taking": 0, "raw": "", "elapsed_ms": 50},
            {"matched": True, "status": "MATCHED", "making": 5.1, "taking": 0, "raw": "ok", "elapsed_ms": 50},
        ]
        success, output, actual = pm.buy_opposite_token("token1", 10, 0.50, max_retries=2)
        self.assertTrue(success)
        # 第二次: price=0.51, making=5.1 → actual=0.51
        self.assertAlmostEqual(actual, 0.51, places=4)


class TestExecuteDipBuy(unittest.TestCase):
    """execute_dip_buy: 抄底加仓"""

    @patch("trading_state.check_daily_loss_limit", return_value=(False, -50, 50))
    def test_daily_loss_limit_blocks(self, _):
        success, size, price = pm.execute_dip_buy("token1", 10, "BTC", "slug", {})
        self.assertFalse(success)
        self.assertEqual(size, 0)

    @patch("trading_state.check_daily_loss_limit", return_value=(True, -10, 50))
    @patch.object(pm, "get_best_ask", return_value=0.40)
    @patch.object(pm, "clob_client")
    def test_success(self, mock_clob, _, __):
        mock_clob.get_fee_rate_bps.return_value = 0
        mock_clob.get_balance.return_value = 100.0
        mock_clob.get_token_balance.return_value = 5.0
        mock_clob.place_fok_order.return_value = {
            "matched": True,
            "status": "MATCHED",
            "making": 2.0,
            "taking": 5.0,
            "raw": "ok",
            "elapsed_ms": 80,
        }
        success, size, price = pm.execute_dip_buy("token1", 10, "BTC", "slug", {})
        self.assertTrue(success)
        self.assertEqual(size, 5.0)
        self.assertEqual(price, 0.4)
        mock_clob.update_token_allowance.assert_called_once_with("token1")

    @patch("trading_state.check_daily_loss_limit", return_value=(True, -10, 50))
    @patch.object(pm, "get_best_ask", return_value=0.96)
    def test_ask_too_high(self, _, __):
        success, size, price = pm.execute_dip_buy("token1", 10, "BTC", "slug", {})
        self.assertFalse(success)

    @patch("trading_state.check_daily_loss_limit", return_value=(True, -10, 50))
    @patch.object(pm, "get_best_ask", return_value=0.40)
    @patch.object(pm, "clob_client")
    def test_fee_aware_dip_buy_forces_minimum_net_size(self, mock_clob, _, __):
        mock_clob.get_fee_rate_bps.return_value = 2500
        mock_clob.get_balance.return_value = 100.0
        mock_clob.get_token_balance.return_value = 9.02656
        mock_clob.place_fok_order.return_value = {
            "matched": True,
            "status": "MATCHED",
            "making": 2.04,
            "taking": 5.1,
            "raw": "ok",
            "elapsed_ms": 70,
        }
        pos = {"size": 4.0, "token_balance": 4.0}

        success, size, price = pm.execute_dip_buy("token1", 4, "BTC", "slug", pos)

        self.assertTrue(success)
        self.assertAlmostEqual(size, 5.02656, places=5)
        self.assertAlmostEqual(price, 2.04 / 5.02656, places=5)
        self.assertEqual(mock_clob.place_fok_order.call_args.args[3], 5.1)
        self.assertAlmostEqual(pos["token_balance"], 9.02656, places=5)
        self.assertGreater(pos["dip_buy_fee_shares"], 0.07)


class TestMonitorExpiryCleanup(unittest.TestCase):
    """过期持仓应先清理，不应卡在无 orderbook 的 current_price 拉取。"""

    @patch.object(pm, "close_position")
    @patch.object(pm, "get_market_outcome", return_value=1.0)
    @patch.object(pm, "get_market_price", return_value=None)
    @patch.object(pm, "get_open_positions")
    @patch.object(pm, "reconcile_pending_orders")
    def test_expired_position_closes_without_market_price(self, _, mock_positions, mock_price, __, mock_close):
        stale_pos = {
            "token_id": "token1",
            "slug": "btc-updown-5m-1700000000",
            "direction": "UP",
            "entry_price": 0.5,
            "size": 4,
            "entry_time": "2023-11-14T22:14:20+00:00",
            "closed": False,
        }
        mock_positions.return_value = [stale_pos]

        with patch.object(pm._chainlink_stream, "start"), \
             patch.object(pm._chainlink_stream, "wait_for_update", side_effect=KeyboardInterrupt), \
             patch.object(pm._pyth_stream, "start"), \
             patch.object(pm._price_stream, "start"), \
             patch.object(pm._poly_ws, "start"):
            with self.assertRaises(KeyboardInterrupt):
                pm.monitor()

        mock_close.assert_called_once_with(stale_pos, 1.0)
        mock_price.assert_not_called()


class TestPendingOrderReconcile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.prev_cwd = os.getcwd()
        os.chdir(self.tmpdir.name)
        os.makedirs("logs", exist_ok=True)
        self.pending_file = os.path.join(self.tmpdir.name, "logs", "pending_orders.jsonl")
        self.positions_file = os.path.join(self.tmpdir.name, "logs", "positions.jsonl")

    def tearDown(self):
        os.chdir(self.prev_cwd)
        self.tmpdir.cleanup()

    @patch.object(pm, "_append_pending_update")
    @patch.object(pm, "send_telegram")
    @patch.object(pm, "cancel_all_orders")
    @patch.object(pm, "get_token_balance", side_effect=[8.15, 8.15])
    @patch.object(pm, "get_open_positions", return_value=[])
    @patch.object(pm, "_coin_from_slug", return_value="BTC")
    @patch.object(pm, "estimate_buy_fill", return_value={
        "net_size": 8.15,
        "effective_entry_price": 0.77,
        "fee_shares": 0.15,
        "fee_usdc": 0.11,
    })
    @patch.object(pm.clob_client, "update_token_allowance", return_value=True)
    @patch.object(pm.clob_client, "get_fee_rate_bps", return_value=1000)
    def test_reconcile_pending_records_cost_and_position(
        self,
        _mock_fee_rate,
        _mock_allowance,
        _mock_fill,
        _mock_coin,
        _mock_open_positions,
        _mock_balance,
        _mock_cancel,
        _mock_tg,
        mock_pending_update,
    ):
        pending_entry = {
            "pending_id": "pending-1",
            "created_at": "2026-03-24T09:33:41.000000+00:00",
            "slug": "btc-updown-5m-1774344600",
            "direction": "UP",
            "token_id": "token-1",
            "status": "PENDING",
            "limit_price": 0.77,
            "requested_size": 8.3,
            "confidence": 0.79,
            "ev": 0.021,
            "entry_details": {"p_win_final": 0.90},
            "cost_recorded": False,
        }
        with open(self.pending_file, "a") as f:
            f.write(json.dumps(pending_entry) + "\n")

        with patch.object(pm, "PENDING_ORDERS_FILE", self.pending_file), \
             patch.object(pm, "POSITIONS_FILE", self.positions_file), \
             patch("trading_state.record_bet_cost") as mock_record_cost:
            pm.reconcile_pending_orders()

        mock_record_cost.assert_called_once_with("btc-updown-5m-1774344600", 6.2755)
        with open(self.positions_file) as f:
            position = json.loads(f.readline())

        self.assertEqual(position["slug"], "btc-updown-5m-1774344600")
        self.assertEqual(position["size"], 8.15)
        self.assertEqual(position["entry_price"], 0.77)
        self.assertEqual(position["pending_order_id"], "pending-1")
        self.assertTrue(mock_pending_update.called)


class TestSelfNotify(unittest.TestCase):
    """self_notify: 通知格式化"""

    @patch.object(pm, "send_telegram")
    def test_profit_notification(self, mock_tg):
        pos = {"entry_price": 0.50}
        pm.self_notify(pos, 0.60, "BTC", "UP", 10, "test")
        mock_tg.assert_called_once()
        msg = mock_tg.call_args[0][0]
        self.assertIn("BTC", msg)
        self.assertIn("UP", msg)

    @patch.object(pm, "send_telegram")
    def test_loss_notification(self, mock_tg):
        pos = {"entry_price": 0.50}
        pm.self_notify(pos, 0.30, "BTC", "UP", 10, "止损")
        msg = mock_tg.call_args[0][0]
        self.assertIn("亏损", msg)


# ═══════════════════════════════════════════════════════════
# 4. TeeWriter 测试
# ═══════════════════════════════════════════════════════════

class TestTeeWriter(unittest.TestCase):
    """_TeeWriter: 同时写终端和日志"""

    def test_write_and_flush(self):
        from io import StringIO
        stream = StringIO()
        writer = pm._TeeWriter(stream)
        writer.write("hello")
        writer.flush()
        self.assertEqual(stream.getvalue(), "hello")


# ═══════════════════════════════════════════════════════════
# 5. Pre-orders 测试
# ═══════════════════════════════════════════════════════════

class TestPreOrders(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.pre_orders_file = os.path.join(self.tmpdir, "pre_orders.json")
        self._orig = pm.PRE_ORDERS_FILE
        pm.PRE_ORDERS_FILE = self.pre_orders_file

    def tearDown(self):
        pm.PRE_ORDERS_FILE = self._orig
        if os.path.exists(self.pre_orders_file):
            os.remove(self.pre_orders_file)
        os.rmdir(self.tmpdir)

    def test_save_and_load(self):
        data = {"slug1": {"type": "tp", "price": 0.60, "time": "now"}}
        pm._save_pre_orders(data)
        loaded = pm._load_pre_orders()
        self.assertEqual(loaded["slug1"]["price"], 0.60)

    def test_load_missing_file(self):
        loaded = pm._load_pre_orders()
        self.assertEqual(loaded, {})


if __name__ == "__main__":
    unittest.main()
