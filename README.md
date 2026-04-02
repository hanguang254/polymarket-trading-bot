# Polymarket Trading Bot v14.3

Automated trading bot for Polymarket 5-minute crypto UP/DOWN markets. Uses EV-driven entry/exit with Bayesian sequential updating, random-walk probability modeling, LMSR theoretical pricing, momentum sniper fast-path, trailing take-profit, event-driven WebSocket orderbook, market-price stop-loss, pending order reconciliation, and correlated exposure control.

## Strategy

**EV-Driven Entry + Market-Price Stop-Loss**

The bot uses Bayesian sequential updating to detect directional signals, enters when EV (p_win - execution_price) is positive with sufficient confidence, and manages positions with full-duration market-price stop-loss.

### Execution Timeline (300s market, all timings configurable via `.env`)

```
0s    Market starts
2s    PTB acquisition (crypto-price API, Playwright fallback)
Ns    Bayesian warmup starts (WARMUP_START_SECONDS, default 20)
      Sampling: WARMUP_SAMPLE_INTERVAL_EARLY (5s) → _LATE (3s) after early window
40s   PTB deadline
E0-E1 Early bet window (EARLY_BET_START-EARLY_BET_END, default 90-95s)
L0-L1 Late bet window (LATE_BET_START-LATE_BET_END, default 100-160s)
      → Real-time EV monitoring starts after bet
      ── Exit Protocol ──
>30s  PTB Proximity Buffer: crypto near PTB → freeze direction signal (prevent noise stop-loss)
>30s  -25% hard stop: unconditional market sell (proximity extreme stop at -50%)
>30s  Direction flip (True→False): consecutive confirmation required, then liquidation
>30s  Token drop ≥15%: ATR ≥2.0 → dip-buy 50% / ATR 1.0-2.0 → hold / ATR <1.0 → stop-loss
>30s  Universal stop-loss: direction wrong + streak confirmed → market-price ladder sell
>30s  P1 hedge: bid < $0.05 → buy opposite token for $1 pair
>90s  P0 take-profit: hyperbolic discounting threshold
120s  Stage 2: Weak signal exit (batch sell)
60s   Stage 3: Aggressive exit (EV floor protection)
30s   Stage 4: EV ≥ 0 → hold to settlement, EV < 0 → floor prices
0s    Market ends → Settlement (API real outcome)
```

## Architecture

```
systemd services (auto-restart, boot-start):

polymarket-bot.service     → auto_bot_v3.py      (betting engine)
polymarket-monitor.service → position_monitor.py  (EV-driven position monitor + pending order reconciliation)
polymarket-redeem.service  → auto_redeem_v2.py    (auto settlement + balance query)
watchdog_v3.sh             → process watchdog     (monitors all 3 services)
```

## Features

### Entry (P0 + P2 + P3)
- **Random Walk p_win**: Uses `Φ(|gap|/σ√t)` (normal CDF) to compute real probability from price deviation, replacing static base_rate lookup. More theoretically grounded for 5-min markets.
- **Base Rate Calibration**: Conservative ATR-band priors (0.50-0.85), auto-calibrates with empirical data after 30+ samples per band. Weak edge (base_rate < 0.55) halves Kelly.
- **Strict Binary EV**: `EV = p_win - price` (replaces discount/odds ratio). Minimum edge required (configurable via `MIN_EV`).
- **Early Bet Window (35-99s)**: Enters before CLOB fully prices in, with lower thresholds (`EARLY_MIN_EV`, `EARLY_MIN_CONFIDENCE`). Captures mispricing before market makers adjust.
- **Momentum Sniper (v12)**: Fast-path entry when large price deviation detected during warmup sampling (ATR>=1.0 + gap direction matches Bayesian + CLOB token <= $0.55). Skips full analysis pipeline (saves ~1.5s), uses Bayesian p_hat + random walk for p_win -> execute_bet. Detection to order ~300ms (FOK network latency only). Triggers from 15s onwards (independent of early window), covers all time slots. v12.9.1: Main-thread inline sniper disabled (lacks safety checks — WS health/gap momentum/FOK revalidation), all sniper entries now through dedicated sniper thread or directional ambush. Telegram notifications distinguish 4 modes: `sniper_thread(independent)` / `momentum_sniper(main)` / `ambush_fill(limit)` / `normal_bet`. Logs include step-by-step latency (WS read/calc/FOK).
- **Sniper Safety Guards (v12.9)**: Four-layer defense against false signal entries: (1) **WS Health Check** — blocks FOK when WS BBA age > `SNIPER_WS_MAX_AGE` (default 5s), prevents stale REST fallback prices from triggering entries (production case: WS age=9999ms caused REST $0.61->$0.50 flash entry). Ambush limit orders still allowed during WS outage. (2) **Gap Momentum Check** — skips when current gap < peak gap x `SNIPER_GAP_DECAY_RATIO` (default 0.75), detects dead-cat bounces and mean reversion. (3) **Time-Decayed Shrinkage** — when `remaining > SNIPER_SHRINK_FULL_AT` (default 120s), p_win shrinkage scales down proportionally, prevents overconfident early entries. (4) **Pre-FOK Revalidation** — re-reads Chainlink price before order submission; aborts if ATR dropped below threshold or direction reversed (solves 1-2s price shift between condition pass and FOK execution).
- **Double-Entry Protection (v12.9)**: `analyze_and_trade()` entry + pre-`execute_bet` dual check on `self.positions` + `_sniper_processing`, blocking race condition between sniper thread and normal analysis path (production case: same slug entered by both paths within 0.5s, $10.91 exposed in same direction).
- **Smart Ambush v2.0 (v13)**: GTD limit order strategy with 5-module enhancement. When sniper detects confirmed direction (Bayesian + gap aligned) but ask exceeds `SNIPER_MAX_PRICE` or WS is stale, places a GTD BUY limit on the confirmed direction only. **Module 1+5 Dynamic EDGE**: Avellaneda-Stoikov time-decay `EDGE = base × √(remaining/300) + min_edge`, multiplied by GLFT volatility-adaptive factor `clamp(atr/avg_atr, 0.5, 2.0)`. Wider spread in high-vol, tighter in calm markets. **Module 2 Bidirectional Repricing**: Backward (price-lowering) reprice only allowed when old order's edge is thinner than `EDGE_MIN` floor (`_old_price >= p_win - min_edge`), preventing profitable limit orders from being pulled back. **Module 3 OFI Detection**: Pre-placement orderbook depth check — `OFI = (bid_depth - ask_depth) / total_depth` at top-5 levels, skips placement when `OFI < -OFI_THRESHOLD` (adverse selection filter). **Module 4 Toxic Fill Detection**: Fill within 2s of current order placement (`last_order_ts`, reset on each reprice) raises `FILL_MIN_CONF` to 0.35, forcing sell on low-confidence toxic fills from informed adversaries. Post-fill direction validation re-checks Bayesian direction and confidence — if direction flipped or confidence < `SNIPER_AMBUSH_FILL_MIN_CONF`, immediately FOK sells. Anti-loop guards, Kelly-sized positions (MIN/MAX_BET_SIZE bounds). Env: `SNIPER_AMBUSH_EDGE_MIN`, `SNIPER_AMBUSH_AVG_ATR`, `SNIPER_AMBUSH_OFI_THRESHOLD`.
- **Shadow Mode (v14.1)**: Taker sniper and endgame entries default to shadow mode (`SNIPER_TAKER_LIVE=0`, `ENDGAME_LIVE=0`) — signals are logged via `trade_logger` but no orders placed. Allows after-fee EV validation before going live. Ambush maker path remains live (zero maker fee). Toggle to `=1` to restore real trading.
- **Fast Direction Module (v14.1)**: `ai_trader/fast_direction.py` fuses 3 signals in 3-5s for pre-Bayesian direction estimate: (1) Binance trade-flow OFI (leads Chainlink 10-30s, weight 0.40), (2) cross-exchange price spread BN-CL/ATR (weight 0.35), (3) Polymarket orderbook imbalance OBI (market-maker repositioning signal, weight 0.25). Outputs direction/confidence/prior_bias for Bayesian prior seeding. Optional ambush gate: `AMBUSH_REQUIRE_FAST_DIR=1` requires fast direction alignment before placing ambush orders.
- **Ambush GTD v14.2 Tuning**: Reprice cap raised 5→8 (`SNIPER_AMBUSH_REPRICE_MAX`), GTD lifetime shortened 90→45s (`SNIPER_AMBUSH_GTD_REPRICE_SEC`, actual expiry=45+60s safety margin=105s). Faster repricing cycle improves fill probability in fast-moving markets. Counter resets only after GTD expires, not on each reprice.
- **Phased Time-Decay TP/SL (v14)**: Replaces binary hold-to-expiry with 4-phase ambush position management, controlled by `AMBUSH_HOLD_TO_EXPIRY=1`:
  - **Rapid phase** (held ≤30s): Aggressive `AMBUSH_RAPID_SL` (-25%) with 3-round confirmation (`rapid_sl_confirms`). Direction ❌ path only.
  - **Mid phase** (30s < held, remaining >60s): `AMBUSH_MID_SL` (-35%) threshold. Direction ✅ adds `AMBUSH_NOISE_TOLERANCE` (+10%, total -45%), linearly decaying from 120s→60s remaining. Direction ❌ uses base threshold only.
  - **Late phase** (remaining ≤60s): Tighter `AMBUSH_LATE_SL` (-30%). Near settlement, noise is lower so tighter stops are appropriate.
  - **Absolute hard stop**: `AMBUSH_HARD_STOP` (-70%) fires in ALL phases regardless of direction.
  - **Phased take-profit**: `AMBUSH_TP_PHASE1_RATIO` (60%) → `PHASE2` (55%) → `PHASE3` (40%) → `PHASE4_FIXED` ($0.05), decaying with remaining time.
  - **Breakeven lock**: After profit reaches `AMBUSH_BREAKEVEN_TRIGGER` (15%), trailing stop at `AMBUSH_BREAKEVEN_MARGIN` (2%) above entry.
  - **Direction confirmation window**: First `AMBUSH_CONFIRM_WINDOW` (15s) after fill, confirms direction via `AMBUSH_CONFIRM_THRESHOLD` price movement.
- **CL-BN Skew Protection (v12.9.3)**: When Chainlink lags Binance by > `EV_SKEW_BLOCK_ATR` (default 1.0 ATR), EV stop-loss is suspended — CL direction judgment is unreliable with $40-50 price discrepancy. Reads directly from CL/BN price snapshots (independent of `ENABLE_ORACLE_STALE_WATCH`). Additionally, when direction is correct and Binance confirms with gap >= 0.3 ATR, EV exit is blocked regardless of CL-based P(win). Absolute hard stop (-40%/-70%) remains as final safety net.
- **p_win Shrinkage Calibration (v12)**: `P_WIN_SHRINKAGE` parameter shrinks p_win toward 0.5 (default 0.80), correcting systematic overconfidence from Random Walk + Bayesian fusion. `p_win = 0.5 + (raw - 0.5) x shrinkage`. Log outputs `p_win_raw` for calibration comparison. v12.9: sniper thread adds time-decayed shrinkage (`SNIPER_SHRINK_FULL_AT`) — more conservative when more time remains.
- **Continuous estimated_value (v12)**: ATR-to-token valuation changed from discrete if-elif table to linear interpolation (`_interpolate_estimated_value`), eliminating step jumps where 1.01 ATR and 1.49 ATR had identical valuations.
- **Bidirectional Bayesian Fusion (v12)**: Changed `p_win = max(p_win, fused_p)` one-way gate to `p_win = fused_p` bidirectional fusion, allowing Bayesian to pull down overly high Random Walk p_win.
- **5-min K-line Trend Filter**: Checks Binance 5-min candle trend (same timeframe as market) to avoid counter-trend trades. Replaces 15-min filter which was too slow for 5-minute markets.
- **Bayesian Fusion (v2.1 Gate)**: Engine upgraded to dual-channel: incremental signal (delta-price) + state signal (gap/ATR/remaining time). `_gate_signal()` fuses both channels (state weight 65% when aligned, 85% when conflicting), outputs unified direction/confidence for pre-screening and EV calculation. p_win fusion uses incremental posterior only (avoids double-counting state probability in EV). Continuous direction flipping triggers soft reset (`_maybe_soft_reset`, gap >= 0.6 ATR + 3 consecutive reversals -> reset posterior to 0.58 seed), reducing early wrong-direction anchoring.
- **Cross-Validation**: Flags overestimation when `estimated_value > p_win + 0.15` (reduces confidence 15%).
- **LMSR Inefficiency Signal**: When realtime `best_ask` diverges >10% from `p_win`, lowers discount threshold by 2% (min 6%) for easier entry on mispriced markets.
- **LMSR Liquidity Assessment**: Orderbook spread/depth/slippage scoring → dynamic discount threshold (8%-20%).
- **Exit Liquidity Gate (P2)**: Before entering, checks bid-side depth of the chosen token. `bid_depth < 5` → skip entry entirely. Prevents entering positions that can't be exited.
- **Correlated Exposure Control**: BTC/ETH correlation ~0.85. Same-direction position halves Kelly sizing.
- **1/4 Kelly Sizing**: Binary formula `f* = (p - price) / (1 - price)`, quarter Kelly, 5-10 shares hard bounds. Computes target net position from fee-aware effective entry price; skips when `EV<=0`, `Kelly<=0`, or hard cap below minimum net position. When `0 < raw_net_size < MIN_BET_SIZE`, pads to minimum net size then back-calculates gross order quantity.
- **Liquidity-Capped Sizing (P3)**: Kelly size capped at 50% of exit bid depth. Works with P2 — P2 gates entry, P3 adjusts size.
- **Balance Auto-Retry**: When balance insufficient, automatically retries with reduced size (98%/95%/90%) instead of skipping entirely.
- **Post-Buy Allowance Refresh**: After successful buy (both direct and pending fill), queries actual on-chain `token_balance` and calls `update_balance_allowance` to ensure sell authorization is pre-set. Prevents "not enough balance / allowance" errors at exit time.
- **Parallel Entry Fetch**: Balance + orderbook queries run concurrently via ThreadPoolExecutor, saving ~0.5s per bet execution.
- **CLOB C1 Calibration**: Replaces Gamma odds with CLOB `best_ask` for discount/EV/price checks — prevents buying at $0.85 while thinking price is $0.50. Empty book detection (ask ≥ 0.95) falls back to `last-trade-price`.
- **Empty Book Override**: When C1 calibration makes discount negative (stale last-trade-price > estimated_value), a second-chance check uses Gamma odds: if `gamma_discount ≥ threshold`, `gamma_EV > 0.05`, and `confidence ≥ 75%`, overrides to BET. Execution still uses calibrated price (last-trade-price + SLIPPAGE).
- **Volatility-Triggered Re-Analysis**: Markets skipped due to low confidence are tracked in `skipped_markets`. When crypto price moves ≥1.5 ATR from the skip price (`REANALYZE_ATR_MULT`), the market re-enters the analysis pipeline with updated Bayesian state. The same re-analysis controls apply to both early and late windows; if an early-window retry is retriggered, it carries the same `reanalyze` semantics so failed retries do not reset back into a fresh skip loop. Entry-stage periodic rescans now also share one cooldown env: `ENTRY_REANALYZE_INTERVAL` (falls back to legacy `LATE_REANALYZE_INTERVAL`). Cooldown (`REANALYZE_COOLDOWN`, default 15s) and max retrigger cap (`MAX_REANALYZE`, default 1) prevent loops. Skipped markets are cleaned up on position entry or market cleanup.
- **Pending Order Tracking**: LIVE (unfilled) orders are recorded to `pending_orders.jsonl` and reconciled by position_monitor when filled on-chain. FOK request exceptions (`Request exception` / `timeout` / `connection reset` and other transport errors) without on-chain balance confirmation automatically write `PENDING_GHOST` for reconciliation, instead of discarding as failed — solves network jitter causing actual fills to be misjudged as unfilled.
- **Ghost Fill Multi-Recheck**: `_detect_ghost_fill()` supports `GHOST_FILL_RECHECKS` (default 4) x `GHOST_FILL_RECHECK_INTERVAL` (default 0.25s) multiple on-chain balance rechecks, tolerating RPC latency. `GHOST_FILL_MIN_SIZE` controls minimum confirmation threshold.
- **Directional Parameter Config**: `MAX_BUY_PRICE`/`MIN_EV`/`MIN_CONFIDENCE` support `_UP`/`_DOWN` suffix overrides (e.g., `MIN_EV_UP=0.04`, `MIN_EV_DOWN=0.08`), early window likewise. Falls back to unified threshold when unset.
- **FOK Entry Restructuring**: Pre-execution refreshes orderbook snapshot via `_get_execution_quote()`, `_plan_fok_entry()` plans limit price and size based on `price_cap` (p_win price ceiling). Retries refresh orderbook again, routing by price drift/depth shortage, replacing old fixed +2tick repricing. `_is_explicit_fok_kill()` distinguishes explicit FOK rejection vs network timeout — former skips on-chain balance recheck.
- **CLOB Price Validation**: `_is_valid_clob_price()` unified price validity check (0.01 < price < 0.99), replacing scattered `if price is None` checks.
- **Fee-Aware EV**: EV calculation deducts entry_fee_cost + exit_fee_cost (scaled by `EARLY_EXIT_RATIO`), no longer just spread_cost. `effective_entry_price = price / (1 - fee_rate)` reflects buy fee impact on actual entry cost.
- **Fee-Aware Kelly Sizing**: `calculate_kelly_size()` computes target net position from fee-inclusive effective entry price, then back-calculates gross order quantity (`gross = net / fill_ratio`). Supports `SIZE_STEP` (default 0.1) fractional share precision. Returns detailed dict (`return_details=True`) with gross_order_size/expected_net_size/skip_reason fields.
- **Fee Module (`ai_trader/fees.py`)**: `effective_fee_rate(price, fee_rate_bps)` computes actual taker fee rate, with fee curve exponent auto-inferred by market type (crypto 2500bps->exponent=2, 720bps->exponent=1). `estimate_buy_fill()` / `estimate_sell_fill()` handle buy (shares-based fee) and sell (USDC-based fee) gross->net conversion respectively.

### Exit (P0-P1 + P4)
- **PTB Proximity Buffer**: When `abs(crypto_price - ptb_price) / ATR < dynamic_threshold`, crypto is in the "noise zone" near PTB — direction signal is unreliable. Freezes `direction_correct = True` (trusts original bet), suppressing all direction-based stop-losses. Threshold decays with time: 0.7 ATR (first 2min) → 0.3 ATR (mid) → 0.15 ATR (last 1min). Extreme safety valve: token drop ≥50% (`PTB_PROXIMITY_EXTREME_STOP`) forces exit regardless. Configurable via `PTB_PROXIMITY_ATR`.
- **-20% Hard Stop (v12)**: When token drops >= 20% from entry price, market sell when direction is not confirmed correct. In proximity zone, direction is frozen True so hard stop only fires via extreme safety valve (-25%). v12: Tightened from -25%/-50% to -20%/-25%, reducing catastrophic losses. **v12.9: Last-60s relaxation** — when `remaining <= 60s`, absolute hard stop threshold relaxes from -40% to -70%, preventing noise-driven exits on winning positions near settlement (production case: correct direction but temporary spike triggered -55% stop, DOWN ultimately won but was already stopped out, lost $1.54 instead of profiting $1.90).
- **Force-Close Escalation (v12)**: After close intent is locked, 5 consecutive FOK failures trigger automatic floor-price $0.01 force-close, preventing positions from hanging forever.
- **Direction Flip Exit (Consecutive Confirmation)**: Tracks `direction_correct` across cycles. True→False flip now requires **consecutive confirmation** (2 rounds for ATR<1.5, 1 round for ATR≥1.5) before liquidation — prevents single-poll noise from triggering premature exits. `direction_wrong_streak` counter resets when direction returns to True.
- **ATR 3-Layer Decision Matrix**: When token drops ≥15% (`PRICE_DROP_TRIGGER`), action depends on ATR deviation (crypto distance from PTB in ATR units):
  - **ATR ≥ 2.0** (safe zone): 🟢 Dip-buy — adds 50% of original position at best_ask via FOK. Requires: direction correct, remaining > 60s, max 1 dip-buy per position. Averages down cost basis for higher profit if direction holds.
  - **ATR 1.0-2.0** (uncertain): 🟡 Hold — no action, continue monitoring. ATR rising → enters safe zone, ATR falling → enters danger zone.
  - **ATR < 1.0** (danger zone): 🔴 Stop-loss — immediate sell at best_bid, only when direction is wrong. Direction correct + ATR dip is normal volatility, not a stop signal.
- **Hyperbolic Discounting Profit-Take (P0) + Trailing TP (v12.8)**: Two-layer take-profit system. Layer 1: dynamic threshold `base x (1 + k x minutes_remaining)`, activates Layer 2 when profit target met. Layer 2: Trailing Take-Profit — tracks highest profit during hold (high_water_mark), exits when drawdown from peak exceeds dynamic threshold. Drawdown thresholds by profit tier: >30% profit tolerates 30% drawdown, >20% tolerates 35%, >15% tolerates 40%. Drawdown tolerance auto-tightens when remaining<120s. Direction correct + ATR>=2.0 relaxes by 20% for more room. Solves the core problem of "peak +22.8% sliding all the way to +4.5% before taking profit".Configurable via `P0_BASE_PROFIT` (default 0.15), `P0_HYPERBOLIC_K` (default 0.15), `TRAILING_TP_ENABLED` (default 1).
- **EV-Gate Stop-Loss (v11)**: Core binary market stop-loss redesign. `calc_ev_comparison()` compares `EV(hold to settlement) = P(win)` vs `EV(sell) = net_bid_price` every tick, only stops when selling is better. `random_walk_p_win()` uses oracle (Chainlink) gap/ATR/remaining time for win probability, independent of CLOB token price. Solves the core issue of "direction correct but ATR near 0 causing false kills" (13/23 losses). Requires `EV_EXIT_CONFIRMATIONS` (default 2) consecutive confirmations. Three circuit breakers bypass EV Gate: CB1 (oracle missing + loss >30%), CB2 (token drop >=70%), CB3 (settlement approaching + tail oracle confirms wrong direction). `ENABLE_EV_GATE=0` reverts to old logic.
- **Market-Price Immediate Stop-Loss**: `market_sell_immediate()` cancels existing orders first (prevents balance lock), then ladder sells at bid→95%→90%→80%→$0.05→$0.01 (~6s to clear). On "not enough balance / allowance" error with confirmed on-chain balance, auto-refreshes allowance and retries before falling back to reduced-size attempts.
- **Opposite Token Hedge (P1)**: When losing and bid < $0.05 (no buyers), buys the opposite token to form a guaranteed pair (UP + DOWN = $1.00 at settlement). Only hedges when `opposite_ask < (1.00 - entry_price - 0.02)`, ensuring net profit. Opposite token_id is recorded at entry time.
- **EV-Driven Diamond Hands**: Direction correct + token drop < 15% → calculates real-time EV. High EV (>0.03) or large ATR deviation (≥ time-decayed threshold) → hold to settlement. Weak signal → releases to Stage 2/3/4 for fine-grained exit.
- **Direction-Based Exit**: Uses crypto price vs PTB to determine win/loss (not token orderbook price, which can be misleading due to wide bid-ask spreads near settlement).
  - Crypto near PTB (< dynamic ATR threshold) → **Proximity freeze** (direction = True, hold)
  - Token drop ≥ 25% + direction not True → **-25% hard stop** (proximity zone: -50% extreme stop)
  - Direction flips True→False → **Consecutive confirmation** then liquidation
  - Token drop ≥ 15% → **ATR 3-layer decision** (dip-buy / hold / stop-loss)
  - Direction correct + EV > 0.03 or strong ATR → **hold to settlement** ($1.00)
  - Direction correct but weak signal → releases to stage exit strategy
  - Direction wrong + streak confirmed → **Universal market-price stop-loss** (ladder sell)
  - No buyers (bid < $0.05) → Attempt hedge (P1), fall back to wait for expiry
- **Wide Spread Protection**: `get_market_price()` detects bid-ask spread > 50% and falls back to midpoint API → `last-trade-price` → single-side price, preventing false loss signals.
- **Empty Book Fallback (Monitor)**: `get_best_bid()` / `get_best_ask()` fall back to `last-trade-price ± SLIPPAGE` when CLOB is empty (bid ≤ 0.02 or ask ≥ 0.95). Enables hedge and stop-loss decisions with real market prices instead of $0.01/$0.99.
- **EV Sanity Check (Stage 3/4)**: When EV says "hold" but `last-trade-price < entry_price × 0.5` (market price halved), overrides EV to negative — prevents holding to settlement on fake positive EV.
- **Post-Close Safety**: No sell logic runs after market close (remaining < 0), preventing direction flicker from causing unwanted trades.
- **End-of-Market Oracle State Machine**: When remaining <=60s, direction determination switches to `classify_tail_oracle_state()`: collects last 7 oracle prices (`TAIL_ORACLE_WINDOW`), uses median to filter single Chainlink spikes, MAD x 3.0 + ATR floor for hysteresis threshold. State machine: warming -> correct/noise/wrong_pending -> wrong_confirmed. Only `wrong_confirmed` (consecutive confirmation rounds met: <=30s needs 3 rounds, 30-60s needs 2 rounds) triggers stop-loss, preventing single-tick spikes from killing winning positions. Proximity buffer no longer overrides direction signal during end-of-market phase.
- **3-Stage Graduated Exit**: For remaining edge cases — Stage 2 (120-60s) with batch orders, Stage 3 (60-30s) aggressive, Stage 4 (30-0s) EV≥0 hold / EV<0 floor prices.
- **Close Intent Locking**: When stop-loss decision triggers, `_arm_close_intent()` persists close intent to position record (reason + timestamp). If current-round sell fails (network/depth issues), next loop cycle detects `close_intent_active` and enters sell flow directly, skipping all condition checks. Ensures stop-loss decisions are not lost between polling intervals. All stop-loss paths (ATR decay/ATR acceleration/hard stop/direction flip/ATR matrix/wrong direction) are connected.
- **Stop-Loss Sell Retry**: `market_sell_immediate()` adds `max_retries` parameter (default 3), each retry calls `_get_fresh_exit_quote()` to refresh orderbook pricing, replacing old single fixed price-reduction strategy.
- **Post-Dip-Buy Allowance Refresh**: After dip-buy (50% add) fills, immediately calls `update_token_allowance()`, preventing insufficient allowance errors on subsequent sells.
- **Fee-Aware Selling**: All sell paths (`market_sell_immediate`/`sell_position`/`sell_and_confirm`) use `estimate_sell_fill()` to compute net price after fee deduction, logs show gross/net/fee three-part breakdown.
- **Fee-Aware Dip-Buy**: `_plan_dip_buy_size()` plans dip-buy gross order quantity with fee-aware logic, `_finalize_dip_fill()` calibrates actual net shares received via on-chain `token_balance`, position records include `dip_buy_fee_shares`/`dip_buy_fee_usdc`/`dip_buy_cost`.
- **ATR Decay Stop Consecutive Confirmation**: ATR decay stop (ATR < 0.5 and decay >70%) enters pending state, requiring `ATR_DECAY_CONFIRMATIONS` (default 3) consecutive confirmations + `true_direction_correct=False` before triggering close, preventing single-spike false kills on winning positions. Proximity freeze zone equally protected.
- **Direction Downgrade Toggle**: `ENABLE_DIRECTION_DOWNGRADE=0` (default off) — no longer force-downgrades positions whose underlying direction is actually correct. Only when explicitly enabled, ATR < `ATR_DOWNGRADE_THRESHOLD` downgrades direction_correct.
- **Close Price Freeze**: Freezes underlying crypto price to `close_crypto_price` 5s before market close. When API has no outcome and needs fallback judgment, prefers frozen price (live price may have drifted to next market).
- **Realized PnL Aggregation (v13 fix)**: `_calculate_total_realized_pnl()` aggregates partial fills + final close real PnL. Win/loss determination based on total realized PnL (replaces simple entry vs exit price comparison), more accurate for add/reduce position scenarios. v13: `_resolve_or_estimate_exit_price()` — NO_BALANCE路径优先查`get_market_outcome()`结算价($1.0/$0.0)，回退到CLOB估价。`self_notify` PnL统一使用`_calculate_total_realized_pnl`（含partial_exits），消除与`close_position`的价格不一致。15个NO_BALANCE分支缓存`_exit_price`局部变量，防止双调用网络非确定性。
- **Base Rate Direction Calibration**: Outcome records distinguish `directional_won` (direction correct/wrong, only determinable at market expiry settlement) and `won` (PnL profit/loss). Base rate calibration prioritizes `directional_won`, skips `calibration_eligible=False` early-exit records, preventing early-exit profitable trades from polluting directional win rate statistics.
- **API-Based Settlement**: Expiry cleanup uses Polymarket API real outcome (not Binance price guess) to determine $1.00/$0.00.
- **Pending Order Reconciliation**: Monitor checks LIVE buy orders every cycle — detects fills via wallet balance, writes position to `positions.jsonl`, sends distinct TG notification (⏰ vs 🎯). Auto-cancels stale orders after `PENDING_ORDER_TTL` (default 120s). Reconcile  calls `record_bet_cost`（ghost fill entry cost），preventing daily_pnl under-deduction。
- **Restart State Recovery**: `_restore_recent_market_state()` loads recent (within 2 hours) positions and ended market records from `positions.jsonl` at startup, restoring `open_positions`/`recent_markets`/`restored_slugs`, preventing re-entry into markets with existing positions after restart.

### Infrastructure
- **On-Chain Price Unified Entry (`price_oracle.py`)**: `get_onchain_price(coin)` provides unified Chainlink / Pyth dual-source fallback, returns `(price, source)` for log tracing. Default Chainlink priority (same source as settlement), pass `prefer="pyth"` to switch. Replaces scattered direct calls to `chainlink_stream` + `get_current_price` across modules.
- **WebSocket TLS Certificate Fix (`ws_ssl.py`)**: All WSS connections (Binance / Chainlink RTDS / Polymarket Orderbook) use `get_websocket_sslopt()` with explicit CA certificate path. Solves WSS handshake failures on macOS Python where websocket-client cannot find system CA. Prefers certifi, supports `SSL_CERT_FILE` env override.
- **Chainlink RTDS Price Stream**: `ChainlinkStream` singleton in `polymarket_rtds.py` connects to Polymarket RTDS WebSocket (`wss://ws-live-data.polymarket.com`) for Chainlink on-chain settlement prices. This is the exact price Polymarket uses for settlement (UMA oracle references Chainlink feeds). Zero-latency WebSocket push (~1s update frequency), 15s staleness threshold. **Also used as primary price source for warmup sampling and decision-time `current_price` in `analyze_market()`** — ensures gap/ATR calculations are aligned with the same oracle as PTB and settlement. Binance WS serves as fallback when Chainlink is stale. `get_snapshot()` returns price + age_ms + source_ts + update_count for observability. `wait_for_update(with_details=True)` returns push metadata (coin/age_ms/wait_ms).
- **Pyth Network On-Chain Price Stream**: `PythPriceStream` singleton in `pyth_api.py` receives BTC/ETH prices from Pyth Hermes v2 SSE stream (`hermes.pyth.network`). On-chain oracle prices independent of Polymarket. REST fallback when SSE is stale. No API key required, free tier is production-grade. `get_snapshot()` returns price + age_ms + source_ts (publish_time) + update_count.
- **Binance WebSocket Price Stream**: Shared `BinancePriceStream` singleton in `binance_api.py` receives BTC/ETH trade prices via `wss://stream.binance.com` (~10ms push latency). `get_current_price()` reads from memory (0ms), auto-fallback to REST if WebSocket data is stale (>5s). Used for **K-line/ATR data** (Chainlink has no OHLCV). Serves as **fallback** for current price when Chainlink RTDS is stale. Serves as **final fallback** for position monitoring. Auto-reconnect with 2s backoff + 30s ping keepalive. `get_snapshot()` returns price + age_ms + event_ts + trade_ts + update_count.
- **Price Source Observability Log**: Each monitoring cycle prints `source observation` line showing CL/Pyth/BN three-source age + staleness + cross-source skew (ATR units) + orderbook spread + wake type. `get_current_crypto_price_debug()` returns complete source selection path and age_ms.
- **Oracle Stale Watch**: `evaluate_oracle_stale_watch()` Observes whether Chainlink (settlement source) lags behind Binance (fast market). When remaining <=60s, |CL-BN| >= 0.5 ATR with 3 consecutive confirmations triggers alert, prints recovery when deviation disappears. **Alert only, no trade intervention**, used for post-hoc analysis of settlement price lag causing stop-loss/direction judgment bias. Disable via `ENABLE_ORACLE_STALE_WATCH=0`.
- **Polymarket WebSocket Orderbook Stream**: `PolymarketOrderbookStream` singleton in `polymarket_ws.py` connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market` for real-time orderbook data. Handles `best_bid_ask`, `book`, and `price_change` events. FOK entry uses WS `best_ask` (0ms) instead of REST orderbook query (100-300ms), significantly reducing price staleness between read and order placement. Position monitor's `get_best_bid/get_best_ask/get_market_price` all use WS-first with REST fallback. Lazy connection: WS connects only on first `subscribe()` call (Polymarket requires immediate subscription after connect). Auto-subscribes token_ids during warmup, auto-unsubscribes on market cleanup. 9s PING heartbeat, auto-reconnect with exponential backoff (2s→4s→8s→...→30s, resets on stable connection). New `get_best_bid_ask_snapshot()`/`get_book_snapshot()` APIs return price data with `age_ms` for staleness-aware consumers.
- **CLOB Keepalive Non-Blocking**: `_keepalive` thread uses `_client_lock.acquire(blocking=False)`, skips heartbeat when order/book query is executing, preventing trade path blocking.
- **Warmup Token Pre-Cache**: During warmup phase, token_ids + SDK parameters (neg_risk/fee_rate/tick_size) are fetched and cached in parallel, saving ~2s at analysis time.
- **Orderbook Cache (2s TTL)**: `get_orderbook()` caches results for 2 seconds to eliminate duplicate HTTP requests within the same analysis cycle. Auto-invalidated after order placement.
- **Adaptive Warmup Sampling**: `WARMUP_SAMPLE_INTERVAL_EARLY` (default 3s) intervals before early window, accelerates to `WARMUP_SAMPLE_INTERVAL_LATE` (default 2s) after early window opens. Warmup starts at `WARMUP_START_SECONDS` (default 8). **v12: Warmup sampling switched to Binance WS (0ms memory read), discovers price movements 1-5s faster than Chainlink**，Auto fallback when Chainlink has no data. Entry decisions still use Chainlink (same source as settlement). Sample log shows source tag `(BN)`/`(CL)` for traceability.
- **7-Coin Support (v12)**: Supports BTC/ETH/BNB/SOL/HYPE/DOGE/XRP parallel trading. `coins.py` centrally manages Binance WS/Chainlink/Pyth feed configuration. PTB price validation floor lowered from $100 to $0.01, compatible with SOL($92)/HYPE($40)/DOGE($0.20)/XRP($2.5) low-price coins.
- **Execution Speed Optimization (v12)**: execute_bet removes redundant REST orderbook request (previously fetched twice), WS data timeout relaxed from 400ms to 2000ms (prefers WS real-time data over REST fallback), adds analysis-price fallback (uses analysis-stage CLOB price when WS/REST/last-trade all unavailable). Total execution latency reduced from ~1.5s to ~300ms (when WS available).
- **Strategy Timing Config (`get_strategy_config()`)**: All warmup/early/late window timing parameters and Bayesian thresholds unified via `.env` configuration, all hardcoded magic numbers extracted. Includes `EARLY_MIN_SAMPLES`, `LATE_MIN_UPDATES`, `LATE_LOW_CONF_THRESHOLD`, `LATE_GAP_CROSS_ALLOW_CONF`, `LATE_MATURE_SAMPLE_COUNT`, etc.
- **Trend Safety Valve**: Gap expanding/shrinking/crossing/oscillating → adjusts min discount
- **Network Circuit Breaker**: 5 consecutive API failures → 300s pause
- **PTB Acquisition (crypto-price API + Playwright fallback)**: Primary path uses Polymarket `crypto-price` REST API (`get_price_to_beat_api()`, constructs request params from slug timestamp, ~50ms), no browser process needed. Early-market API may not yet return values, `_fetch_ptb_api()` auto-retries (`PTB_API_RETRY_ATTEMPTS` times, interval `PTB_API_RETRY_INTERVAL` seconds), falls back to Playwright subprocess when all empty. 3 consecutive failures skip that coin. `get_current_markets()` and `position_monitor.get_ptb_from_slug()` switch in sync.
- **Volatility Re-Trigger**: Skipped markets monitored for large price moves — piggybacks on existing sampling loop (no extra API calls), re-enters analysis when volatility exceeds threshold
- **Outcome Learning Loop**: Every close records outcome → auto-calibrates base rates every 50 trades
- **Telegram Notifications**: Entry (🎯 direct / ⏰ pending fill), exits, settlements, errors, balance. Pending order expiry (⌛) also notified.
- **Overnight Position PnL Isolation (v13 fix)**: `pending_costs` stores `{cost, session_date}`, cross-day settlement position costs are not credited back to new trading day daily_pnl, preventing overnight positions from polluting daily risk limits. Old format auto-migrated for compatibility. v13: `_reset_daily_if_needed()` 同时清除旧格式numeric条目（`not isinstance(entry, dict)`），防止`_normalize_pending_costs`将其转为当日假条目导致成本累积。
- **Auto Redeem + PnL Matching**: Claim settled positions (configurable interval via `REDEEM_INTERVAL`), After settlement matches `positions.jsonl` to compute real cost basis and net profit, shows USDC balance + PnL stats after each round. Multi-wallet balance display (EOA + Proxy + Total). `find_redeemable()` uses data-api REST (`/positions?redeemable=true`) for zero-RPC position discovery.
- **Relayer Free-Gas Redeem (v12.9.10)**: Uses Polymarket Relayer API (`relayer-v2.polymarket.com/submit`) with official `py-builder-relayer-client` SDK for EIP-712 signed gasless transactions. Requires `RELAYER_API_KEY` + `RELAYER_API_KEY_ADDRESS` in `.env`. Automatic fallback to self-paid gas if Relayer fails. Normal redeem flow is fully RPC-free: data-api discovery → Relayer submit → confirmed.
- **Proxy Safe Redeem**: When `CLOB_SIGNATURE_TYPE=2` (GNOSIS_SAFE) and position is in proxy wallet, automatically executes on-chain redeem via 1/1 Safe `execTransaction` on behalf of proxy, no manual extraction to EOA needed. On-chain verification: Safe threshold=1 and EOA is owner. Parallel redeem also supports Safe routing (gas limit 600k).
- **Stale Mark Auto-Cleanup**: `cleanup_false_redeemed()` checks each startup via data-api (`redeemable=true` query) for conditions marked as redeemed but still redeemable, removes mark and re-redeems. Zero RPC calls.
- **Watchdog**: `watchdog_v3.sh` monitors all 3 services and auto-restarts on failure
- **systemd Management**: Auto-restart on crash, boot-start

## Components

| File | Role |
|------|------|
| `auto_bot_v3.py` | Main engine: market discovery, warmup, early/late betting, correlation control |
| `ai_trader/clob_client.py` | CLOB SDK wrapper: global singleton, GTC/FOK orders, balance, orderbook, warmup, fee_rate_bps |
| `ai_analyze_v2.py` | Decision engine: strict EV, Kelly sizing, Bayesian fusion, bet execution, pending order tracking |
| `ai_trader/ai_model_v2.py` | Scoring model: ATR deviation → token value estimation → discount |
| `ai_trader/base_rate.py` | Base Rate calibration: conservative priors + empirical learning |
| `scripts/validate_base_rate.py` | Base Rate calibration validator: ATR-band win rates vs priors |
| `ai_trader/bayesian_engine.py` | Bayesian sequential updater v2.1: incremental + state gate, sigmoid likelihood, soft reset |
| `ai_trader/lmsr_liquidity.py` | Orderbook liquidity: spread + depth + slippage → dynamic threshold |
| `ai_trader/fees.py` | Taker fee helpers: effective_fee_rate, estimate_buy_fill, estimate_sell_fill |
| `position_monitor.py` | EV-driven exit + 4-stage closing + pending order reconciliation + outcome recording |
| `auto_redeem_v2.py` | Auto claim settled positions (REST API + on-chain redeem) |
| `ai_trader/pyth_api.py` | Pyth Network on-chain price stream: SSE real-time + REST fallback (primary for position monitor) |
| `ai_trader/binance_api.py` | Binance market data: WebSocket real-time price stream + REST klines/stats (fallback for position monitor) |
| `ai_trader/polymarket_ws.py` | Polymarket Market Channel WebSocket: real-time orderbook/best_bid_ask stream |
| `ai_trader/indicators.py` | Technical indicators (EMA, RSI, ATR, Bollinger Bands) |
| `ai_trader/price_oracle.py` | On-chain price unified entry: Chainlink/Pyth dual-source fallback |
| `ai_trader/ws_ssl.py` | WebSocket TLS helpers: explicit CA certificate path |
| `ai_trader/fast_direction.py` | Fast direction: 3-signal fusion (Binance OFI + CL-BN spread + Polymarket OBI) for pre-Bayesian direction |
| `ai_trader/trade_logger.py` | Structured decision logger: entry/reprice/TP/SL/shadow events to `logs/trade_decisions.jsonl` |
| `ai_trader/polymarket_api.py` | Polymarket API + PTB HTML scraper |
| `ai_trader/playwright_ptb.py` | PTB extraction via headless Chromium |
| `trading_state.py` | State management: cooldown, daily PnL, win/loss tracking, overnight position isolation |
| `backtest.py` | PnL-based backtest report: reconstructs real trading performance from local logs |
| `backtest_accuracy.py` | Compatibility wrapper (redirects to backtest.py) |
| `watchdog_v3.sh` | Process watchdog: monitors and auto-restarts all services |

## Betting Conditions

All must be met (all configurable via `.env`):

| Condition | Late Window | Early Window | Env Key (supports `_UP`/`_DOWN` suffix) |
|-----------|-------------|--------------|---------|
| EV | > `MIN_EV` (0.10) | > `EARLY_MIN_EV` (0.08) | `MIN_EV` / `EARLY_MIN_EV` |
| Confidence | ≥ `MIN_CONFIDENCE` (0.70) | ≥ `EARLY_MIN_CONFIDENCE` (0.60) | `MIN_CONFIDENCE` / `EARLY_MIN_CONFIDENCE` |
| ATR Deviation | ≥ `MIN_ATR_DEVIATION` (1.4) | ≥ 1.0 | `MIN_ATR_DEVIATION` |
| Odds | < `MAX_BUY_PRICE` (0.90) | same | `MAX_BUY_PRICE` |
| 15-min Trend | Not counter-trend | same | — |
| Base Rate | < 0.55 → Kelly halved | same | — |
| Correlation | Same-direction → Kelly halved | same | — |

## Risk Controls

```
Layer 1 — Entry Filters
  ├─ EV > MIN_EV (default 10%, env configurable)
  ├─ Confidence ≥ MIN_CONFIDENCE (default 70%, env configurable)
  ├─ ATR deviation ≥ MIN_ATR_DEVIATION (default 1.4)
  ├─ Odds < MAX_BUY_PRICE (default 0.90, env configurable)
  ├─ 15-min K-line trend filter (anti counter-trend)
  ├─ Base Rate calibration (weak edge → Kelly halved)
  ├─ P2: Exit liquidity gate (bid_depth < 5 → skip entry)
  ├─ Empty book override (Gamma EV pass → bet with calibrated price)
  └─ Early window: lower thresholds (EV 8%, conf 60%, ATR 1.0)

Layer 2 — Position Sizing
  ├─ 1/4 Kelly (binary formula)
  ├─ Base Rate reduction (× 0.5 if < 0.55)
  ├─ Correlation reduction (× 0.5 if same-direction open)
  ├─ P3: Liquidity cap (≤ 50% of exit bid depth)
  ├─ Hard bounds: MIN_BET_SIZE-MAX_BET_SIZE shares (default 2-8)
  └─ Balance constraints (MAX_ENTRY_BALANCE_PCT of balance, default 20%)

Layer 3 — Position Management
  ├─ PTB Proximity Buffer: crypto near PTB → freeze direction, prevent noise stop-loss
  ├─ -25% hard stop: market sell when direction≠True (proximity: -50% extreme stop)
  ├─ Direction flip exit: True→False + consecutive confirmation → liquidation
  ├─ ATR 3-layer decision: ≥15% drop → dip-buy (ATR≥2) / hold (1-2) / stop-loss (ATR<1)
  ├─ v14 Ambush Phased TP/SL: rapid(-25%,3-confirm) → mid(-35%±noise) → late(-30%) + -70% hard stop
  ├─ v14 Ambush Breakeven Lock: profit ≥15% → trailing stop at entry+2%
  ├─ P0: Hyperbolic discounting profit-take (entry_price sell for guaranteed fill)
  ├─ P1: Opposite token hedge (bid < $0.05 → buy opposite for $1 pair)
  ├─ Maker Exit: stop-loss first tries GTD maker sell (0 fee), timeout → FAK fallback
  ├─ Market-price immediate stop-loss (cancel first, then ladder sell)
  ├─ Wide spread detection (prevents false loss signals)
  ├─ Post-close safety (no trades after market ends)
  ├─ 3-stage graduated closing (Stage 2/3/4 for remaining edge cases)
  ├─ API-based settlement (real outcome, not price guess)
  └─ Pending order reconciliation (LIVE → detect fill → record position → notify)

Layer 4 — System Protection
  ├─ Max open positions: 2 (configurable)
  ├─ Daily loss limit: $10 (thread-safe, pre-deducted cost on entry, settled on close)
  ├─ Circuit breaker: 5 failures → 300s pause
  ├─ Loss cooldown: 3 periods after failed bet
  └─ Min balance check: $5
```

## CLOB SDK Integration

This bot uses [py-clob-client](https://github.com/Polymarket/py-clob-client) Python SDK for all trading operations (order placement, cancellation, balance queries, orderbook data). SDK direct calls achieve **<50ms latency** vs 2-8s with CLI subprocess.

```python
# SDK initialization (once at startup, with TLS + signing library + cache warmup)
from ai_trader import clob_client
clob_client.init_client()

# Order placement (GTC limit for entry, FOK for exit)
clob_client.place_order(token_id, BUY, price, size)      # GTC entry ~26ms
clob_client.place_fok_order(token_id, SELL, price, size)  # FOK exit ~26ms

# Market data
clob_client.get_orderbook(token_id)
clob_client.get_midpoint(token_id)
clob_client.get_last_trade_price(token_id)

# Balance & Allowance
clob_client.get_balance()                  # USDC collateral
clob_client.get_token_balance(token_id)    # Conditional token balance
clob_client.update_token_allowance(token_id) # Refresh token sell authorization

# Fees & Metadata
clob_client.get_fee_rate_bps(token_id)     # Token taker fee (bps), 0 for fee-free
clob_client.get_token_metadata(token_id)   # Cached token metadata dict

# Warmup & Cache
clob_client.precache_tokens([t1, t2])      # Parallel neg_risk/fee_rate/tick_size cache
clob_client.invalidate_book_cache(token_id) # Clear orderbook cache after trades

# Cancel
clob_client.cancel_all(token_id)
```

All components now use SDK/REST API directly — no Polymarket CLI dependency.

## Setup

### Prerequisites

- Python 3.10+
- **Minimum server specs**: 2-core 2GB (2 coins serial PTB) / 4-core 4GB (3 coins parallel PTB)
- [py-clob-client](https://github.com/Polymarket/py-clob-client) (`pip install py-clob-client`)
- [websocket-client](https://pypi.org/project/websocket-client/) (`pip install websocket-client`) — Binance WebSocket price stream
- Chromium browser + system dependencies (optional, Playwright PTB fallback only — primary PTB uses crypto-price API)

### Installation

**One-click deploy (recommended):**

```bash
git clone https://github.com/youruser/polymarket-trading-bot.git
cd polymarket-trading-bot
bash setup.sh
nano .env  # Fill in wallet address and Telegram token
```

`setup.sh` auto-completes: Python dependencies -> Playwright Chromium + system libraries -> log directory -> .env template -> validation test.

**Manual installation:**

```bash
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium  # Install Chromium system deps (libatk, libglib, etc.)
cp .env.example .env
```

### Environment Variables

See `.env.example` for all configurable parameters:

| Variable | Required | Description |
|----------|----------|-------------|
| `EOA_WALLET` | Yes | EOA wallet address for signing |
| `PROXY_WALLET` | Yes | Polymarket proxy wallet (holds positions, see Settings on polymarket.com) |
| `CLOB_SIGNATURE_TYPE` | No | `0` = EOA direct, `1` = POLY_PROXY (Magic/email), `2` = GNOSIS_SAFE (browser wallet / most Polymarket.com proxy wallets) |
| `PRIVATE_KEY` | Yes | Private key for SDK signing + on-chain settlement |
| `POLYGON_RPC_URL` | No | Polygon RPC endpoint (default: public, only used for self-pay gas fallback) |
| `RELAYER_API_KEY` | No | Polymarket Relayer API key for gasless redeem (get from polymarket.com Settings → Relayer API Keys) |
| `RELAYER_API_KEY_ADDRESS` | No | EOA address associated with Relayer key (defaults to `EOA_WALLET`) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | No | Telegram chat ID for notifications |
| `REDEEM_INTERVAL` | No | Auto redeem polling interval in seconds (default: 600) |
| `MAX_DAILY_LOSS` | No | Daily loss limit in USD (default: 10) |
| `MAX_OPEN_POSITIONS` | No | Max concurrent positions (default: 2) |
| `MIN_BET_SIZE` | No | Min shares per bet (default: 5) |
| `MAX_BET_SIZE` | No | Max shares per bet — raise to scale with balance (default: 10) |
| `MAX_ENTRY_BALANCE_PCT` | No | Max fraction of wallet balance allowed for a single entry before fee/min-size checks (default: 0.20) |
| `SIZE_STEP` | No | Entry order size rounding step for gross shares (default: 0.1) |
| `MIN_BALANCE` | No | Min balance to place bets (default: 5) |
| `MAX_BUY_PRICE` | No | Max buy price for entry (default: 0.90) |
| `MIN_EV` | No | Min EV for late window (default: 0.10) |
| `MIN_CONFIDENCE` | No | Min confidence for late window (default: 0.70) |
| `MIN_ATR_DEVIATION` | No | Min ATR deviation for entry (default: 1.4) |
| `P_WIN_CAP` | No | Max p_win cap (default: 0.92) |
| `WARMUP_START_SECONDS` | No | Seconds after market start to begin warmup sampling (default: 20) |
| `WARMUP_SAMPLE_INTERVAL_EARLY` | No | Warmup sample interval before the early bet window, in seconds (default: 5) |
| `WARMUP_SAMPLE_INTERVAL_LATE` | No | Warmup sample interval after the early bet window opens, in seconds (default: 3) |
| `EARLY_BET_START` | No | Early bet window start in seconds (default: 90) |
| `EARLY_BET_END` | No | Early bet window end in seconds (default: 95) |
| `EARLY_MIN_SAMPLES` | No | Minimum warmup sample count required before early-window bets are allowed (default: 4) |
| `EARLY_MIN_EV` | No | Min EV for early window (default: 0.08) |
| `EARLY_MIN_CONFIDENCE` | No | Min confidence for early window (default: 0.60) |
| `LATE_BET_START` | No | Late bet window start (default: 100) |
| `LATE_BET_END` | No | Late bet window end (default: 160) |
| `LATE_MIN_UPDATES` | No | Minimum Bayesian update count required before late-window Bayesian gating applies (default: 3) |
| `LATE_LOW_CONF_THRESHOLD` | No | Late-window confidence below this is treated as low-confidence skip (default: 0.15) |
| `LATE_GAP_CROSS_ALLOW_CONF` | No | When gap crosses PTB, only allow late-window bets if confidence is at least this value (default: 0.60) |
| `LATE_MATURE_SAMPLE_COUNT` | No | Mature-sample threshold used to stop repeatedly scanning low-confidence late setups (default: 8) |
| `ENTRY_REANALYZE_INTERVAL` | No | Cooldown in seconds before early/late entry windows may re-run analysis after a failed entry attempt (default: 20) |
| `LATE_REANALYZE_INTERVAL` | No | Legacy alias for `ENTRY_REANALYZE_INTERVAL`; used only if the new env is unset |
| `SLIPPAGE` | No | Empty book fallback slippage for last-trade-price ± (default: 0.02) |
| `P0_BASE_PROFIT` | No | P0 take-profit base threshold (default: 0.20) |
| `PROFIT_THRESHOLD` | No | General profit threshold (default: 0.15) |
| `P0_HYPERBOLIC_K` | No | Hyperbolic discounting coefficient (default: 0.15) |
| `PENDING_ORDER_TTL` | No | Max wait for LIVE orders before cancel (default: 120s) |
| `EARLY_EXIT_RATIO` | No | Probability of early exit for spread cost calc (default: 0.3) |
| `PENDING_MIN_FILL` | No | Min filled size to record position (default: 0.5) |
| `PRICE_DROP_TRIGGER` | No | Token drop % to trigger ATR decision (default: 0.15 = 15%) |
| `PRICE_DROP_HARD_STOP` | No | Unconditional hard stop-loss % (default: 0.25 = 25%) |
| `ATR_SAFE_THRESHOLD` | No | ATR deviation ≥ this = safe zone, dip-buy (default: 2.0) |
| `ATR_DANGER_THRESHOLD` | No | ATR deviation < this = danger zone, stop-loss (default: 1.0) |
| `DIP_BUY_SIZE_RATIO` | No | Dip-buy amount as ratio of original position (default: 0.50) |
| `DIP_BUY_MIN_REMAINING` | No | Min remaining seconds to allow dip-buy (default: 60) |
| `PTB_PROXIMITY_ATR` | No | Proximity zone width in ATR units, time-decayed (default: 0.7) |
| `PTB_PROXIMITY_EXTREME_STOP` | No | Extreme safety valve: token drop % to force exit in proximity zone (default: 0.50) |
| `REANALYZE_ATR_MULT` | No | Price move in ATR multiples to retrigger skipped market (default: 1.5) |
| `REANALYZE_COOLDOWN` | No | Min seconds before retrigger allowed (default: 15) |
| `MAX_REANALYZE` | No | Max retrigger attempts per market (default: 1) |
| `MAX_TRADE_RETRIES` | No | Max FOK retry attempts in late window (default: 3) |
| `ATR_DECAY_CONFIRMATIONS` | No | ATR decay stop consecutive confirmation rounds (default: 3) |
| `ENABLE_DIRECTION_DOWNGRADE` | No | Direction downgrade toggle, 0=off 1=on (default: 0) |
| `ATR_DOWNGRADE_THRESHOLD` | No | Direction downgrade ATR threshold, only effective when enabled (default: 0.15) |
| `SNIPER_SHRINK_FULL_AT` | No | Full sniper shrinkage when remaining<=this, proportionally decays above (default: 120) |
| `SNIPER_GAP_DECAY_RATIO` | No | Skip sniper when current gap < peak x this ratio (momentum decay) (default: 0.75) |
| `SNIPER_WS_MAX_AGE` | No | Block sniper FOK when WS BBA age exceeds this ms (default: 5000) |
| `SNIPER_AMBUSH` | No | Ambush limit order toggle, 1=on 0=off (default: 0) |
| `SNIPER_AMBUSH_PRICE` | No | Ambush limit price (default: 0.55) |
| `SNIPER_AMBUSH_SIZE` | No | Ambush amount per direction USD (default: 3.0) |
| `SNIPER_AMBUSH_MIN_ATR` | No | Min ATR to place ambush (default: 0.5) |
| `SNIPER_AMBUSH_MIN_CONF` | No | Min confidence to place ambush (default: 0.25) |
| `SNIPER_AMBUSH_FILL_MIN_CONF` | No | Min confidence at fill time to keep position; below this immediately sells (default: 0.15) |
| `SNIPER_AMBUSH_END` | No | Ambush window end, cancel when remaining < this (default: 30) |
| `EV_SKEW_BLOCK_ATR` | No | Suspend EV stop-loss when \|CL-BN\| exceeds this ATR multiple (default: 1.0) |

### Running

```bash
# Run directly
python auto_bot_v3.py          # Betting engine
python position_monitor.py     # Position monitor (separate terminal)
python auto_redeem_v2.py       # Auto redemption (separate terminal)

# Or via systemd (recommended for production)
systemctl start polymarket-bot polymarket-monitor polymarket-redeem
```

### Useful Commands

```bash
# Check status
systemctl status polymarket-bot polymarket-monitor polymarket-redeem

# View logs
journalctl -u polymarket-bot -f
tail -f logs/polymarket-bot.log

# View base rate calibration
cat logs/base_rates.json

# View trade outcomes
tail -20 logs/outcomes.jsonl | python -m json.tool
```

## Data Files

| File | Purpose |
|------|---------|
| `logs/positions.jsonl` | Open/closed positions with enriched entry details |
| `logs/bets.jsonl` | Bet execution records |
| `logs/decisions_v2.jsonl` | Analysis decisions (BET/SKIP with full details) |
| `logs/closed_positions.jsonl` | Closed positions with PnL |
| `logs/outcomes.jsonl` | Trade outcomes for base rate learning |
| `logs/base_rates.json` | Empirical win rates by ATR band |
| `logs/trading_state.json` | State machine (cooldown, daily PnL) |
| `logs/pre_orders.json` | Pre-order state persistence |
| `logs/pending_orders.jsonl` | LIVE (unfilled) buy orders awaiting reconciliation |
| `logs/polymarket-bot.log` | Application log |
| `logs/monitor_YYYY-MM-DD.log` | Monitor daily log (auto-rotated) |

## Version History

- **v13.0.0**: Smart Ambush v2.0 + PnL修复 — **5模块伏击升级**: (1) Avellaneda-Stoikov动态EDGE：`base × √(remaining/300) + min_edge`，time-decay + GLFT波动率自适应 `clamp(atr/avg_atr, 0.5, 2.0)`。(2) 双向追价守卫：后退追价仅在旧单edge薄于`EDGE_MIN`时允许，防止有利限价单被错误回撤。(3) OFI逆向选择过滤：top-5订单簿深度倾斜 > `OFI_THRESHOLD` 时跳过挂单。(4) 毒性成交检测：成交发生在下单后2s内 → 提高`FILL_MIN_CONF`到0.35，迫使低信心毒性成交立即卖出。(5) 波动率自适应：高ATR放大EDGE，低ATR收紧EDGE。**PnL计算修复**: (1) `_resolve_or_estimate_exit_price` — NO_BALANCE路径优先查结算价($1/$0)而非CLOB估价，29处call site统一替换。(2) `self_notify` PnL统一使用 `_calculate_total_realized_pnl`（含partial_exits）。(3) 15个NO_BALANCE分支缓存exit_price局部变量，消除双调用网络非确定性。(4) `_reset_daily_if_needed` 同时清除旧格式numeric pending_costs条目，防止跨日成本累积。新增环境变量: `SNIPER_AMBUSH_EDGE_MIN`, `SNIPER_AMBUSH_AVG_ATR`, `SNIPER_AMBUSH_OFI_THRESHOLD`。新增回归测试: `TestM2BackwardRepriceGuard`, `TestM4ToxicFillAfterReprice`。

- **v12.9.16**: 伏击止盈单 + FOK SELL PnL修复 + 心跳安全网 — **伏击止盈GTD**: 伏击成交后自动挂GTD SELL限价单锁利，`AMBUSH_TP_RATIO=0.5`（取利润空间一半），到期前`AMBUSH_TP_DEADLINE`秒截止。**FOK SELL PnL**: FAK部分成交用taking金额算sold_count，不依赖链上余额延迟。**抄底均价**: 修复用当前持有份额成本而非累计总成本。新增环境变量: `AMBUSH_TP_ENABLED`, `AMBUSH_TP_RATIO`, `AMBUSH_TP_DEADLINE`。

- **v12.9.12**: FAK部分成交PnL丢失修复 + 抄底均价修复 + 伏击CLOB感知定价 — **FAK PnL**: 部分成交的已售份额正确记录partial_pnl。**抄底均价**: 修复累计总成本导致的均价爆炸。**CLOB感知定价**: 伏击价格参考市场best_ask，`ASK_OFFSET`从`MAX`到`MIN`时间衰减。

- **v12.9.9**: Reverse entry on stop-loss + live ATR + orphan dedup fix — **Reverse entry**: When EV/hard stop-loss fires and direction has clearly reversed, automatically buys opposite token (FOK). Requires: reversed Bayesian direction, BN confirmation, diff_atr >= 0.5, remaining > 60s, max 1 reverse per slug. `STOP_LOSS_REVERSE_ENTRY=1`. **Live ATR**: ATR now dynamically updated from sniper updater samples (`max(static_atr, live_atr)`) — catches markets that start calm but become volatile, preventing MIN_ATR_ABS from blocking strong signals. **Orphan scanner dedup fix**: Memory-based `_orphan_known_tokens` set prevents duplicate writes even after monitor closes and reopens positions. New env: `STOP_LOSS_REVERSE_ENTRY`, `REVERSE_MIN_CONF`, `REVERSE_MIN_ATR`.

- **v12.9.8**: User WS fill detection + FAK stop-loss + orphan token scanner + ATR abs filter + GTD orders — **User Channel WS**: Real-time order/trade event stream (`wss://ws-subscriptions-clob.polymarket.com/ws/user`) for instant fill detection (~0ms vs 500ms polling). 500ms polling retained as fallback. **FAK stop-loss**: Fill-And-Kill replaces FOK for `market_sell_immediate()` — partial fills accepted, remainder sold in next round. Eliminates consecutive FOK killed delays. `STOP_LOSS_USE_FAK=1`. **Orphan token scanner**: Monitor scans data-api `/positions` every 30s, auto-detects tokens not in positions.jsonl, writes them for monitor management. Solves reprice residual tokens going unmanaged. **ATR absolute filter**: `SNIPER_AMBUSH_MIN_ATR_ABS=30` blocks ambush in low-volatility markets (ATR<$30 was 29% win rate, -$15.38). **GTD orders**: Ambush uses GTD (Good-Til-Date) instead of GTC — initial order expires at market_end-30s, reprice orders expire in `SNIPER_AMBUSH_GTD_REPRICE_SEC` seconds. Zero residual risk from expired orders. **3-layer independent timing**: Balance check (500ms), reprice (200ms), and WS push (~0ms) run on independent timers. **Reprice instant-match fix**: When reprice order matches immediately, cancel_all + return to prevent further repricing. **Full balance sell**: Direction-fail and direction-pass paths both query actual on-chain balance, selling/positioning all tokens including reprice residuals. New env: `SNIPER_AMBUSH_MIN_ATR_ABS`, `SNIPER_AMBUSH_GTD_REPRICE_SEC`, `SNIPER_AMBUSH_BAL_INTERVAL`, `SNIPER_AMBUSH_REPRICE_INTERVAL`, `STOP_LOSS_USE_FAK`. New file: `ai_trader/user_ws.py`.

- **v12.9.6**: Dynamic ambush pricing + hold-to-settlement + batch cancel + FOK pre-clear — **Dynamic pricing (v12.9.5)**: `ambush_price = p_win - edge(0.045)`, clamped [0.45, 0.65]. Strong signals bid higher ($0.70), weak signals bid lower ($0.53). Replaces fixed $0.55. **Hold to settlement**: Ambush positions (`ambush=True`) skip P0/Trailing TP when profitable, hold for $1.00 settlement payout. Production data: 3 winning trades exited at $0.77-0.86, leaving $6.80 on table. **Dynamic repricing**: Recalculates optimal price every 2s, reprices when delta >= $0.02. Max `SNIPER_AMBUSH_REPRICE_MAX` reprices per market. **Batch cancel fix**: `cancel_orders_batch()` uses `DELETE /orders` API (single request for up to 3000 orders), replacing per-order cancel loops that left residual orders. **FOK pre-clear**: `cancel_all(sniper_token)` before FOK entry clears any ambush residuals. **Entry tightened**: `AMBUSH_MIN_ATR` 0.5->0.8, `MIN_CONF` 0.25->0.35, `FILL_MIN_CONF` 0.20->0.25 (ATR<0.7 was 0/4 win rate). **Sell PnL fix**: FOK sell making/taking parsed as USDC amount not unit price. New env: `SNIPER_AMBUSH_MIN_PRICE`, `SNIPER_AMBUSH_MAX_PRICE`, `SNIPER_AMBUSH_EDGE`, `SNIPER_AMBUSH_REPRICE_MAX`.

- **v12.9.4**: Post-fill direction validation + ambush fill size cap — **Post-fill direction check**: When ambush fill is detected, re-reads Bayesian direction and confidence before creating position. If direction flipped or confidence < `SNIPER_AMBUSH_FILL_MIN_CONF` (default 25%), immediately FOK sells instead of holding. Production backtest on 8 trades: all 4 losers had direction flipped or conf<15% at fill time, all 3 winners had conf>=25% — would save $4.26 with zero impact on wins. **Fill size cap**: Detected fill capped at ordered_size x 1.05, preventing balance contamination from other token sources (production case: ordered 5.74 but detected 23 shares). New env: `SNIPER_AMBUSH_FILL_MIN_CONF`.

- **v12.9.3**: Sniper safety guards + directional ambush + CL-BN skew protection + stop-loss optimization — **Sniper 4-layer safety**: (1) WS health check (age>5s blocks FOK, allows ambush); (2) Gap momentum check (gap shrunk >25% = skip); (3) Time-decayed shrinkage (remaining>120s = more conservative p_win); (4) Pre-FOK revalidation (re-read Chainlink before order). **Double-entry protection**: `analyze_and_trade()` entry + pre-execute_bet dual check on positions/_sniper_processing. **Directional ambush (v12.9.1)**: When sniper detects confirmed direction but ask too high or WS stale, places GTC limit on confirmed direction with Kelly sizing. Anti-loop: no placement when remaining<=35s, 30s cooldown after cancel. Market cleanup auto-cancels residual GTC. **CL-BN skew protection (v12.9.3)**: When |CL-BN| >= 1 ATR, EV stop-loss suspended (CL direction unreliable). BN direction confirmation: when direction correct + BN gap >= 0.3 ATR, EV exit blocked. Reads directly from price snapshots (no dependency on ENABLE_ORACLE_STALE_WATCH). **Hard stop last-60s relaxation**: threshold -40% -> -70% when remaining<=60s. **Main-thread inline sniper disabled** (lacks safety checks). Removed dead config `SNIPER_PROTECTION_SECONDS`. New env: `SNIPER_SHRINK_FULL_AT`, `SNIPER_GAP_DECAY_RATIO`, `SNIPER_WS_MAX_AGE`, `SNIPER_AMBUSH`(+PRICE/SIZE/MIN_ATR/MIN_CONF/END), `EV_SKEW_BLOCK_ATR`.

- **v10.0**: PTB parallel fetch + per-coin failure counting — PTB fetch for multiple coins now runs in parallel using `ThreadPoolExecutor` (one Chromium subprocess per coin, all launched simultaneously). On 4H4G servers, 3 coins fetch in ~10s instead of ~30s serial. PTB requests are collected during warmup phase (2-40s) and batch-launched before analysis. `_playwright_failures` changed from global integer to per-coin dict — BTC timeout no longer blocks ETH/BNB. Counter resets on each new 5-minute market cycle. `analyze_and_trade` fallback PTB fetch uses `_playwright_lock` to serialize (prevents parallel threads from launching extra Chromium). Requires 4GB+ RAM for 3 concurrent Chromium processes.

- **v9.9**: Multi-coin config + BNB support + Chainlink stop-loss optimization — **Multi-coin support**: New `ai_trader/coins.py` centralized coin configuration. All hardcoded BTC/ETH references replaced with dynamic lookups. Add/remove coins via `ENABLED_COINS` env var (e.g., `BTC,ETH,BNB`). Each coin auto-configures: Binance WS stream, Chainlink RTDS symbol, Pyth feed ID, Polymarket slug prefix. BNB (Binance Coin) added as third supported coin (`bnb-updown-5m` markets, Chainlink `bnb/usd` settlement feed). Files updated: `polymarket_api.py` (market discovery), `binance_api.py` (WS streams + symbol parsing), `pyth_api.py` (feed IDs), `polymarket_rtds.py` (Chainlink symbols), `position_monitor.py` (slug→coin + Binance symbol), `auto_bot_v3.py` (slug→coin). P3 correlation control applies to all enabled coins (same-direction positions get Kelly halved). **Chainlink stop-loss optimization**: (1) ATR decay stop — exits when ATR drops below 0.5 AND below 30% of entry ATR, before token price collapses (saves ~$2-3 per losing trade). (2) ATR acceleration stop — 3 consecutive ATR readings declining with current ATR<0.5 + token loss>10% triggers immediate exit. (3) Direction-correct ATR stop — ATR<0.5 + token loss>20% now triggers stop-loss even with correct direction (50:50 gamble not worth holding). (4) Proximity fast release — token loss exceeding hard stop line (-25%) immediately breaks proximity protection without waiting for streak=4. New env: `ENABLED_COINS`, `ATR_DECAY_EXIT_THRESHOLD`, `ATR_DECAY_RATIO`, `ATR_DIRECTION_CORRECT_STOP`. New file: `ai_trader/coins.py`.

- **v9.8**: RTDS Chainlink settlement price + MAX_OPEN_POSITIONS race fix — Position monitor now uses Polymarket RTDS WebSocket (`wss://ws-live-data.polymarket.com`) Chainlink on-chain feed as primary price source, replacing Pyth. Chainlink is the exact price Polymarket uses for settlement (UMA oracle references Chainlink feeds), eliminating any price discrepancy in win/loss judgment. Zero-latency WebSocket push vs Pyth SSE (~1s). Price source priority: Chainlink(CL) → Pyth SSE → Pyth REST → Binance WS → Binance REST. Monitor status line shows `CL`/`Pyth`/`WS`/`REST` tag. Pyth and Binance fully retained as fallback. New `ai_trader/polymarket_rtds.py`: singleton WebSocket client subscribing to `crypto_prices_chainlink` topic, 5s PING heartbeat, exponential backoff reconnection, 5s staleness threshold. MAX_OPEN_POSITIONS race condition fix: BTC/ETH parallel `analyze_and_trade` (ThreadPoolExecutor) could both pass the position count check simultaneously when `open_count=0`, opening 2 positions despite `MAX_OPEN_POSITIONS=1`. Fixed with `threading.Lock` + `_pending_bets` reservation counter — check atomically reads `open_count + pending`, reserves a slot before releasing the lock, and releases in `finally` block after bet completes. New file: `ai_trader/polymarket_rtds.py`.

- **v9.7**: PnL tracking fixes + late window re-analysis — **PnL tracking fixes**: (1) Ghost trade price estimation — when FOK returns ERROR but on-chain balance is zero (ghost fill), exit price now estimated via LTP/best_bid instead of $0.01 floor price, preventing massive false losses ($4-5 per ghost trade). (2) Cross-process file lock — `trading_state.json` now protected by `fcntl.flock` (process-level) + `threading.Lock` (thread-level), fixing race conditions between `auto_bot_v3` and `position_monitor` that could corrupt `daily_pnl`/`pending_costs`/win-loss counts. `record_bet_result` now runs under the same lock. (3) Partial exit PnL tracking — Stage 2 batch sell partial fills now call `record_partial_pnl()` for mini-settlement, adding back the sold shares' pre-deducted cost + actual PnL to `daily_pnl` immediately (previously lost until final close). (4) NO_BALANCE exit price fallback — 9 code paths that used `current_price or 0` now use multi-source fallback (current_price → LTP → best_bid → entry_price), preventing false full-loss recording when tokens were already sold. (5) Dip-buy cost pre-deduction — `execute_dip_buy` now calls `record_bet_cost` after successful buy; `pending_costs` accumulates (+=) instead of overwriting, correctly tracking total cost basis for dip-buy positions. **Entry-stage re-analysis**: Early-window failures no longer have to rely only on volatility retriggers; early and late entry windows now share one cooldown for periodic re-checks, `ENTRY_REANALYZE_INTERVAL` (with `LATE_REANALYZE_INTERVAL` kept as a compatibility alias). Late window pre-check rejections (conf<0.15, gap cross) still wait for cooldown and re-evaluate with updated Bayesian posterior. New env: `ENTRY_REANALYZE_INTERVAL`, `MAX_TRADE_RETRIES` (legacy alias: `LATE_REANALYZE_INTERVAL`).

- **v9.6**: Pyth on-chain price source + FOK late retry + WS exponential backoff — Position monitor now uses Pyth Network on-chain oracle prices (Hermes v2 SSE stream) as primary price source, replacing Binance. Pyth prices are closer to Polymarket settlement source (UMA oracle references on-chain feeds), reducing price discrepancy in win/loss judgment. Price source priority: Pyth SSE → Pyth REST → Binance WS → Binance REST. Monitor status line shows `Pyth`/`WS`/`REST` tag. K-line data (ATR/OHLCV) remains Binance (Pyth has no candle data). Late window FOK retry: previously, FOK/liquidity failures in the late window (100-160s) permanently blocked re-analysis (`self.analyzed.add` was unconditional before trade execution). Now only marks as analyzed on success or non-retryable failure (AI says no bet, missing PTB/token). FOK/liquidity failures allow retry on next loop iteration (~7s), up to `MAX_TRADE_RETRIES` (default 3). Polymarket WS reconnection now uses exponential backoff (2s→4s→8s→...→30s) instead of flat 2s, preventing reconnection storms that trigger server-side rate limiting. Backoff resets on stable connection (>10s). Reduced reconnect log noise. New files: `ai_trader/pyth_api.py`. New env: `MAX_TRADE_RETRIES`.

- **v9.5**: Volatility-triggered re-analysis — Skipped markets (low confidence, weak gap cross) are now tracked and monitored for volatility. When crypto price moves ≥1.5 ATR (`REANALYZE_ATR_MULT`) from the skip price, the market re-enters the late window analysis pipeline with its accumulated Bayesian state. Piggybacks on existing warmup sampling loop (zero extra API calls). Guards: cooldown timer (`REANALYZE_COOLDOWN`, default 15s), max 1 retrigger per market (`MAX_REANALYZE`), no retrigger if position already open, `is_reanalyze` flag prevents infinite skip→retrigger loops. Skipped markets cleaned up on position entry and market cleanup. New env: `REANALYZE_ATR_MULT`, `REANALYZE_COOLDOWN`, `MAX_REANALYZE`.

- **v9.4**: MAX_DAILY_LOSS risk control fix + PTB fallback removal — Daily loss limit now thread-safe with `threading.Lock`, checked at the top of `analyze_and_trade()` (before any analysis/betting), preventing parallel BTC/ETH threads from both passing the check simultaneously. Bet cost pre-deducted to `daily_pnl` on entry (worst-case full loss), settled with actual PnL on position close via `settle_bet_cost()`. Dip-buy (`execute_dip_buy`) also checks daily loss limit before adding. PTB fallback layers (HTML scraper + Gamma API) removed — they returned wrong market's PTB on Playwright timeout (e.g., $73,934 from a different 5-min window instead of $71,193). Now Playwright-only: fail → skip market, no false PTB.

- **v9.3**: PTB Proximity Buffer + consecutive confirmation stop-loss — When crypto price is near PTB (within configurable ATR threshold), direction signal is unreliable noise. New proximity buffer freezes `direction_correct = True`, suppressing all direction-based stop-losses (hard stop, direction flip, ATR danger, full-time wrong). Threshold decays with time: 0.7 ATR (first 2min) → 0.3 ATR (mid) → 0.15 ATR (last 1min). Extreme safety valve at -50% token drop. Direction flip (True→False) now requires consecutive confirmation rounds (2 for ATR<1.5, 1 for ATR≥1.5) instead of instant liquidation — prevents single-poll price fluctuation from triggering premature exits. Full-time direction-wrong stop-loss also requires streak confirmation (3 rounds for ATR<1.0, 2 for ATR<1.5, 1 for larger deviations). Bug fix: `prev_direction_correct` now preserved during pending confirmation to prevent #2 trigger from becoming dead code. New env: `PTB_PROXIMITY_ATR` (default 0.7), `PTB_PROXIMITY_EXTREME_STOP` (default 0.50).

- **v9.2**: Polymarket WebSocket real-time orderbook — FOK entry now uses WS `best_ask` (0ms delay) instead of REST orderbook query (100-300ms), eliminating price staleness between read and order placement. New `polymarket_ws.py` singleton connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`, handles `best_bid_ask`/`book`/`price_change` events. Position monitor's `get_best_bid/get_best_ask/get_market_price` all WS-first with REST fallback. Lazy connection design: WS only connects on first `subscribe()` (Polymarket requires immediate subscription after connect, otherwise disconnects). Auto-subscribes token_ids during warmup, auto-unsubscribes on market cleanup. 9s PING heartbeat, auto-reconnect. FOK retry logic (+1tick/+2tick/80% size) now operates on fresher prices, expected to significantly improve fill rate.

- **v9.1**: Binance WebSocket real-time price stream + 3 bug fixes — Price data for warmup sampling, analysis, and position monitoring now uses WebSocket (`wss://stream.binance.com` @trade stream, ~10ms push) instead of REST polling (100-300ms per call). Shared `BinancePriceStream` singleton in `binance_api.py` with auto-reconnect and REST fallback. Eliminates ~215 REST calls/cycle. Bug fixes: (1) ATR<1.0 stop-loss now requires `direction_correct=False` — direction correct + ATR dip no longer triggers false stop-loss. (2) P0 take-profit consecutive failure cap: after 3 failed sell attempts with direction correct, stops retrying and waits for $1.00 settlement (orderbook has no buyers near expiry). (3) `_check_and_adjust_size` syncs `positions.jsonl` when on-chain balance < recorded size, preventing repeated size mismatch warnings. Monitor status line now shows crypto price and data source (WS/REST).

- **v9.0**: ATR 3-layer stop-loss redesign — Replaces old Token crash circuit breaker (60% drop) and late-loss circuit breaker (30%+wrong direction) with ATR-deviation-based decision matrix. When token drops ≥15% from entry: ATR≥2.0 (crypto far from PTB) → dip-buy 50% of original position at best_ask via FOK (max 1 per position, requires direction correct + >60s remaining); ATR 1.0-2.0 → hold and monitor; ATR<1.0 (crypto near PTB) → immediate stop-loss at best_bid. New -25% hard stop: unconditional market sell regardless of ATR or direction. New direction flip emergency exit: when `direction_correct` transitions True→False, immediately liquidates all holdings including dip-bought shares. All thresholds configurable via env (`PRICE_DROP_TRIGGER`, `PRICE_DROP_HARD_STOP`, `ATR_SAFE_THRESHOLD`, `ATR_DANGER_THRESHOLD`, `DIP_BUY_SIZE_RATIO`, `DIP_BUY_MIN_REMAINING`). Preserves P0 hyperbolic take-profit, early tolerance window, P1 hedge, and Stage 2/3/4 graduated exit.

- **v8.4**: Fast stop-loss + FOK entry + late-loss circuit breaker — HTTP timeout reduced from 5s to 3s (`FOK_TIMEOUT` env, saves 2s per failed order during stop-loss). Entry orders switched from GTC limit to FOK (fill-or-kill): no more pending/LIVE orders, either instant fill or instant fail. FOK orders now auto-retry on HTTP 425. `sell_position` adds ghost fill detection after FOK timeout (checks on-chain balance before retry). All 7 NO_BALANCE code paths now send Telegram notification (previously silently closed). New late-loss circuit breaker: when remaining ≤120s + direction wrong + loss >30% (`LATE_LOSS_THRESHOLD` env) → force immediate market sell (early stage skipped to avoid false triggers from thin liquidity). Token crash threshold adjusted: 50%→40% drop, absolute price check now relative to entry (50% of entry_price instead of fixed $0.10).
- **v8.3**: Balance unit fix + ghost fill detection fix — `get_balance()` and `get_token_balance()` now divide API response by 1e6 (Polymarket returns atomic USDC/token units with 6 decimals). Previously raw balance ~57M fed into Kelly produced `$1.7M dollar_amount` → always clamped to MAX_BET. Kelly sizing now works correctly with real dollar balance. Stop-loss ghost fill detection no longer bypassed on "not enough balance / allowance" errors — all FOK ERROR status now triggers on-chain balance recheck (~100ms, saves 3-4s of useless retries when tokens already sold). Added logging for allowance-refresh retry failures. Added post-reduction-loop final balance recheck as ghost fill safety net.
- **v12.8.0**: WS event-driven + Trailing Take-Profit + sniper thread overhaul — **WS event-driven**: sniper thread changed from fixed `sleep(50ms)` to `poly_ws.wait_for_update()` event-driven，WS push arrival triggers immediate scan（age ~0ms）。Polymarket WS subscription fix：Initial connection uses `type: market + initial_dump: true`, dynamic additions use `operation: subscribe` (fixing root cause of dynamic subscription receiving no data push）。`_on_message` all BBA update paths trigger `_price_event.set()`.**Price fetch optimization**: `get_realtime_odds()` / `get_market_price()` / `get_best_bid()` / `get_best_ask()` / `_get_orderbook_bids_asks()` all add WS freshness fast path — skip REST calls when WS has fresh data (saves ~300ms/call). Sniper thread REST correction changed to on-demand (skipped when WS age < threshold). Monitor `seed_from_rest` ensures WS data available from first round.**Trailing Take-Profit**: Activates trailing TP when profit first reaches P0 threshold, tracks highest profit (HWM) during hold. Exits when drawdown from peak exceeds dynamic threshold — three tiers (>30%/20%/15% profit -> tolerate 30%/35%/40% drawdown), auto-tightens when remaining<120s. Solves "BTC +22.8% peak sliding to +4.5% before exit" problem.**Sniper thread fixes**: Fixed `gap_dir` undefined causing NameError silent crash; sniper thread proactively fetches tokens (no warmup wait); WS Auto-subscribe uses token_id instead of slug (tokens differ across epochs).**Sniper time window expanded**: Changed from fixed `5s-160s` to "PTB ready -> 5s before end", covering longer observation period.**Monitor optimization**: Main loop wake changed to CLOB WS + Chainlink dual-source event-driven (50ms fallback); `seed_from_rest` after subscribe ensures first-round data; WS diagnostic logs switched to logger (into bot log).**env template**: `.env.example` fully annotated (11 module sections + value examples + timeline diagram).

- **v12.7.3**: Main-thread momentum sniper WS->REST price correction — Fixed main-thread sniper using fake low WS price (e.g. $0.53) bypassing `SNIPER_MAX_PRICE` check while actually filling at REST real high price ($0.73). Sniper thread already had this correction (v12.6), now main thread aligned. Telegram notifications distinguish 3 modes. `.env.example` adds full sniper thread config parameter docs.
- **v8.2**: Kelly P_WIN_CAP unification + sell_and_confirm floor price fallback + Kelly logger — Kelly `calculate_kelly_size` now reads `P_WIN_CAP` env (default 0.92) instead of hardcoded 0.85 cap; fixes Kelly always returning MIN_BET when entry price ≥ 0.85. All Kelly `print()` calls converted to `logger.info()` so sizing calculations are visible in server logs (including early-return paths for EV≤0 and kelly≤0). `sell_and_confirm` (used by P0 take-profit) now retries at $0.01 floor price when original price has no buyers — prevents profitable positions from being abandoned as "FOK unfilled". Allowance-refresh retry in `sell_and_confirm` now distinguishes "no balance" vs "no buyer" instead of always returning NO_BALANCE.
- **v8.1**: Ghost fill detection + 425 retry + dynamic Kelly sizing + auto_redeem REST migration — Kelly sizing now scales with balance: `balance × kelly_quarter / price` → shares, clamped by `MIN_BET_SIZE` / `MAX_BET_SIZE` env vars (default 5/10, backward compatible). Raise `MAX_BET_SIZE` to scale up. — Stop-loss FOK timeout: re-check on-chain balance to detect phantom fills (order executed but response lost). Floor-price retry now handles "not enough balance" errors (refresh allowance + retry, or return NO_BALANCE). `place_order` auto-retries on HTTP 425 "Too Early" (0.5s/1.0s/1.5s backoff, up to 3 attempts) — no longer loses bets when CLOB service is momentarily not ready. auto_redeem_v2 migrated from Polymarket CLI subprocess to data-api REST (`/positions` + `/closed-positions`), eliminating `[WinError 2]`; skips positions < 0.1 USDC to save gas.
- **v8.0**: SDK migration + unbiased Bayesian — All trading operations (order, cancel, balance, orderbook) migrated from Polymarket CLI subprocess (2-8s) to py-clob-client SDK direct calls (<50ms). SDK warmup pre-loads TLS connection pool + coincurve signing library + pre-caches neg_risk/fee_rate/tick_size into SDK internal cache (bypasses per-order HTTP lookups). Fake POST warmup pre-establishes HTTP/2 stream for first real order. Pending order reconciliation uses SDK `get_order()` instead of CLI subprocess (CLI dependency fully removed from monitor). Entry pricing uses best_ask from orderbook (primary) instead of stale last-trade-price. FOK (Fill-or-Kill) orders for all exit/stop-loss operations. Bayesian prior changed from market odds to unbiased 0.5 (fixes DOWN directional bias). Tie-breaking at price==PTB now assigns UP instead of DOWN. Circuit breaker fix: empty market list no longer triggers false 300s cooldown. EV spread cost scales by early exit probability (EARLY_EXIT_RATIO, default 0.3). Sell operations return NO_BALANCE on insufficient balance instead of infinite retry. Status field normalized to uppercase throughout (fixes "live"/"pending" case mismatch from SDK responses). Post-buy `token_balance` saved to position + `update_balance_allowance` called to pre-authorize sells (fixes "not enough balance / allowance" at exit). Sell functions auto-refresh allowance and retry when on-chain balance confirmed but API rejects. Monitor uses `token_balance` (actual chain balance) over `size` (order response) for sell sizing. Parallel balance+orderbook fetch in execute_bet (~0.5s saved). Orderbook 2s TTL cache eliminates redundant HTTP within analysis cycle. Warmup phase pre-caches token_ids + SDK parameters in parallel (~2s saved at analysis time).
- **v7.0**: Pending order reconciliation + early bet window + random walk p_win — LIVE orders tracked in `pending_orders.jsonl`, monitor auto-detects fills via wallet balance and records positions with distinct TG notification (⏰). Early bet window (90-95s) with lower thresholds captures CLOB mispricing. Random walk probability `Φ(|gap|/σ√t)` replaces static base_rate. 15-min K-line trend filter reduces counter-trend entries. Balance auto-retry (98%/95%/90%). Market-price immediate stop-loss cancels existing orders first. P0 sells at entry_price for guaranteed fill. Settlement uses API real outcome. ATR hold threshold capped at 2.0 with dual early-exit condition.
- **v6.0**: Full-duration stop-loss + EV-only entry — Stop-loss covers entire market duration (>30s) instead of stage-limited windows. Market-price ladder sell (bid→95%→90%→80%→$0.05→$0.01, ~6s). Removed discount condition from entry (was blocking almost all bets due to conservative estimated_value ≈ 0.51). Entry now uses 3 conditions: EV > MIN_EV, confidence ≥ MIN_CONFIDENCE, odds < MAX_BUY_PRICE (all env configurable). Consolidated duplicated P1 hedge code into single universal block. New `get_best_bid_raw()` for stop-loss (no discount, no slippage deduction).
- **v4.0**: Empty book resilience — Entry: CLOB C1 calibration (best_ask calibration preventing fake discounts) + empty book override (Gamma EVsecond-chance override, calibrated-price order). Exit: `get_best_bid/ask/market_price` fall back to `last-trade-price ± SLIPPAGE` when orderbook empty. Stage 3/4 EV sanity check (market price halved → override fake positive EV). New env: `MAX_BUY_PRICE`, `SLIPPAGE`. Fixes BTC DOWN $0.21->$0.01->$0.00 no stop-loss case.
- **v3.9**: EV-driven diamond hands — direction correct + ≤120s no longer blindly holds; calculates real-time EV (base_rate lookup) and releases weak signals (EV 0~0.03) to Stage 3/4 fine-grained exit. P1 hedge extended to Stage 1 (120-180s) — bid < $0.05 now triggers opposite token hedge instead of doing nothing. Direction-correct hold blocks (>60s) now log EV/ATR for signal quality observation. Removed dead EV computation in Stage 1 entry.
- **v3.8**: Document-inspired enhancements — #7: LMSR inefficiency signal (realtime best_ask vs p_win mispricing → lower entry threshold). #1: Hyperbolic discounting profit-take (dynamic threshold scales with time to settlement, configurable via env). #6: Adaptive Bayesian sampling (3s near bet window). #4: Base Rate validation script (`scripts/validate_base_rate.py`).
- **v3.7**: 4-layer quantitative defense — P0: early profit-taking (≥20% profit + >90s → sell immediately, don't risk boundary reversal). P1: opposite token hedge (bid < $0.05 → buy opposite token to form $1.00 pair at settlement). P2: exit liquidity gate (bid_depth < 5 → skip entry). P3: liquidity-capped sizing (Kelly ≤ 50% of exit bid depth). Opposite token_id recorded at entry for hedge execution.
- **v12.9.11**: Ambush ghost-order hardening — GTD ambush repricing now preserves and tracks all historical order IDs so `not_canceled` residuals remain visible to cleanup and balance reconciliation. FOK retries that hit transport uncertainty before a later success are now written to `pending_orders` as ghost candidates, and `reconcile_pending_orders()` can merge those addon fills into an already-open position while backfilling incremental cost basis. `.gitignore` also ignores local `AGENTS.md` and runtime `logs/*.lock` files.
- **v12.9.10**: Relayer free-gas redeem + zero-RPC settlement — `auto_redeem_v2.py` rewritten: (1) `find_redeemable()` uses data-api REST (`/positions?redeemable=true`) instead of on-chain `payoutDenominator`/`balanceOf` calls (eliminates RPC timeout hangs). (2) `parallel_redeem()` sends via Polymarket Relayer API (EIP-712 signed, `py-builder-relayer-client` SDK) for gasless transactions, with automatic self-paid gas fallback. (3) `cleanup_false_redeemed()` uses data-api instead of chain calls. (4) Post-redeem verification is best-effort (non-blocking). Normal redeem flow: data-api → Relayer → confirmed, zero RPC, zero gas. Requires `RELAYER_API_KEY` in `.env` (from polymarket.com Settings).
- **v3.6**: Direction-based exit — uses crypto price vs PTB (not token orderbook) to determine win/loss. Winning positions hold to settlement ($1.00) instead of being sold at misleading prices. Wide bid-ask spread detection prevents false losses. Post-close safety prevents trades after market ends. Auto redeem now shows USDC balance.
- **v3.5**: Fill price fix — parse CLI output (Taking/Size) for actual fill price instead of limit price. MATCHED status check prevents recording unfilled LIVE orders. Fixes inflated entry_price in notifications, logs, and PnL calculations.
- **v3.4**: Trading Desk Protocol implementation — Base Rate calibration (P0), EV-driven exit (P1), strict binary EV formula (P2), correlated exposure control (P3), EV pricing protection for stages 3-4 (P4), outcome learning loop, cross-validation guard
- **v3.3**: Bayesian sequential updating, LMSR liquidity assessment, corrected Kelly formula, circuit breaker, Playwright smart degradation, CLI-based auto redemption
- **v3.2**: Auto redeem + systemd + stability fixes
- **v3.1**: Warmup observation + trend confirmation + real-time TP/SL
- **v2.1**: Discount arbitrage + Kelly sizing + liquidity filter
- **v2.0**: Direction prediction strategy
- **v1.0**: Initial version

## License

MIT
