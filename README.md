# Polymarket Trading Bot v6.0

Automated trading bot for Polymarket 5-minute crypto UP/DOWN markets. Uses EV-driven entry/exit with Bayesian sequential updating, LMSR theoretical pricing, market-price stop-loss, and correlated exposure control.

## Strategy

**EV-Driven Entry + Market-Price Stop-Loss**

The bot uses Bayesian sequential updating to detect directional signals, enters when EV (p_win - execution_price) is positive with sufficient confidence, and manages positions with full-duration market-price stop-loss.

### Execution Timeline (300s market)

```
0s    Market starts
2s    PTB acquisition (Playwright → HTML → Gamma API fallback)
20s   Bayesian warmup (5s intervals, 3s after 80s)
40s   PTB deadline
100s  Bet window opens (80s observation data available)
160s  Bet window closes → Real-time EV monitoring starts
      ── Exit Protocol ──
>30s  Universal stop-loss: direction wrong → market-price ladder sell
>30s  P1 hedge: bid < $0.05 → buy opposite token for $1 pair
>90s  P0 take-profit: hyperbolic discounting threshold
120s  Stage 2: Weak signal exit (batch sell)
60s   Stage 3: Aggressive exit (EV floor protection)
30s   Stage 4: EV ≥ 0 → hold to settlement, EV < 0 → floor prices
0s    Market ends → Settlement
```

## Architecture

```
systemd services (auto-restart, boot-start):

polymarket-bot.service     → auto_bot_v3.py      (betting engine)
polymarket-monitor.service → position_monitor.py  (EV-driven position monitor)
polymarket-redeem.service  → auto_redeem_v2.py    (auto settlement + balance query)
```

## Features

### Entry (P0 + P2 + P3)
- **Base Rate Calibration**: Conservative ATR-band priors (0.50-0.85), auto-calibrates with empirical data after 30+ samples per band. Weak edge (base_rate < 0.55) halves Kelly.
- **Strict Binary EV**: `EV = p_win - price` (replaces discount/odds ratio). Minimum 3% edge required.
- **Bayesian Fusion**: Base rate (40%) + Bayesian posterior (60%) when direction-aligned with confidence > 30%.
- **Cross-Validation**: Flags overestimation when `estimated_value > p_win + 0.15` (reduces confidence 15%).
- **LMSR Inefficiency Signal**: When realtime `best_ask` diverges >10% from `p_win`, lowers discount threshold by 2% (min 6%) for easier entry on mispriced markets.
- **LMSR Liquidity Assessment**: Orderbook spread/depth/slippage scoring → dynamic discount threshold (8%-20%).
- **Exit Liquidity Gate (P2)**: Before entering, checks bid-side depth of the chosen token. `bid_depth < 5` → skip entry entirely. Prevents entering positions that can't be exited.
- **Correlated Exposure Control**: BTC/ETH correlation ~0.85. Same-direction position halves Kelly sizing.
- **1/4 Kelly Sizing**: Binary formula `f* = (p - price) / (1 - price)`, quarter Kelly, 5-10 shares hard bounds.
- **Liquidity-Capped Sizing (P3)**: Kelly size capped at 50% of exit bid depth. Works with P2 — P2 gates entry, P3 adjusts size.
- **CLOB C1 Calibration**: Replaces Gamma odds with CLOB `best_ask` for discount/EV/price checks — prevents buying at $0.85 while thinking price is $0.50. Empty book detection (ask ≥ 0.95) falls back to `last-trade-price`.
- **Empty Book Override**: When C1 calibration makes discount negative (stale last-trade-price > estimated_value), a second-chance check uses Gamma odds: if `gamma_discount ≥ threshold`, `gamma_EV > 0.05`, and `confidence ≥ 75%`, overrides to BET. Execution still uses calibrated price (last-trade-price + SLIPPAGE).

### Exit (P0-P1 + P4)
- **Hyperbolic Discounting Profit-Take (P0)**: Dynamic threshold `base × (1 + k × minutes_remaining)` — faraway paper profits are less reliable, so the further from settlement, the higher the profit bar. Configurable via `P0_BASE_PROFIT` (default 0.15) and `P0_HYPERBOLIC_K` (default 0.15). Example: 240s → 26%, 100s → 20%.
- **Opposite Token Hedge (P1)**: When losing and bid < $0.05 (no buyers), buys the opposite token to form a guaranteed pair (UP + DOWN = $1.00 at settlement). Only hedges when `opposite_ask < (1.00 - entry_price - 0.02)`, ensuring net profit. Opposite token_id is recorded at entry time.
- **EV-Driven Diamond Hands**: Direction correct + ≤120s remaining → calculates real-time EV. High EV (>0.03) or large ATR deviation (≥1.0) → hold to settlement. Weak EV (0~0.03) → releases to Stage 3/4 fine-grained EV strategy instead of blindly holding.
- **Direction-Based Exit**: Uses crypto price vs PTB to determine win/loss (not token orderbook price, which can be misleading due to wide bid-ask spreads near settlement).
  - Direction correct + EV > 0.03 or strong ATR → **hold to settlement** ($1.00)
  - Direction correct but weak signal → releases to stage exit strategy
  - Direction wrong → **Universal market-price stop-loss** (bid→95%→90%→80%→$0.05→$0.01, ~6s to clear)
  - No buyers (bid < $0.05) → Attempt hedge (P1), fall back to wait for expiry
  - Safety: direction correct but token drops >10% → downgrade to "unknown", trigger stop-loss
- **Wide Spread Protection**: `get_market_price()` detects bid-ask spread > 50% and falls back to midpoint API → `last-trade-price` → single-side price, preventing false loss signals.
- **Empty Book Fallback (Monitor)**: `get_best_bid()` / `get_best_ask()` fall back to `last-trade-price ± SLIPPAGE` when CLOB is empty (bid ≤ 0.02 or ask ≥ 0.95). Enables hedge and stop-loss decisions with real market prices instead of $0.01/$0.99.
- **EV Sanity Check (Stage 3/4)**: When EV says "hold" but `last-trade-price < entry_price × 0.5` (market price halved), overrides EV to negative — prevents holding to settlement on fake positive EV.
- **Post-Close Safety**: No sell logic runs after market close (remaining < 0), preventing direction flicker from causing unwanted trades.
- **3-Stage Graduated Exit**: For remaining edge cases — Stage 2 (120-60s) with batch orders, Stage 3 (60-30s) aggressive, Stage 4 (30-0s) EV≥0 hold / EV<0 floor prices.
- **Accurate Settlement**: Expiry cleanup uses crypto price vs PTB to determine $1.00/$0.00, not unreliable token price.

### Infrastructure
- **Adaptive Warmup Sampling**: 5s intervals during 20-80s, accelerates to 3s during 80-100s (near bet window) for sharper Bayesian posterior
- **Trend Safety Valve**: Gap expanding/shrinking/crossing/oscillating → adjusts min discount
- **Network Circuit Breaker**: 5 consecutive API failures → 300s pause
- **Playwright Smart Degradation**: Skip browser PTB after 3 consecutive failures
- **Outcome Learning Loop**: Every close records outcome → auto-calibrates base rates every 50 trades
- **Telegram Notifications**: Bets, exits, settlements, errors, balance
- **Auto Redeem**: Claim settled positions (configurable interval via `REDEEM_INTERVAL`), shows USDC balance after each round
- **systemd Management**: Auto-restart on crash, boot-start

## Components

| File | Role |
|------|------|
| `auto_bot_v3.py` | Main engine: market discovery, warmup, betting, correlation control |
| `ai_analyze_v2.py` | Decision engine: strict EV, Kelly sizing, Bayesian fusion, bet execution |
| `ai_trader/ai_model_v2.py` | Scoring model: ATR deviation → token value estimation → discount |
| `ai_trader/base_rate.py` | Base Rate calibration: conservative priors + empirical learning |
| `scripts/validate_base_rate.py` | Base Rate calibration validator: ATR-band win rates vs priors |
| `ai_trader/bayesian_engine.py` | Bayesian sequential updater: sigmoid likelihood, log-space, anti-saturation |
| `ai_trader/lmsr_liquidity.py` | Orderbook liquidity: spread + depth + slippage → dynamic threshold |
| `position_monitor.py` | EV-driven exit + 4-stage closing + outcome recording |
| `auto_redeem_v2.py` | Auto claim settled positions (Polymarket CLI) |
| `ai_trader/binance_api.py` | Binance market data (klines, price, 24h stats) |
| `ai_trader/indicators.py` | Technical indicators (EMA, RSI, ATR, Bollinger Bands) |
| `ai_trader/polymarket_api.py` | Polymarket API + PTB HTML scraper |
| `ai_trader/playwright_ptb.py` | PTB extraction via headless Chromium |
| `trading_state.py` | State management: cooldown, daily PnL, win/loss tracking |

## Betting Conditions

All must be met (all configurable via `.env`):

| Condition | Threshold | Env Key | Source |
|-----------|-----------|---------|--------|
| EV | > 0.03 (3%) | `MIN_EV` | `p_win - exec_price` |
| Confidence | ≥ 60% | `MIN_CONFIDENCE` | Bayesian-fused momentum |
| Odds | < 0.92 | `MAX_BUY_PRICE` | Don't buy overpriced tokens |
| Base Rate | Checked | — | < 0.55 → Kelly halved |
| Correlation | Checked | — | Same-direction → Kelly halved |

## Risk Controls

```
Layer 1 — Entry Filters
  ├─ EV > MIN_EV (default 3%, env configurable)
  ├─ Confidence ≥ MIN_CONFIDENCE (default 60%, env configurable)
  ├─ Odds < MAX_BUY_PRICE (default 0.92, env configurable)
  ├─ Base Rate calibration (weak edge → Kelly halved)
  ├─ P2: Exit liquidity gate (bid_depth < 5 → skip entry)
  └─ Empty book override (Gamma EV pass → bet with calibrated price)

Layer 2 — Position Sizing
  ├─ 1/4 Kelly (binary formula)
  ├─ Base Rate reduction (× 0.5 if < 0.55)
  ├─ Correlation reduction (× 0.5 if same-direction open)
  ├─ P3: Liquidity cap (≤ 50% of exit bid depth)
  ├─ Hard bounds: 5-10 shares
  └─ Balance constraints (10-20% of balance)

Layer 3 — Position Management
  ├─ P0: Hyperbolic discounting profit-take (dynamic threshold, configurable)
  ├─ P1: Opposite token hedge (bid < $0.05 → buy opposite for $1 pair)
  ├─ Universal stop-loss (direction wrong → market-price ladder, full duration >30s)
  ├─ Wide spread detection (prevents false loss signals)
  ├─ Post-close safety (no trades after market ends)
  ├─ 3-stage graduated closing (Stage 2/3/4 for remaining edge cases)
  └─ Accurate settlement ($1/$0 from crypto price vs PTB)

Layer 4 — System Protection
  ├─ Max open positions: 2 (configurable)
  ├─ Daily loss limit: $10 (configurable)
  ├─ Circuit breaker: 5 failures → 300s pause
  ├─ Loss cooldown: 3 periods after failed bet
  └─ Min balance check: $5
```

## Polymarket CLI

This bot uses [Polymarket CLI](https://github.com/Polymarket/polymarket-cli) for all on-chain operations:

```bash
# Order placement
polymarket clob create-order --token <id> --side buy/sell --price <p> --size <n> --signature-type eoa

# Balance check
polymarket clob balance --asset-type collateral --signature-type eoa

# Redemption
polymarket ctf redeem --condition <id> --signature-type eoa
polymarket ctf redeem-neg-risk --condition <id> --amounts <a1>,<a2> --signature-type eoa

# Position query
polymarket data positions <wallet> -o json

# Order cancellation
polymarket clob cancel-all --token <id> --signature-type eoa
```

## Setup

### Prerequisites

- Python 3.10+
- [Polymarket CLI](https://github.com/Polymarket/polymarket-cli) installed and configured
- Chromium browser (for Playwright PTB extraction)

### Installation

```bash
git clone https://github.com/youruser/polymarket-trading-bot.git
cd polymarket-trading-bot

pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# Edit .env with your wallet addresses and Telegram token

polymarket setup  # One-time CLI setup
```

### Environment Variables

See `.env.example` for all configurable parameters:

| Variable | Required | Description |
|----------|----------|-------------|
| `EOA_WALLET` | Yes | EOA wallet address for signing |
| `PROXY_WALLET` | Yes | Polymarket proxy wallet (holds positions) |
| `SIGNATURE_TYPE` | Yes | Signature type (`eoa` or `gnosis-safe`) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | No | Telegram chat ID for notifications |
| `REDEEM_INTERVAL` | No | Auto redeem polling interval in seconds (default: 600) |
| `MAX_DAILY_LOSS` | No | Daily loss limit in USD (default: 10) |
| `MAX_OPEN_POSITIONS` | No | Max concurrent positions (default: 2) |
| `MIN_BALANCE` | No | Min balance to place bets (default: 5) |
| `MAX_BUY_PRICE` | No | Max buy price for entry (default: 0.92) |
| `MIN_EV` | No | Min EV to place bet (default: 0.03) |
| `MIN_CONFIDENCE` | No | Min confidence to place bet (default: 0.60) |
| `SLIPPAGE` | No | Empty book fallback slippage for last-trade-price ± (default: 0.01) |
| `P0_BASE_PROFIT` | No | P0 take-profit base threshold (default: 0.15) |
| `P0_HYPERBOLIC_K` | No | Hyperbolic discounting coefficient (default: 0.15) |

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

# Check balance
polymarket clob balance --asset-type collateral --signature-type eoa

# View positions
polymarket data positions <your-proxy-wallet> -o json

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
| `logs/polymarket-bot.log` | Application log |

## Version History

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
