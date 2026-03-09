# Polymarket Trading Bot v2.1

Automated trading bot for Polymarket 5-minute crypto UP/DOWN markets using discount arbitrage strategy.

## Features

- **Discount Arbitrage Strategy**: Calculate token undervaluation and bet when discount ≥10%
- **4-Stage Exit**: Exit positions before market close (don't wait for settlement)
- **AI-Powered Analysis**: Binance technical analysis (ATR, momentum, volume)
- **Gnosis Safe Support**: Secure wallet integration
- **Auto-Restart**: Watchdog monitors and restarts crashed processes

## Strategy

v2.1 uses discount arbitrage instead of direction prediction:

1. Calculate ATR deviation from Price-To-Beat (PTB)
2. Estimate token value based on deviation
3. Calculate discount: `value - market_odds`
4. Bet when discount ≥10% + momentum confirmation
5. Exit in 4 stages (120s/90s/60s/30s before close)

**Why it works:** 889 trades analysis shows price converges to PTB at close (deviation shrinks 91.7%). Real profit comes from buying undervalued tokens and exiting early.

## Requirements

```bash
# Polymarket CLI
npm install -g @polymarket/clob-client

# Python dependencies
pip install requests playwright pandas numpy

# Playwright browsers
playwright install chromium
```

## Setup

### 1. Configure Wallet

⚠️ **NEVER commit private keys to git!**

```bash
# Option 1: Polymarket CLI config (recommended)
polymarket config set-key <your_private_key>
polymarket config set-safe <your_gnosis_safe_address>

# Option 2: Environment variables
echo "PRIVATE_KEY=your_key_here" > .env
echo "GNOSIS_SAFE_ADDRESS=your_safe_here" >> .env
```

### 2. Run Bot

```bash
# Start main bot
nohup python3 -u auto_bot_v3.py > logs/auto_bot.log 2>&1 &

# Start position monitor
nohup python3 -u position_monitor.py > logs/position_monitor.log 2>&1 &

# Setup watchdog (auto-restart)
crontab -e
# Add: * * * * * /path/to/watchdog_v3.sh >> /tmp/watchdog.log 2>&1
```

## Configuration

Edit `ai_analyze_v2.py` to adjust betting conditions:

```python
should_bet = (
    discount >= 0.10       # Discount threshold (10%)
    and ev > 0.05          # Positive expected value
    and target_odds < 0.85 # Don't buy expensive tokens
    and confidence >= 0.50 # Momentum confirmation
)
```

## Monitoring

```bash
# Check processes
ps aux | grep -E "(auto_bot_v3|position_monitor)"

# View decisions
tail -f logs/decisions_v2.jsonl | jq .

# Check balance
polymarket clob balance --asset-type collateral --signature-type gnosis-safe
```

## Files

- `auto_bot_v3.py` - Main bot (market scanning + AI analysis)
- `ai_analyze_v2.py` - Decision engine (discount calculation)
- `position_monitor.py` - 4-stage exit strategy
- `watchdog_v3.sh` - Auto-restart watchdog
- `ai_trader/` - AI model + Binance API

## Performance

- **v2.0**: 20.7% bet frequency (2.5 times/hour)
- **v2.1**: 33.3% bet frequency (4 times/hour, +60%)

## Security

⚠️ **Important:**
- Never upload private keys to git
- Use `.env` file (already in .gitignore)
- Check for sensitive data before committing
- Review `.gitignore` before pushing

## License

MIT

## Disclaimer

This bot is for educational purposes. Cryptocurrency trading involves risk. Use at your own risk.
