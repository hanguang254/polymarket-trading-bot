# Polymarket Trading Bot v3.3

Automated trading bot for Polymarket 5-minute crypto UP/DOWN markets using discount arbitrage strategy with Bayesian sequential updating, LMSR liquidity assessment, real-time monitoring, and auto settlement via Polymarket CLI.

## Strategy

**Discount Arbitrage + Bayesian Confirmation**:

1. PTB (Price-To-Beat) early acquisition (2-40s)
2. Bayesian sequential warmup (20-100s, ~16 samples)
3. Gap trend safety valve (expanding/shrinking/crossing/oscillating)
4. ATR deviation analysis + token value estimation
5. LMSR orderbook liquidity assessment → dynamic discount threshold
6. Bet when discount ≥ threshold + EV > 0.1 + Bayesian confidence ≥ 65%
7. 1/4 Kelly position sizing (binary market formula)
8. Real-time TP/SL monitoring after entry
9. 4-stage exit before market close
10. Auto redeem settled positions via Polymarket CLI

**Core insight**: 889 trades analysis shows price converges to PTB at close (deviation shrinks 91.7%). Real profit comes from buying undervalued tokens and selling before settlement.

## Architecture

```
systemd services (auto-restart, boot-start):

polymarket-bot.service     → auto_bot_v3.py      (betting engine)
polymarket-monitor.service → position_monitor.py  (position monitor)
polymarket-redeem.service  → auto_redeem_v2.py    (auto settlement, 30min)
```

## Features

- **Bayesian Sequential Updating**: Real-time posterior probability estimation replacing simple gap trend analysis
- **LMSR Liquidity Assessment**: Orderbook-based spread/depth/slippage scoring for dynamic discount thresholds
- **Corrected Kelly Sizing**: Binary market formula `f* = (p - price) / (1 - price)`, 1/4 Kelly (5-10 shares)
- **Discount Arbitrage**: Token undervaluation detection via ATR-normalized deviation
- **Warmup Observation**: 80s Bayesian sampling before betting (20-100s window)
- **Trend Confirmation**: Gap trend as safety valve (crossing = skip unless Bayesian conf ≥ 60%)
- **Real-time TP/SL**: Take profit 8%/15%, stop loss 10%, trend reversal 3%
- **4-Stage Exit**: 180s → 120s → 60s → 30s graduated closing with liquidity-aware pricing
- **Auto Redeem**: Claim settled winning positions via Polymarket CLI (every 30min)
- **Network Circuit Breaker**: Auto-pause 5min after 5 consecutive API failures
- **Playwright Smart Degradation**: Skip browser PTB after 3 consecutive failures
- **Telegram Notifications**: Bets, exits, settlements, errors
- **systemd Management**: Auto-restart on crash, boot-start

## Components

| File | Role |
|------|------|
| `auto_bot_v3.py` | Main betting engine (v3.3 Bayesian + LMSR) |
| `ai_analyze_v2.py` | AI decision engine + Kelly sizing + bet execution |
| `ai_trader/ai_model_v2.py` | Valuation model (ATR deviation → estimated value) |
| `ai_trader/bayesian_engine.py` | Bayesian sequential updater (sigmoid likelihood, anti-saturation) |
| `ai_trader/lmsr_liquidity.py` | Orderbook liquidity assessment (spread + depth + slippage) |
| `position_monitor.py` | Real-time TP/SL + 4-stage exit |
| `auto_redeem_v2.py` | Auto claim settled positions (Polymarket CLI) |
| `ai_trader/binance_api.py` | Binance data source |
| `ai_trader/indicators.py` | Technical indicators (EMA, RSI, ATR) |
| `ai_trader/polymarket_api.py` | Polymarket API + PTB HTML scraper |
| `ai_trader/playwright_ptb.py` | PTB extraction via Playwright browser |
| `trading_state.py` | Trading state management (cooldown, daily PnL) |

## Timeline (5-min market = 300s)

```
0s    Market starts
2s    PTB acquisition begins (Playwright → HTML → Gamma API)
20s   Bayesian warmup begins (price sampling every 5s)
40s   PTB acquisition deadline
100s  Bet window opens (Bayesian posterior + gap trend confirmed)
160s  Bet window closes
      → Real-time monitoring starts (TP/SL every 2s)
180s  Stage 1: Healthy liquidity exit (TP ≥10%, winning/losing detection)
120s  Stage 2: Declining liquidity (batch selling, multi-price orders)
60s   Stage 3: Aggressive exit (smart sell + multi-price gradient)
30s   Stage 4: Floor price ($0.01)
0s    Market ends
-30s  Auto cleanup expired positions
```

## Betting Conditions (all must be met)

| Condition | Threshold |
|-----------|-----------|
| Discount | ≥ dynamic (8-20% based on liquidity) |
| EV | > 0.1 |
| Odds | < 0.85 |
| Confidence | ≥ 65% (Bayesian-fused) |

## Polymarket CLI

This bot uses [Polymarket CLI](https://github.com/Polymarket/polymarket-cli) for:

- **Order placement**: `polymarket clob create-order --token <id> --side buy/sell --price <p> --size <n>`
- **Balance check**: `polymarket clob balance --asset-type collateral --signature-type eoa`
- **Auto redemption**: `polymarket ctf redeem --condition <id> --signature-type eoa`
- **Neg-risk redemption**: `polymarket ctf redeem-neg-risk --condition <id> --amounts <a1>,<a2>`
- **Position query**: `polymarket data positions <wallet> -o json`
- **Order cancellation**: `polymarket clob cancel-all --token <id>`

## Setup

### Prerequisites

- Python 3.10+
- [Polymarket CLI](https://github.com/Polymarket/polymarket-cli) installed and configured with your private key
- Chromium browser (for Playwright PTB extraction)

### Installation

```bash
git clone https://github.com/youruser/polymarket-trading-bot.git
cd polymarket-trading-bot

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium

# Configure environment
cp .env.example .env
# Edit .env with your wallet addresses and Telegram token

# Configure Polymarket CLI (one-time setup)
polymarket setup
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
| `MAX_DAILY_LOSS` | No | Max daily loss limit in USD (default: 10) |
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

## Commands

```bash
# Check status
systemctl status polymarket-bot
systemctl status polymarket-monitor
systemctl status polymarket-redeem

# View logs
journalctl -u polymarket-bot -f
tail -f logs/polymarket-bot.log

# Restart
systemctl restart polymarket-bot

# Check balance
polymarket clob balance --asset-type collateral --signature-type eoa

# View positions
polymarket data positions <your-proxy-wallet> -o json
```

## Version History

- **v3.3**: Bayesian sequential updating, LMSR liquidity assessment, corrected Kelly formula, network circuit breaker, Playwright smart degradation, anti-saturation fix, ATR window expansion (30 K-lines), pre-order persistence, CLI-based auto redemption
- **v3.2**: Auto redeem + systemd + stability fixes
- **v3.1**: Warmup observation + trend confirmation + real-time TP/SL
- **v2.1**: Discount arbitrage + Kelly sizing + liquidity filter
- **v2.0**: Direction prediction strategy
- **v1.0**: Initial version

## License

MIT
