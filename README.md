# Polymarket Trading Bot v8.0

Automated trading bot for Polymarket 5-minute crypto UP/DOWN markets. Uses EV-driven entry/exit with Bayesian sequential updating, random-walk probability modeling, LMSR theoretical pricing, market-price stop-loss, pending order reconciliation, and correlated exposure control.

## Strategy

**EV-Driven Entry + Market-Price Stop-Loss**

The bot uses Bayesian sequential updating to detect directional signals, enters when EV (p_win - execution_price) is positive with sufficient confidence, and manages positions with full-duration market-price stop-loss.

### Execution Timeline (300s market)

```
0s    Market starts
2s    PTB acquisition (Playwright → HTML → Gamma API fallback)
20s   Bayesian warmup (5s intervals, 3s after 80s)
40s   PTB deadline
90s   Early bet window opens (lower thresholds, CLOB mispricing)
95s   Early bet window closes
100s  Late bet window opens (standard thresholds)
160s  Late bet window closes → Real-time EV monitoring starts
      ── Exit Protocol ──
>30s  Universal stop-loss: direction wrong → market-price ladder sell
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
- **Early Bet Window (90-95s)**: Enters before CLOB fully prices in, with lower thresholds (`EARLY_MIN_EV`, `EARLY_MIN_CONFIDENCE`). Captures mispricing before market makers adjust.
- **5-min K-line Trend Filter**: Checks Binance 5-min candle trend (same timeframe as market) to avoid counter-trend trades. Replaces 15-min filter which was too slow for 5-minute markets.
- **Bayesian Fusion**: Base rate (40%) + Bayesian posterior (60%) when direction-aligned with confidence > 30%.
- **Cross-Validation**: Flags overestimation when `estimated_value > p_win + 0.15` (reduces confidence 15%).
- **LMSR Inefficiency Signal**: When realtime `best_ask` diverges >10% from `p_win`, lowers discount threshold by 2% (min 6%) for easier entry on mispriced markets.
- **LMSR Liquidity Assessment**: Orderbook spread/depth/slippage scoring → dynamic discount threshold (8%-20%).
- **Exit Liquidity Gate (P2)**: Before entering, checks bid-side depth of the chosen token. `bid_depth < 5` → skip entry entirely. Prevents entering positions that can't be exited.
- **Correlated Exposure Control**: BTC/ETH correlation ~0.85. Same-direction position halves Kelly sizing.
- **1/4 Kelly Sizing**: Binary formula `f* = (p - price) / (1 - price)`, quarter Kelly, 5-10 shares hard bounds.
- **Liquidity-Capped Sizing (P3)**: Kelly size capped at 50% of exit bid depth. Works with P2 — P2 gates entry, P3 adjusts size.
- **Balance Auto-Retry**: When balance insufficient, automatically retries with reduced size (98%/95%/90%) instead of skipping entirely.
- **Post-Buy Allowance Refresh**: After successful buy (both direct and pending fill), queries actual on-chain `token_balance` and calls `update_balance_allowance` to ensure sell authorization is pre-set. Prevents "not enough balance / allowance" errors at exit time.
- **Parallel Entry Fetch**: Balance + orderbook queries run concurrently via ThreadPoolExecutor, saving ~0.5s per bet execution.
- **CLOB C1 Calibration**: Replaces Gamma odds with CLOB `best_ask` for discount/EV/price checks — prevents buying at $0.85 while thinking price is $0.50. Empty book detection (ask ≥ 0.95) falls back to `last-trade-price`.
- **Empty Book Override**: When C1 calibration makes discount negative (stale last-trade-price > estimated_value), a second-chance check uses Gamma odds: if `gamma_discount ≥ threshold`, `gamma_EV > 0.05`, and `confidence ≥ 75%`, overrides to BET. Execution still uses calibrated price (last-trade-price + SLIPPAGE).
- **Pending Order Tracking**: LIVE (unfilled) orders are recorded to `pending_orders.jsonl` and reconciled by position_monitor when filled on-chain.

### Exit (P0-P1 + P4)
- **Hyperbolic Discounting Profit-Take (P0)**: Dynamic threshold `base × (1 + k × minutes_remaining)` — faraway paper profits are less reliable, so the further from settlement, the higher the profit bar. Configurable via `P0_BASE_PROFIT` (default 0.20) and `P0_HYPERBOLIC_K` (default 0.15). P0 sells at entry_price (guaranteed fill) instead of best_bid.
- **Market-Price Immediate Stop-Loss**: `market_sell_immediate()` cancels existing orders first (prevents balance lock), then ladder sells at bid→95%→90%→80%→$0.05→$0.01 (~6s to clear). On "not enough balance / allowance" error with confirmed on-chain balance, auto-refreshes allowance and retries before falling back to reduced-size attempts.
- **Opposite Token Hedge (P1)**: When losing and bid < $0.05 (no buyers), buys the opposite token to form a guaranteed pair (UP + DOWN = $1.00 at settlement). Only hedges when `opposite_ask < (1.00 - entry_price - 0.02)`, ensuring net profit. Opposite token_id is recorded at entry time.
- **EV-Driven Diamond Hands**: Direction correct + ≤120s remaining → calculates real-time EV. High EV (>0.03) or large ATR deviation (≥1.0, capped at 2.0) → hold to settlement. Weak EV (0~0.03) → releases to Stage 3/4 fine-grained EV strategy. Early exit requires both EV < -3% AND ATR < 1.0 (dual condition).
- **Direction-Based Exit**: Uses crypto price vs PTB to determine win/loss (not token orderbook price, which can be misleading due to wide bid-ask spreads near settlement).
  - Direction correct + EV > 0.03 or strong ATR → **hold to settlement** ($1.00)
  - Direction correct but weak signal → releases to stage exit strategy
  - Direction wrong → **Universal market-price stop-loss** (immediate ladder sell)
  - No buyers (bid < $0.05) → Attempt hedge (P1), fall back to wait for expiry
  - Safety: direction correct but token drops >10% → downgrade to "unknown", trigger stop-loss
- **Wide Spread Protection**: `get_market_price()` detects bid-ask spread > 50% and falls back to midpoint API → `last-trade-price` → single-side price, preventing false loss signals.
- **Empty Book Fallback (Monitor)**: `get_best_bid()` / `get_best_ask()` fall back to `last-trade-price ± SLIPPAGE` when CLOB is empty (bid ≤ 0.02 or ask ≥ 0.95). Enables hedge and stop-loss decisions with real market prices instead of $0.01/$0.99.
- **EV Sanity Check (Stage 3/4)**: When EV says "hold" but `last-trade-price < entry_price × 0.5` (market price halved), overrides EV to negative — prevents holding to settlement on fake positive EV.
- **Post-Close Safety**: No sell logic runs after market close (remaining < 0), preventing direction flicker from causing unwanted trades.
- **3-Stage Graduated Exit**: For remaining edge cases — Stage 2 (120-60s) with batch orders, Stage 3 (60-30s) aggressive, Stage 4 (30-0s) EV≥0 hold / EV<0 floor prices.
- **API-Based Settlement**: Expiry cleanup uses Polymarket API real outcome (not Binance price guess) to determine $1.00/$0.00.
- **Pending Order Reconciliation**: Monitor checks LIVE buy orders every cycle — detects fills via wallet balance, writes position to `positions.jsonl`, sends distinct TG notification (⏰ vs 🎯). Auto-cancels stale orders after `PENDING_ORDER_TTL` (default 120s).

### Infrastructure
- **Warmup Token Pre-Cache**: During warmup phase, token_ids + SDK parameters (neg_risk/fee_rate/tick_size) are fetched and cached in parallel, saving ~2s at analysis time.
- **Orderbook Cache (2s TTL)**: `get_orderbook()` caches results for 2 seconds to eliminate duplicate HTTP requests within the same analysis cycle. Auto-invalidated after order placement.
- **Adaptive Warmup Sampling**: 5s intervals during 20-80s, accelerates to 3s during 80-100s (near bet window) for sharper Bayesian posterior
- **Trend Safety Valve**: Gap expanding/shrinking/crossing/oscillating → adjusts min discount
- **Network Circuit Breaker**: 5 consecutive API failures → 300s pause
- **Playwright Smart Degradation**: Skip browser PTB after 3 consecutive failures
- **Outcome Learning Loop**: Every close records outcome → auto-calibrates base rates every 50 trades
- **Telegram Notifications**: Entry (🎯 direct / ⏰ pending fill), exits, settlements, errors, balance. Pending order expiry (⌛) also notified.
- **Auto Redeem**: Claim settled positions (configurable interval via `REDEEM_INTERVAL`), shows USDC balance after each round
- **Watchdog**: `watchdog_v3.sh` monitors all 3 services and auto-restarts on failure
- **systemd Management**: Auto-restart on crash, boot-start

## Components

| File | Role |
|------|------|
| `auto_bot_v3.py` | Main engine: market discovery, warmup, early/late betting, correlation control |
| `ai_trader/clob_client.py` | CLOB SDK wrapper: global singleton, GTC/FOK orders, balance, orderbook, warmup |
| `ai_analyze_v2.py` | Decision engine: strict EV, Kelly sizing, Bayesian fusion, bet execution, pending order tracking |
| `ai_trader/ai_model_v2.py` | Scoring model: ATR deviation → token value estimation → discount |
| `ai_trader/base_rate.py` | Base Rate calibration: conservative priors + empirical learning |
| `scripts/validate_base_rate.py` | Base Rate calibration validator: ATR-band win rates vs priors |
| `ai_trader/bayesian_engine.py` | Bayesian sequential updater: sigmoid likelihood, log-space, anti-saturation |
| `ai_trader/lmsr_liquidity.py` | Orderbook liquidity: spread + depth + slippage → dynamic threshold |
| `position_monitor.py` | EV-driven exit + 4-stage closing + pending order reconciliation + outcome recording |
| `auto_redeem_v2.py` | Auto claim settled positions (REST API + on-chain redeem) |
| `ai_trader/binance_api.py` | Binance market data (klines, price, 24h stats) |
| `ai_trader/indicators.py` | Technical indicators (EMA, RSI, ATR, Bollinger Bands) |
| `ai_trader/polymarket_api.py` | Polymarket API + PTB HTML scraper |
| `ai_trader/playwright_ptb.py` | PTB extraction via headless Chromium |
| `trading_state.py` | State management: cooldown, daily PnL, win/loss tracking |
| `watchdog_v3.sh` | Process watchdog: monitors and auto-restarts all services |

## Betting Conditions

All must be met (all configurable via `.env`):

| Condition | Late Window | Early Window | Env Key |
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
  ├─ Hard bounds: 5-10 shares
  └─ Balance constraints (10-20% of balance)

Layer 3 — Position Management
  ├─ P0: Hyperbolic discounting profit-take (entry_price sell for guaranteed fill)
  ├─ P1: Opposite token hedge (bid < $0.05 → buy opposite for $1 pair)
  ├─ Market-price immediate stop-loss (cancel first, then ladder sell)
  ├─ Wide spread detection (prevents false loss signals)
  ├─ Post-close safety (no trades after market ends)
  ├─ 3-stage graduated closing (Stage 2/3/4 for remaining edge cases)
  ├─ API-based settlement (real outcome, not price guess)
  └─ Pending order reconciliation (LIVE → detect fill → record position → notify)

Layer 4 — System Protection
  ├─ Max open positions: 2 (configurable)
  ├─ Daily loss limit: $10 (configurable)
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
- [py-clob-client](https://github.com/Polymarket/py-clob-client) (`pip install py-clob-client`)
- Chromium browser (for Playwright PTB extraction)

### Installation

```bash
git clone https://github.com/youruser/polymarket-trading-bot.git
cd polymarket-trading-bot

pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# Edit .env with your wallet addresses and Telegram token

```

### Environment Variables

See `.env.example` for all configurable parameters:

| Variable | Required | Description |
|----------|----------|-------------|
| `EOA_WALLET` | Yes | EOA wallet address for signing |
| `PROXY_WALLET` | Yes | Polymarket proxy wallet (holds positions, see Settings on polymarket.com) |
| `CLOB_SIGNATURE_TYPE` | No | `0` = EOA direct (default), `1` = Polymarket Gnosis Safe proxy |
| `PRIVATE_KEY` | Yes | Private key for SDK signing + on-chain settlement |
| `POLYGON_RPC_URL` | No | Polygon RPC endpoint (default: public) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | No | Telegram chat ID for notifications |
| `REDEEM_INTERVAL` | No | Auto redeem polling interval in seconds (default: 600) |
| `MAX_DAILY_LOSS` | No | Daily loss limit in USD (default: 10) |
| `MAX_OPEN_POSITIONS` | No | Max concurrent positions (default: 2) |
| `MIN_BET_SIZE` | No | Min shares per bet (default: 5) |
| `MAX_BET_SIZE` | No | Max shares per bet — raise to scale with balance (default: 10) |
| `MIN_BALANCE` | No | Min balance to place bets (default: 5) |
| `MAX_BUY_PRICE` | No | Max buy price for entry (default: 0.90) |
| `MIN_EV` | No | Min EV for late window (default: 0.10) |
| `MIN_CONFIDENCE` | No | Min confidence for late window (default: 0.70) |
| `MIN_ATR_DEVIATION` | No | Min ATR deviation for entry (default: 1.4) |
| `P_WIN_CAP` | No | Max p_win cap (default: 0.92) |
| `EARLY_BET_START` | No | Early bet window start in seconds (default: 90) |
| `EARLY_BET_END` | No | Early bet window end in seconds (default: 95) |
| `EARLY_MIN_EV` | No | Min EV for early window (default: 0.08) |
| `EARLY_MIN_CONFIDENCE` | No | Min confidence for early window (default: 0.60) |
| `LATE_BET_START` | No | Late bet window start (default: 100) |
| `LATE_BET_END` | No | Late bet window end (default: 160) |
| `SLIPPAGE` | No | Empty book fallback slippage for last-trade-price ± (default: 0.02) |
| `P0_BASE_PROFIT` | No | P0 take-profit base threshold (default: 0.20) |
| `PROFIT_THRESHOLD` | No | General profit threshold (default: 0.15) |
| `P0_HYPERBOLIC_K` | No | Hyperbolic discounting coefficient (default: 0.15) |
| `PENDING_ORDER_TTL` | No | Max wait for LIVE orders before cancel (default: 120s) |
| `EARLY_EXIT_RATIO` | No | Probability of early exit for spread cost calc (default: 0.3) |
| `PENDING_MIN_FILL` | No | Min filled size to record position (default: 0.5) |

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

- **v8.2**: Kelly P_WIN_CAP unification + sell_and_confirm floor price fallback + Kelly logger — Kelly `calculate_kelly_size` now reads `P_WIN_CAP` env (default 0.92) instead of hardcoded 0.85 cap; fixes Kelly always returning MIN_BET when entry price ≥ 0.85. All Kelly `print()` calls converted to `logger.info()` so sizing calculations are visible in server logs (including early-return paths for EV≤0 and kelly≤0). `sell_and_confirm` (used by P0 take-profit) now retries at $0.01 floor price when original price has no buyers — prevents profitable positions from being abandoned as "FOK未成交". Allowance-refresh retry in `sell_and_confirm` now distinguishes "no balance" vs "no buyer" instead of always returning NO_BALANCE.
- **v8.1**: Ghost fill detection + 425 retry + dynamic Kelly sizing + auto_redeem REST migration — Kelly sizing now scales with balance: `balance × kelly_quarter / price` → shares, clamped by `MIN_BET_SIZE` / `MAX_BET_SIZE` env vars (default 5/10, backward compatible). Raise `MAX_BET_SIZE` to scale up. — Stop-loss FOK timeout: re-check on-chain balance to detect phantom fills (order executed but response lost). Floor-price retry now handles "not enough balance" errors (refresh allowance + retry, or return NO_BALANCE). `place_order` auto-retries on HTTP 425 "Too Early" (0.5s/1.0s/1.5s backoff, up to 3 attempts) — no longer loses bets when CLOB service is momentarily not ready. auto_redeem_v2 migrated from Polymarket CLI subprocess to data-api REST (`/positions` + `/closed-positions`), eliminating `[WinError 2]`; skips positions < 0.1 USDC to save gas.
- **v8.0**: SDK migration + unbiased Bayesian — All trading operations (order, cancel, balance, orderbook) migrated from Polymarket CLI subprocess (2-8s) to py-clob-client SDK direct calls (<50ms). SDK warmup pre-loads TLS connection pool + coincurve signing library + pre-caches neg_risk/fee_rate/tick_size into SDK internal cache (bypasses per-order HTTP lookups). Fake POST warmup pre-establishes HTTP/2 stream for first real order. Pending order reconciliation uses SDK `get_order()` instead of CLI subprocess (CLI dependency fully removed from monitor). Entry pricing uses best_ask from orderbook (primary) instead of stale last-trade-price. FOK (Fill-or-Kill) orders for all exit/stop-loss operations. Bayesian prior changed from market odds to unbiased 0.5 (fixes DOWN directional bias). Tie-breaking at price==PTB now assigns UP instead of DOWN. Circuit breaker fix: empty market list no longer triggers false 300s cooldown. EV spread cost scales by early exit probability (EARLY_EXIT_RATIO, default 0.3). Sell operations return NO_BALANCE on insufficient balance instead of infinite retry. Status field normalized to uppercase throughout (fixes "live"/"pending" case mismatch from SDK responses). Post-buy `token_balance` saved to position + `update_balance_allowance` called to pre-authorize sells (fixes "not enough balance / allowance" at exit). Sell functions auto-refresh allowance and retry when on-chain balance confirmed but API rejects. Monitor uses `token_balance` (actual chain balance) over `size` (order response) for sell sizing. Parallel balance+orderbook fetch in execute_bet (~0.5s saved). Orderbook 2s TTL cache eliminates redundant HTTP within analysis cycle. Warmup phase pre-caches token_ids + SDK parameters in parallel (~2s saved at analysis time).
- **v7.0**: Pending order reconciliation + early bet window + random walk p_win — LIVE orders tracked in `pending_orders.jsonl`, monitor auto-detects fills via wallet balance and records positions with distinct TG notification (⏰). Early bet window (90-95s) with lower thresholds captures CLOB mispricing. Random walk probability `Φ(|gap|/σ√t)` replaces static base_rate. 15-min K-line trend filter reduces counter-trend entries. Balance auto-retry (98%/95%/90%). Market-price immediate stop-loss cancels existing orders first. P0 sells at entry_price for guaranteed fill. Settlement uses API real outcome. ATR hold threshold capped at 2.0 with dual early-exit condition.
- **v6.0**: Full-duration stop-loss + EV-only entry — Stop-loss covers entire market duration (>30s) instead of stage-limited windows. Market-price ladder sell (bid→95%→90%→80%→$0.05→$0.01, ~6s). Removed discount condition from entry (was blocking almost all bets due to conservative estimated_value ≈ 0.51). Entry now uses 3 conditions: EV > MIN_EV, confidence ≥ MIN_CONFIDENCE, odds < MAX_BUY_PRICE (all env configurable). Consolidated duplicated P1 hedge code into single universal block. New `get_best_bid_raw()` for stop-loss (no discount, no slippage deduction).
- **v4.0**: Empty book resilience — Entry: CLOB C1 calibration (best_ask校准防虚假折价) + empty book override (Gamma EV二次放行, 校准价下单). Exit: `get_best_bid/ask/market_price` fall back to `last-trade-price ± SLIPPAGE` when orderbook empty. Stage 3/4 EV sanity check (market price halved → override fake positive EV). New env: `MAX_BUY_PRICE`, `SLIPPAGE`. Fixes BTC DOWN $0.21→$0.01→$0.00 全程无止损 case.
- **v3.9**: EV-driven diamond hands — direction correct + ≤120s no longer blindly holds; calculates real-time EV (base_rate lookup) and releases weak signals (EV 0~0.03) to Stage 3/4 fine-grained exit. P1 hedge extended to Stage 1 (120-180s) — bid < $0.05 now triggers opposite token hedge instead of doing nothing. Direction-correct hold blocks (>60s) now log EV/ATR for signal quality observation. Removed dead EV computation in Stage 1 entry.
- **v3.8**: Document-inspired enhancements — #7: LMSR inefficiency signal (realtime best_ask vs p_win mispricing → lower entry threshold). #1: Hyperbolic discounting profit-take (dynamic threshold scales with time to settlement, configurable via env). #6: Adaptive Bayesian sampling (3s near bet window). #4: Base Rate validation script (`scripts/validate_base_rate.py`).
- **v3.7**: 4-layer quantitative defense — P0: early profit-taking (≥20% profit + >90s → sell immediately, don't risk boundary reversal). P1: opposite token hedge (bid < $0.05 → buy opposite token to form $1.00 pair at settlement). P2: exit liquidity gate (bid_depth < 5 → skip entry). P3: liquidity-capped sizing (Kelly ≤ 50% of exit bid depth). Opposite token_id recorded at entry for hedge execution.
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
