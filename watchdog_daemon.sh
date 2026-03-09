#!/bin/bash
# 常驻看门狗进程

SCRIPT_DIR="/root/.openclaw/workspace/polymarket-arb-bot"
LOG_DIR="$SCRIPT_DIR/logs"

# 加载环境变量
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(cat "$SCRIPT_DIR/.env" | xargs)
fi

while true; do
    # 检查 auto_bot_v3.py
    if ! pgrep -f "auto_bot_v3.py" > /dev/null; then
        echo "[$(date)] auto_bot_v3.py 已停止，重启中..." >> /tmp/watchdog.log
        cd "$SCRIPT_DIR"
        nohup python3 -u auto_bot_v3.py >> "$LOG_DIR/bot_v3.log" 2>> "$LOG_DIR/bot_v3_err.log" &
    fi
    
    # 检查 position_monitor.py
    if ! pgrep -f "position_monitor.py" > /dev/null; then
        echo "[$(date)] position_monitor.py 已停止，重启中..." >> /tmp/watchdog.log
        cd "$SCRIPT_DIR"
        nohup python3 -u position_monitor.py >> "$LOG_DIR/position_monitor.log" 2>> "$LOG_DIR/position_monitor_err.log" &
    fi
    
    # 每60秒检查一次
    sleep 60
done
