#!/usr/bin/env bash
# Watchdog for Polymarket Trading Bot
# 监控 auto_bot_v3.py 和 position_monitor.py

set -u

# 默认使用脚本所在目录；也可通过 BOT_DIR 显式指定
SCRIPT_DIR="${BOT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/watchdog.log"

# 可通过环境变量调整“卡死”判定阈值（秒）
AUTO_STALE_SEC="${AUTO_STALE_SEC:-300}"
MON_STALE_SEC="${MON_STALE_SEC:-300}"

AUTO_LOG_FILE="$LOG_DIR/polymarket-bot.log"
MON_LOG_FILE="$LOG_DIR/position_monitor.log"

mkdir -p "$LOG_DIR"
cd "$SCRIPT_DIR" || exit 1

timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

log() {
    echo "[$(timestamp)] $*" >> "$LOG_FILE"
}

is_running() {
    local script="$1"
    pgrep -f "python3.*${script}" > /dev/null
}

last_update_age() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo 999999
        return
    fi
    local now
    local mtime
    now="$(date +%s)"
    mtime="$(date -r "$file" +%s)"
    echo $((now - mtime))
}

restart() {
    local script="$1"
    local out_log="$2"
    log "重启 ${script} ..."
    nohup python3 -u "$script" >> "$out_log" 2>&1 &
    log "已重启 ${script} (PID: $!)"
}

check_proc() {
    local script="$1"
    local out_log="$2"
    local stale_sec="$3"

    if ! is_running "$script"; then
        log "${script} 未运行，正在启动..."
        restart "$script" "$out_log"
        return
    fi

    local age
    age="$(last_update_age "$out_log")"
    if [ "$age" -ge "$stale_sec" ]; then
        log "${script} 可能卡死：日志 ${age}s 未更新，正在重启..."
        pkill -f "python3.*${script}" || true
        sleep 2
        restart "$script" "$out_log"
    fi
}

check_proc "auto_bot_v3.py" "$AUTO_LOG_FILE" "$AUTO_STALE_SEC"
check_proc "position_monitor.py" "$MON_LOG_FILE" "$MON_STALE_SEC"
