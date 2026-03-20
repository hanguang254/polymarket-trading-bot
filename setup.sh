#!/bin/bash
# ──────────────────────────────────────────────
# Polymarket Trading Bot - 一键部署脚本
# 用法: bash setup.sh
# ──────────────────────────────────────────────
set -e

echo "🚀 Polymarket Trading Bot 部署开始"
echo ""

# 1. Python 依赖
echo "📦 安装 Python 依赖..."
pip install -r requirements.txt

# 2. Playwright 浏览器 + 系统依赖
echo "🌐 安装 Playwright Chromium + 系统依赖..."
playwright install chromium
playwright install-deps chromium

# 3. 创建日志目录
echo "📁 创建日志目录..."
mkdir -p logs

# 4. 检查 .env
if [ ! -f .env ]; then
    echo "⚠️  .env 文件不存在，从模板创建..."
    cp .env.example .env
    echo "   请编辑 .env 填入真实配置: nano .env"
else
    echo "✅ .env 已存在"
fi

# 5. 验证关键依赖
echo ""
echo "🔍 验证依赖..."
python3 -c "import playwright; print(f'  ✅ Playwright {playwright.__version__}')"
python3 -c "import py_clob_client; print('  ✅ py-clob-client')"
python3 -c "import websocket; print('  ✅ websocket-client')"
python3 -c "import dotenv; print('  ✅ python-dotenv')"

# 6. 测试 Playwright 浏览器启动
echo "🌐 测试 Chromium 启动..."
python3 -c "
from playwright.sync_api import sync_playwright
p = sync_playwright().start()
b = p.chromium.launch(headless=True, args=['--no-sandbox'])
b.close()
p.stop()
print('  ✅ Chromium 启动正常')
"

echo ""
echo "✅ 部署完成！"
echo "   启动: python3 auto_bot_v3.py"
echo "   监控: python3 position_monitor.py"
echo "   兑换: python3 auto_redeem_v2.py"
