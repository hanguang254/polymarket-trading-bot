# Polymarket Trading Bot v3.6

Automated trading bot for Polymarket 5-minute crypto UP/DOWN markets. Uses discount arbitrage with EV-driven position management, Base Rate calibration, Bayesian sequential updating, and correlated exposure control.

## Strategy

**Discount Arbitrage + EV-Driven Exit Protocol**

The bot does **not** predict market direction. Instead, it detects underpriced tokens via ATR-normalized deviation from PTB (Price-To-Beat), buys at a discount, and exits before settlement to lock arbitrage profit.

**Core insight**: Analysis of 889 trades shows price converges to PTB at close (deviation shrinks 91.7%, 74.5% within 0.01%). Real profit comes from temporary deviation, not direction calls.

### Execution Timeline (300s market)

```
0s    Market starts
2s    PTB acquisition (Playwright → HTML → Gamma API fallback)
20s   Bayesian warmup (price sampling every 5s)
40s   PTB deadline
100s  Bet window opens (80s observation data available)
160s  Bet window closes → Real-time EV monitoring starts
      ── Stages by time remaining ──
180s  Stage 1: Healthy liquidity (EV > 0.05 → hold)
120s  Stage 2: Declining liquidity (batch sell if EV ≤ 0.05)
60s   Stage 3: Aggressive exit (EV pricing floor protection)
30s   Stage 4: Last chance (EV > 0.05 → hold to settlement)
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
- **LMSR Liquidity Assessment**: Orderbook spread/depth/slippage scoring → dynamic discount threshold (8%-20%).
- **Correlated Exposure Control**: BTC/ETH correlation ~0.85. Same-direction position halves Kelly sizing.
- **1/4 Kelly Sizing**: Binary formula `f* = (p - price) / (1 - price)`, quarter Kelly, 5-10 shares hard bounds.

### Exit (P1 + P4)
- **Direction-Based Exit**: Uses crypto price vs PTB to determine win/loss (not token orderbook price, which can be misleading due to wide bid-ask spreads near settlement).
  - Direction correct → **Hold to settlement** (collect $1.00)
  - Direction wrong → Progressive stop-loss with price escalation
  - No buyers (bid < $0.05) → Skip futile sell attempts, wait for expiry
- **Wide Spread Protection**: `get_market_price()` detects bid-ask spread > 50% and falls back to midpoint API, preventing false loss signals (e.g., $0.01/$0.99 → midpoint $0.50 for a token worth $0.99).
- **Post-Close Safety**: No sell logic runs after market close (remaining < 0), preventing direction flicker from causing unwanted trades.
- **4-Stage Graduated Exit**: For losing positions — Stage 1 (180-120s), Stage 2 (120-60s) with batch orders, Stage 3 (60-30s) aggressive, Stage 4 (30-0s) floor prices.
- **Accurate Settlement**: Expiry cleanup uses crypto price vs PTB to determine $1.00/$0.00, not unreliable token price.

### Infrastructure
- **Warmup Observation**: 80s Bayesian sampling (20-100s window) with gap trend analysis
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

All must be met:

| Condition | Threshold | Source |
|-----------|-----------|--------|
| Discount | ≥ dynamic (8-20%) | LMSR liquidity score |
| EV | > 0.03 (3%) | `p_win - target_odds` |
| Odds | < 0.85 | Don't buy overpriced tokens |
| Confidence | ≥ 65% | Bayesian-fused momentum |
| Base Rate | Checked | < 0.55 → Kelly halved |
| Correlation | Checked | Same-direction → Kelly halved |

## Risk Controls

```
Layer 1 — Entry Filters
  ├─ Dynamic discount threshold (LMSR: 8%-20%)
  ├─ Strict binary EV > 3%
  ├─ Odds < 0.85, Confidence ≥ 65%
  └─ Base Rate calibration (weak edge → Kelly halved)

Layer 2 — Position Sizing
  ├─ 1/4 Kelly (binary formula)
  ├─ Base Rate reduction (× 0.5 if < 0.55)
  ├─ Correlation reduction (× 0.5 if same-direction open)
  ├─ Hard bounds: 5-10 shares
  └─ Balance constraints (10-20% of balance)

Layer 3 — Position Management
  ├─ Direction-based exit (correct → hold, wrong → stop-loss)
  ├─ Wide spread detection (prevents false loss signals)
  ├─ Post-close safety (no trades after market ends)
  ├─ 4-stage graduated closing (losing positions only)
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
