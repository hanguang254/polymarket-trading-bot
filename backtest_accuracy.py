#!/usr/bin/env python3
"""
Compatibility wrapper.

Direction accuracy is no longer the primary backtest metric for this bot.
Use the PnL-based report from backtest.py instead.
"""
from backtest import main


if __name__ == "__main__":
    main()
