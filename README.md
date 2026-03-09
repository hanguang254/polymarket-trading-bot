# Polymarket Trading Bot v3.2

Automated trading bot for Polymarket 5-minute crypto UP/DOWN markets using discount arbitrage strategy with warmup observation, trend confirmation, real-time monitoring, and auto settlement.

## Strategy

**Discount Arbitrage + Trend Confirmation**:

1. 60s warmup observation (collect Binance price samples)
2. Trend confirmation (strong/medium/oscillating)
3. Calculate ATR deviation from Price-To-Beat (PTB)
4. Estimate token value based on deviation
5. Calculate discount: `estimated_value - market_odds`
6. Bet when discount ≥10% (≥15% for oscillating) + momentum confirmation
7. Real-time TP/SL monitoring after entry
8. 4-stage exit before market close
9. Auto redeem settled positions (fallback)

**Why?** 889 trades analysis shows price converges to PTB at close (deviation shrinks 91.7%). Real profit comes from buying undervalued tokens.

## Architecture

```
systemd services (auto-restart, boot-start):

polymarket-bot.service     → auto_bot_v3.py (betting engine)
polymarket-monitor.service → position_monitor.py (position monitor)
polymarket-redeem.service  → auto_redeem.py (auto settlement, 30min)
```

## Features

- **Discount Arbitrage**: Token undervaluation detection
- **Warmup Observation**: 60s price sampling before betting
- **Trend Confirmation**: Strong/medium/oscillating classification
- **1/4 Kelly Sizing**: Conservative position management (5-10 shares)
- **Real-time TP/SL**: Take profit 8%/15%, stop loss 10%, trend reversal 3%
- **4-Stage Exit**: 180s→120s→60s→30s graduated closing
- **Auto Redeem**: Claim settled winning positions via CLI (every 30min)
- **Telegram Notifications**: Bets, exits, settlements, errors
- **systemd Management**: Auto-restart on crash, boot-start

## Components

| File | Role |
|------|------|
| `auto_bot_v3.py` | Main betting engine (v3.1 warmup + trend) |
| `ai_analyze_v2.py` | AI analysis (ATR/EMA/RSI/Volume) |
| `position_monitor.py` | Real-time TP/SL + 4-stage exit |
| `auto_redeem.py` | Auto claim settled positions (CLI-based) |
| `ai_trader/ai_model_v2.py` | Valuation model |
| `ai_trader/binance_api.py` | Binance data source |
| `ai_trader/indicators.py` | Technical indicators |
| `ai_trader/playwright_ptb.py` | PTB extraction via browser |

## Timeline (5-min market = 300s)

```
0s    Market starts
60s   Warmup begins (price sampling every 5s)
120s  Bet window opens (trend confirmed)
      → Real-time monitoring starts (TP/SL every 3s)
180s  Stage 1: Healthy liquidity exit
120s  Stage 2: Declining liquidity (multi-price orders)
60s   Stage 3: Aggressive exit
30s   Stage 4: Floor price ($0.01)
0s    Market ends
-30s  Auto cleanup expired positions
```

## Betting Conditions (all must be met)

| Condition | Normal | Oscillating |
|-----------|--------|-------------|
| Discount | ≥ 10% | ≥ 15% |
| EV | > 0.05 | > 0.05 |
| Odds | < 0.85 | < 0.85 |
| Confidence | ≥ 50% | ≥ 50% |

## Setup

1. Install dependencies: `pip install requests`
2. Install Playwright: `playwright install chromium`
3. Install Polymarket CLI: [polymarket-rs](https://github.com/polymarket/polymarket-rs)
4. Configure `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your_bot_token
   ```
5. Configure Polymarket CLI with your private key
6. Create systemd services (see `systemd/` examples)
7. Start: `systemctl start polymarket-bot polymarket-monitor polymarket-redeem`

## Commands

```bash
# Check status
systemctl status polymarket-bot
systemctl status polymarket-monitor
systemctl status polymarket-redeem

# View logs
journalctl -u polymarket-bot -f
tail -f logs/bot_v3.log
tail -f logs/auto_redeem.log

# Restart
systemctl restart polymarket-bot

# Check balance
polymarket clob balance --asset-type collateral --signature-type gnosis-safe
```

## Version History

- **v3.2**: Auto redeem + systemd + stability fixes
- **v3.1**: Warmup observation + trend confirmation + real-time TP/SL
- **v2.1**: Discount arbitrage + Kelly sizing + liquidity filter
- **v2.0**: Direction prediction strategy
- **v1.0**: Initial version

## License

MIT
