# ai_trader/oracle_sniper.py
"""
Oracle Sniper (v14.4) — Tail-segment high-confidence trading channel.

Activates in the final 30 seconds before contract deadline. Uses Chainlink
as the primary directional signal (gbm_p_up > 0.93 or < 0.07) with Binance
15-second trend as a reversal cross-validation filter.

Order placement runs a two-phase state machine:
  - remaining ∈ (15, 30]: MAKER phase (GTD ambush order, reuses SNIPER_AMBUSH_EDGE pricing)
  - remaining ∈ (0, 15]: TAKER phase (IOC fill, relies on low fee at extreme prices)

This module is INDEPENDENT of direction_truth_gate; it runs its own Chainlink +
Binance two-source pipeline. See docs/superpowers/specs/2026-04-08-oracle-sniper-design.md
for the full design document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional


@dataclass
class OracleVerdict:
    """Result of a single check_oracle_sniper() invocation.

    action:
        "BUY"    → downstream should place an order for `direction` in `phase`
        "REJECT" → gate blocked entry; `reason` explains why
        "SHADOW" → shadow mode active; check passed but no order placed
    direction:
        "UP" / "DOWN" / None (only set when action != REJECT)
    p_up:
        Computed gbm_p_up value at check time (for logging/audit)
    phase:
        "MAKER" / "TAKER" / None (routing hint for order placement)
    reason:
        One of: OK, OUT_OF_WINDOW, CL_STALE, CL_MISSING, LOW_CONFIDENCE,
        BN_CONTRADICT, BN_MISSING, COOLDOWN, COMPUTE_ERROR
    details:
        Free-form audit dict (cl_price, bn_delta_bps, remaining, etc.)
    ts:
        Unix timestamp of the check
    """

    action: Literal["BUY", "REJECT", "SHADOW"]
    direction: Optional[Literal["UP", "DOWN"]]
    p_up: Optional[float]
    phase: Optional[Literal["MAKER", "TAKER"]]
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0
