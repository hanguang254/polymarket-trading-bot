# Polymarket Trading Bot v2.1

Automated trading bot for Polymarket 5-minute crypto UP/DOWN markets using discount arbitrage strategy with 1/4 Kelly position sizing and 4-stage exit.

## Strategy

**Discount Arbitrage** (not direction prediction):

1. Calculate ATR deviation from Price-To-Beat (PTB)
2. Estimate token value based on deviation
3. Calculate discount: `estimated_value - market_odds`
4. Bet when discount ≥10% + momentum confirmation
5. Exit in 4 stages before market close

**Why?** 889 trades analysis shows price converges to PTB at close (deviation shrinks 91.7%). Real profit comes from buying undervalued tokens and exiting early.

## Features

- **Discount Arbitrage**: Token undervaluation detection
- **1/4 Kelly Sizing**: Conservative position management for 5-min markets
- **Liquidity Filter**: Dynamic threshold based on odds spread
- **4-Stage Exit**: Staged selling before market close (120s/90s/60s/30s)
- **Auto-Cleanup**: Expired positions auto-closed after 60s
- **Watchdog Daemon**: Background process auto-restarts crashed bots
- **Telegram Push**: Auto-push warmup data every 10 minutes
- **Gnosis Safe**: Secure wallet integration

## Architecture

```
auto_bot_v3.py          Main loop: scan markets → AI analysis → bet
ai_analyze_v2.py        Decision engine: discount calc + Kelly sizing
position_monitor.py     4-stage exit + auto-cleanup expired positions
watchdog_daemon.sh      Background daemon, restarts crashed processes
ai_trader/
  ai_model_v2.py        Token valuation model (conservative)
  binance_api.py        Binance K-line data
  indicators.py         ATR, EMA, RSI calculations
```

## Betting Conditions (all 4 required)

```
1. Discount ≥ 10%       (15% if low liquidity)
2. EV > 0.05            Positive expected value
3. Odds < 0.85          Don't buy expensive tokens
4. Confidence ≥ 50%     Momentum confirmation
```

## Token Valuation Model

```
ATR Deviation    Value     Meaning
> 3.0            $0.80     Huge lead
> 2.0            $0.72     Clear lead
> 1.5            $0.67     Moderate lead
> 1.0            $0.62     Small lead
> 0.7            $0.58     Slight lead
> 0.5            $0.55     Weak lead
≤ 0.5            $0.51     Flat, skip
```

## Position Sizing (1/4 Kelly)

```python
p_win = 0.5 + (confidence * 0.3)  # 50%-80%
kelly_full = (p * b - q) / b
kelly_quarter = kelly_full * 0.25
# Min 5 shares (Polymarket minimum)
```

## 4-Stage Exit

```
Stage 1 (120-90s)  Sell high if winning, stop-loss if losing
Stage 2 (90-60s)   Batch selling, confirm fills
Stage 3 (60-30s)   Aggressive multi-price ladder
Stage 4 (30-0s)    Floor price $0.01 clearance
```

## Setup

```bash
# Install
npm install -g @polymarket/clob-client
pip install requests playwright pandas numpy
playwright install chromium

# Configure wallet (NEVER commit keys!)
polymarket config set-key <your_private_key>
polymarket config set-safe <your_safe_address>
```

## Run

```bash
# Start bot
nohup python3 -u auto_bot_v3.py > logs/bot_v3.log 2>&1 &

# Start position monitor
nohup python3 -u position_monitor.py > logs/position_monitor.log 2>&1 &

# Start watchdog daemon
nohup ./watchdog_daemon.sh > /tmp/watchdog_daemon.log 2>&1 &
```

## Monitoring

```bash
# Check processes
ps aux | grep -E "(auto_bot_v3|position_monitor|watchdog)"

# View decisions
tail -f logs/decisions_v2.jsonl | jq .

# Check balance
polymarket clob balance --asset-type collateral --signature-type gnosis-safe
```

## Security

⚠️ **NEVER upload private keys to git!**
- `.gitignore` excludes: `.env`, `wallet_backup.txt`, `tg_push.sh`
- Use Polymarket CLI config for wallet setup
- Review files before committing

## Version History

- **v2.1.1** (2026-03-09): Fix min 5 shares, auto-cleanup expired positions, liquidity filter, 1/4 Kelly
- **v2.1.0** (2026-03-09): Discount arbitrage strategy, 4-stage exit
- **v2.0.0** (2026-03-07): Direction prediction strategy
- **v1.0.0** (2026-03-05): Initial release

## License

MIT

## Disclaimer

Educational purposes only. Crypto trading involves risk. Use at your own risk.
