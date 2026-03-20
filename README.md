# Polymarket Trading Bot v9.9

Automated trading bot for Polymarket 5-minute crypto UP/DOWN markets. Uses EV-driven entry/exit with Bayesian sequential updating, random-walk probability modeling, LMSR theoretical pricing, market-price stop-loss, pending order reconciliation, and correlated exposure control.

## Strategy

**EV-Driven Entry + Market-Price Stop-Loss**

The bot uses Bayesian sequential updating to detect directional signals, enters when EV (p_win - execution_price) is positive with sufficient confidence, and manages positions with full-duration market-price stop-loss.

### Execution Timeline (300s market)

```
0s    Market starts
2s    PTB acquisition (Playwright only, no fallback — skip if unavailable)
20s   Bayesian warmup (5s intervals, 3s after 80s)
40s   PTB deadline
90s   Early bet window opens (lower thresholds, CLOB mispricing)
95s   Early bet window closes
100s  Late bet window opens (standard thresholds)
160s  Late bet window closes → Real-time EV monitoring starts
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
- **Volatility-Triggered Re-Analysis**: Markets skipped due to low confidence are tracked in `skipped_markets`. When crypto price moves ≥1.5 ATR from the skip price (`REANALYZE_ATR_MULT`), the market re-enters the analysis pipeline with updated Bayesian state. Cooldown (`REANALYZE_COOLDOWN`, default 15s) and max retrigger cap (`MAX_REANALYZE`, default 1) prevent loops. Skipped markets are cleaned up on position entry or market cleanup.
- **Pending Order Tracking**: LIVE (unfilled) orders are recorded to `pending_orders.jsonl` and reconciled by position_monitor when filled on-chain.

### Exit (P0-P1 + P4)
- **PTB Proximity Buffer**: When `abs(crypto_price - ptb_price) / ATR < dynamic_threshold`, crypto is in the "noise zone" near PTB — direction signal is unreliable. Freezes `direction_correct = True` (trusts original bet), suppressing all direction-based stop-losses. Threshold decays with time: 0.7 ATR (first 2min) → 0.3 ATR (mid) → 0.15 ATR (last 1min). Extreme safety valve: token drop ≥50% (`PTB_PROXIMITY_EXTREME_STOP`) forces exit regardless. Configurable via `PTB_PROXIMITY_ATR`.
- **-25% Hard Stop**: When token drops ≥25% from entry price, market sell when direction is not confirmed correct. In proximity zone, direction is frozen True so hard stop only fires via extreme safety valve (-50%).
- **Direction Flip Exit (Consecutive Confirmation)**: Tracks `direction_correct` across cycles. True→False flip now requires **consecutive confirmation** (2 rounds for ATR<1.5, 1 round for ATR≥1.5) before liquidation — prevents single-poll noise from triggering premature exits. `direction_wrong_streak` counter resets when direction returns to True.
- **ATR 3-Layer Decision Matrix**: When token drops ≥15% (`PRICE_DROP_TRIGGER`), action depends on ATR deviation (crypto distance from PTB in ATR units):
  - **ATR ≥ 2.0** (safe zone): 🟢 Dip-buy — adds 50% of original position at best_ask via FOK. Requires: direction correct, remaining > 60s, max 1 dip-buy per position. Averages down cost basis for higher profit if direction holds.
  - **ATR 1.0-2.0** (uncertain): 🟡 Hold — no action, continue monitoring. ATR rising → enters safe zone, ATR falling → enters danger zone.
  - **ATR < 1.0** (danger zone): 🔴 Stop-loss — immediate sell at best_bid, only when direction is wrong. Direction correct + ATR dip is normal volatility, not a stop signal.
- **Hyperbolic Discounting Profit-Take (P0)**: Dynamic threshold `base × (1 + k × minutes_remaining)` — faraway paper profits are less reliable, so the further from settlement, the higher the profit bar. Configurable via `P0_BASE_PROFIT` (default 0.20) and `P0_HYPERBOLIC_K` (default 0.15). P0 sells at entry_price (guaranteed fill) instead of best_bid.
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
- **3-Stage Graduated Exit**: For remaining edge cases — Stage 2 (120-60s) with batch orders, Stage 3 (60-30s) aggressive, Stage 4 (30-0s) EV≥0 hold / EV<0 floor prices.
- **API-Based Settlement**: Expiry cleanup uses Polymarket API real outcome (not Binance price guess) to determine $1.00/$0.00.
- **Pending Order Reconciliation**: Monitor checks LIVE buy orders every cycle — detects fills via wallet balance, writes position to `positions.jsonl`, sends distinct TG notification (⏰ vs 🎯). Auto-cancels stale orders after `PENDING_ORDER_TTL` (default 120s).

### Infrastructure
- **Pyth Network On-Chain Price Stream**: `PythPriceStream` singleton in `pyth_api.py` receives BTC/ETH prices from Pyth Hermes v2 SSE stream (`hermes.pyth.network`). On-chain oracle prices are closer to Polymarket settlement source than CEX prices. Used as **primary** price source for position monitoring and settlement judgment (`get_current_crypto_price()`). REST fallback when SSE is stale. No API key required, free tier is production-grade.
- **Binance WebSocket Price Stream**: Shared `BinancePriceStream` singleton in `binance_api.py` receives BTC/ETH trade prices via `wss://stream.binance.com` (~10ms push latency). `get_current_price()` reads from memory (0ms), auto-fallback to REST if WebSocket data is stale (>5s). Used by warmup sampling and analysis (K-line/ATR). Serves as **fallback** for position monitoring when Pyth is unavailable. Auto-reconnect with 2s backoff + 30s ping keepalive.
- **Polymarket WebSocket Orderbook Stream**: `PolymarketOrderbookStream` singleton in `polymarket_ws.py` connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market` for real-time orderbook data. Handles `best_bid_ask`, `book`, and `price_change` events. FOK entry uses WS `best_ask` (0ms) instead of REST orderbook query (100-300ms), significantly reducing price staleness between read and order placement. Position monitor's `get_best_bid/get_best_ask/get_market_price` all use WS-first with REST fallback. Lazy connection: WS connects only on first `subscribe()` call (Polymarket requires immediate subscription after connect). Auto-subscribes token_ids during warmup, auto-unsubscribes on market cleanup. 9s PING heartbeat, auto-reconnect with exponential backoff (2s→4s→8s→...→30s, resets on stable connection).
- **Warmup Token Pre-Cache**: During warmup phase, token_ids + SDK parameters (neg_risk/fee_rate/tick_size) are fetched and cached in parallel, saving ~2s at analysis time.
- **Orderbook Cache (2s TTL)**: `get_orderbook()` caches results for 2 seconds to eliminate duplicate HTTP requests within the same analysis cycle. Auto-invalidated after order placement.
- **Adaptive Warmup Sampling**: 5s intervals during 20-80s, accelerates to 3s during 80-100s (near bet window) for sharper Bayesian posterior
- **Trend Safety Valve**: Gap expanding/shrinking/crossing/oscillating → adjusts min discount
- **Network Circuit Breaker**: 5 consecutive API failures → 300s pause
- **Playwright PTB (No Fallback)**: PTB only from Playwright subprocess. HTML/Gamma API fallbacks removed (returned wrong market's PTB). Skip browser PTB after 3 consecutive failures.
- **Volatility Re-Trigger**: Skipped markets monitored for large price moves — piggybacks on existing sampling loop (no extra API calls), re-enters analysis when volatility exceeds threshold
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
| `ai_trader/pyth_api.py` | Pyth Network on-chain price stream: SSE real-time + REST fallback (primary for position monitor) |
| `ai_trader/binance_api.py` | Binance market data: WebSocket real-time price stream + REST klines/stats (fallback for position monitor) |
| `ai_trader/polymarket_ws.py` | Polymarket Market Channel WebSocket: real-time orderbook/best_bid_ask stream |
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
  ├─ PTB Proximity Buffer: crypto near PTB → freeze direction, prevent noise stop-loss
  ├─ -25% hard stop: market sell when direction≠True (proximity: -50% extreme stop)
  ├─ Direction flip exit: True→False + consecutive confirmation → liquidation
  ├─ ATR 3-layer decision: ≥15% drop → dip-buy (ATR≥2) / hold (1-2) / stop-loss (ATR<1)
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
- [websocket-client](https://pypi.org/project/websocket-client/) (`pip install websocket-client`) — Binance WebSocket price stream
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

- **v9.9**: 币种可配置化 + BNB支持 + Chainlink止损优化 — **Multi-coin support**: New `ai_trader/coins.py` centralized coin configuration. All hardcoded BTC/ETH references replaced with dynamic lookups. Add/remove coins via `ENABLED_COINS` env var (e.g., `BTC,ETH,BNB`). Each coin auto-configures: Binance WS stream, Chainlink RTDS symbol, Pyth feed ID, Polymarket slug prefix. BNB (Binance Coin) added as third supported coin (`bnb-updown-5m` markets, Chainlink `bnb/usd` settlement feed). Files updated: `polymarket_api.py` (market discovery), `binance_api.py` (WS streams + symbol parsing), `pyth_api.py` (feed IDs), `polymarket_rtds.py` (Chainlink symbols), `position_monitor.py` (slug→coin + Binance symbol), `auto_bot_v3.py` (slug→coin). P3 correlation control applies to all enabled coins (same-direction positions get Kelly halved). **Chainlink stop-loss optimization**: (1) ATR decay stop — exits when ATR drops below 0.5 AND below 30% of entry ATR, before token price collapses (saves ~$2-3 per losing trade). (2) ATR acceleration stop — 3 consecutive ATR readings declining with current ATR<0.5 + token loss>10% triggers immediate exit. (3) Direction-correct ATR stop — ATR<0.5 + token loss>20% now triggers stop-loss even with correct direction (50:50 gamble not worth holding). (4) Proximity fast release — token loss exceeding hard stop line (-25%) immediately breaks proximity protection without waiting for streak=4. New env: `ENABLED_COINS`, `ATR_DECAY_EXIT_THRESHOLD`, `ATR_DECAY_RATIO`, `ATR_DIRECTION_CORRECT_STOP`. New file: `ai_trader/coins.py`.

- **v9.8**: RTDS Chainlink结算价 + MAX_OPEN_POSITIONS竞争修复 — Position monitor now uses Polymarket RTDS WebSocket (`wss://ws-live-data.polymarket.com`) Chainlink on-chain feed as primary price source, replacing Pyth. Chainlink is the exact price Polymarket uses for settlement (UMA oracle references Chainlink feeds), eliminating any price discrepancy in win/loss judgment. Zero-latency WebSocket push vs Pyth SSE (~1s). Price source priority: Chainlink(CL) → Pyth SSE → Pyth REST → Binance WS → Binance REST. Monitor status line shows `CL`/`Pyth`/`WS`/`REST` tag. Pyth and Binance fully retained as fallback. New `ai_trader/polymarket_rtds.py`: singleton WebSocket client subscribing to `crypto_prices_chainlink` topic, 5s PING heartbeat, exponential backoff reconnection, 5s staleness threshold. MAX_OPEN_POSITIONS race condition fix: BTC/ETH parallel `analyze_and_trade` (ThreadPoolExecutor) could both pass the position count check simultaneously when `open_count=0`, opening 2 positions despite `MAX_OPEN_POSITIONS=1`. Fixed with `threading.Lock` + `_pending_bets` reservation counter — check atomically reads `open_count + pending`, reserves a slot before releasing the lock, and releases in `finally` block after bet completes. New file: `ai_trader/polymarket_rtds.py`.

- **v9.7**: PnL统计修复 + 晚期窗口重分析 — **PnL tracking fixes**: (1) Ghost trade price estimation — when FOK returns ERROR but on-chain balance is zero (ghost fill), exit price now estimated via LTP/best_bid instead of $0.01 floor price, preventing massive false losses ($4-5 per ghost trade). (2) Cross-process file lock — `trading_state.json` now protected by `fcntl.flock` (process-level) + `threading.Lock` (thread-level), fixing race conditions between `auto_bot_v3` and `position_monitor` that could corrupt `daily_pnl`/`pending_costs`/win-loss counts. `record_bet_result` now runs under the same lock. (3) Partial exit PnL tracking — Stage 2 batch sell partial fills now call `record_partial_pnl()` for mini-settlement, adding back the sold shares' pre-deducted cost + actual PnL to `daily_pnl` immediately (previously lost until final close). (4) NO_BALANCE exit price fallback — 9 code paths that used `current_price or 0` now use multi-source fallback (current_price → LTP → best_bid → entry_price), preventing false full-loss recording when tokens were already sold. (5) Dip-buy cost pre-deduction — `execute_dip_buy` now calls `record_bet_cost` after successful buy; `pending_costs` accumulates (+=) instead of overwriting, correctly tracking total cost basis for dip-buy positions. **Late window re-analysis**: Early window (90-95s) analysis failure no longer blocks late window — `self.analyzed` is only set on bet success, MAX_TRADE_RETRIES exhaustion, or fatal error. Late window pre-check rejections (conf<0.15, gap cross) no longer permanently block; they wait for `LATE_REANALYZE_INTERVAL` (default 20s) cooldown then re-evaluate with updated Bayesian posterior. With 100s late window (100-200s) and 20s interval, up to 5 re-analyses with fresh market data. New env: `LATE_REANALYZE_INTERVAL`, `MAX_TRADE_RETRIES` (moved to .env.example with recommended default 5).

- **v9.6**: Pyth链上价格源 + FOK晚期重试 + WS指数退避 — Position monitor now uses Pyth Network on-chain oracle prices (Hermes v2 SSE stream) as primary price source, replacing Binance. Pyth prices are closer to Polymarket settlement source (UMA oracle references on-chain feeds), reducing price discrepancy in win/loss judgment. Price source priority: Pyth SSE → Pyth REST → Binance WS → Binance REST. Monitor status line shows `Pyth`/`WS`/`REST` tag. K-line data (ATR/OHLCV) remains Binance (Pyth has no candle data). Late window FOK retry: previously, FOK/liquidity failures in the late window (100-160s) permanently blocked re-analysis (`self.analyzed.add` was unconditional before trade execution). Now only marks as analyzed on success or non-retryable failure (AI says no bet, missing PTB/token). FOK/liquidity failures allow retry on next loop iteration (~7s), up to `MAX_TRADE_RETRIES` (default 3). Polymarket WS reconnection now uses exponential backoff (2s→4s→8s→...→30s) instead of flat 2s, preventing reconnection storms that trigger server-side rate limiting. Backoff resets on stable connection (>10s). Reduced reconnect log noise. New files: `ai_trader/pyth_api.py`. New env: `MAX_TRADE_RETRIES`.

- **v9.5**: 波动触发重分析 — Skipped markets (low confidence, weak gap cross) are now tracked and monitored for volatility. When crypto price moves ≥1.5 ATR (`REANALYZE_ATR_MULT`) from the skip price, the market re-enters the late window analysis pipeline with its accumulated Bayesian state. Piggybacks on existing warmup sampling loop (zero extra API calls). Guards: cooldown timer (`REANALYZE_COOLDOWN`, default 15s), max 1 retrigger per market (`MAX_REANALYZE`), no retrigger if position already open, `is_reanalyze` flag prevents infinite skip→retrigger loops. Skipped markets cleaned up on position entry and market cleanup. New env: `REANALYZE_ATR_MULT`, `REANALYZE_COOLDOWN`, `MAX_REANALYZE`.

- **v9.4**: MAX_DAILY_LOSS风控修复 + PTB去兜底 — Daily loss limit now thread-safe with `threading.Lock`, checked at the top of `analyze_and_trade()` (before any analysis/betting), preventing parallel BTC/ETH threads from both passing the check simultaneously. Bet cost pre-deducted to `daily_pnl` on entry (worst-case full loss), settled with actual PnL on position close via `settle_bet_cost()`. Dip-buy (`execute_dip_buy`) also checks daily loss limit before adding. PTB fallback layers (HTML scraper + Gamma API) removed — they returned wrong market's PTB on Playwright timeout (e.g., $73,934 from a different 5-min window instead of $71,193). Now Playwright-only: fail → skip market, no false PTB.

- **v9.3**: PTB Proximity Buffer + consecutive confirmation stop-loss — When crypto price is near PTB (within configurable ATR threshold), direction signal is unreliable noise. New proximity buffer freezes `direction_correct = True`, suppressing all direction-based stop-losses (hard stop, direction flip, ATR danger, full-time wrong). Threshold decays with time: 0.7 ATR (first 2min) → 0.3 ATR (mid) → 0.15 ATR (last 1min). Extreme safety valve at -50% token drop. Direction flip (True→False) now requires consecutive confirmation rounds (2 for ATR<1.5, 1 for ATR≥1.5) instead of instant liquidation — prevents single-poll price fluctuation from triggering premature exits. Full-time direction-wrong stop-loss also requires streak confirmation (3 rounds for ATR<1.0, 2 for ATR<1.5, 1 for larger deviations). Bug fix: `prev_direction_correct` now preserved during pending confirmation to prevent #2 trigger from becoming dead code. New env: `PTB_PROXIMITY_ATR` (default 0.7), `PTB_PROXIMITY_EXTREME_STOP` (default 0.50).

- **v9.2**: Polymarket WebSocket real-time orderbook — FOK entry now uses WS `best_ask` (0ms delay) instead of REST orderbook query (100-300ms), eliminating price staleness between read and order placement. New `polymarket_ws.py` singleton connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`, handles `best_bid_ask`/`book`/`price_change` events. Position monitor's `get_best_bid/get_best_ask/get_market_price` all WS-first with REST fallback. Lazy connection design: WS only connects on first `subscribe()` (Polymarket requires immediate subscription after connect, otherwise disconnects). Auto-subscribes token_ids during warmup, auto-unsubscribes on market cleanup. 9s PING heartbeat, auto-reconnect. FOK retry logic (+1tick/+2tick/80% size) now operates on fresher prices, expected to significantly improve fill rate.

- **v9.1**: Binance WebSocket real-time price stream + 3 bug fixes — Price data for warmup sampling, analysis, and position monitoring now uses WebSocket (`wss://stream.binance.com` @trade stream, ~10ms push) instead of REST polling (100-300ms per call). Shared `BinancePriceStream` singleton in `binance_api.py` with auto-reconnect and REST fallback. Eliminates ~215 REST calls/cycle. Bug fixes: (1) ATR<1.0 stop-loss now requires `direction_correct=False` — direction correct + ATR dip no longer triggers false stop-loss. (2) P0 take-profit consecutive failure cap: after 3 failed sell attempts with direction correct, stops retrying and waits for $1.00 settlement (orderbook has no buyers near expiry). (3) `_check_and_adjust_size` syncs `positions.jsonl` when on-chain balance < recorded size, preventing repeated size mismatch warnings. Monitor status line now shows crypto price and data source (WS/REST).

- **v9.0**: ATR 3-layer stop-loss redesign — Replaces old Token crash circuit breaker (60% drop) and late-loss circuit breaker (30%+wrong direction) with ATR-deviation-based decision matrix. When token drops ≥15% from entry: ATR≥2.0 (crypto far from PTB) → dip-buy 50% of original position at best_ask via FOK (max 1 per position, requires direction correct + >60s remaining); ATR 1.0-2.0 → hold and monitor; ATR<1.0 (crypto near PTB) → immediate stop-loss at best_bid. New -25% hard stop: unconditional market sell regardless of ATR or direction. New direction flip emergency exit: when `direction_correct` transitions True→False, immediately liquidates all holdings including dip-bought shares. All thresholds configurable via env (`PRICE_DROP_TRIGGER`, `PRICE_DROP_HARD_STOP`, `ATR_SAFE_THRESHOLD`, `ATR_DANGER_THRESHOLD`, `DIP_BUY_SIZE_RATIO`, `DIP_BUY_MIN_REMAINING`). Preserves P0 hyperbolic take-profit, early tolerance window, P1 hedge, and Stage 2/3/4 graduated exit.

- **v8.4**: Fast stop-loss + FOK entry + late-loss circuit breaker — HTTP timeout reduced from 5s to 3s (`FOK_TIMEOUT` env, saves 2s per failed order during stop-loss). Entry orders switched from GTC limit to FOK (fill-or-kill): no more pending/LIVE orders, either instant fill or instant fail. FOK orders now auto-retry on HTTP 425. `sell_position` adds ghost fill detection after FOK timeout (checks on-chain balance before retry). All 7 NO_BALANCE code paths now send Telegram notification (previously silently closed). New late-loss circuit breaker: when remaining ≤120s + direction wrong + loss >30% (`LATE_LOSS_THRESHOLD` env) → force immediate market sell (early stage skipped to avoid false triggers from thin liquidity). Token crash threshold adjusted: 50%→40% drop, absolute price check now relative to entry (50% of entry_price instead of fixed $0.10).
- **v8.3**: Balance unit fix + ghost fill detection fix — `get_balance()` and `get_token_balance()` now divide API response by 1e6 (Polymarket returns atomic USDC/token units with 6 decimals). Previously raw balance ~57M fed into Kelly produced `$1.7M dollar_amount` → always clamped to MAX_BET. Kelly sizing now works correctly with real dollar balance. Stop-loss ghost fill detection no longer bypassed on "not enough balance / allowance" errors — all FOK ERROR status now triggers on-chain balance recheck (~100ms, saves 3-4s of useless retries when tokens already sold). Added logging for allowance-refresh retry failures. Added post-reduction-loop final balance recheck as ghost fill safety net.
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
