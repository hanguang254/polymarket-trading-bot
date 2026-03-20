"""
币种配置中心 — 所有支持的交易对集中管理

通过 .env ENABLED_COINS 控制启用哪些币种（逗号分隔，默认 BTC,ETH）
"""
import os

# 全部支持的币种配置
ALL_COINS = {
    "BTC": {
        "binance_symbol": "BTCUSDT",
        "binance_ws": "btcusdt@trade",
        "chainlink": "btc/usd",
        "pyth_feed": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
        "slug_prefix": "btc-updown-5m",
    },
    "ETH": {
        "binance_symbol": "ETHUSDT",
        "binance_ws": "ethusdt@trade",
        "chainlink": "eth/usd",
        "pyth_feed": "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
        "slug_prefix": "eth-updown-5m",
    },
    "BNB": {
        "binance_symbol": "BNBUSDT",
        "binance_ws": "bnbusdt@trade",
        "chainlink": "bnb/usd",
        "pyth_feed": "2f95862b045670cd22bee3114c39763a4a08beeb663b145d283c31d7d1101c4f",
        "slug_prefix": "bnb-updown-5m",
    },
}


def get_enabled_coins():
    """从 .env 读取启用的币种列表"""
    raw = os.environ.get("ENABLED_COINS", "BTC,ETH")
    return [c.strip().upper() for c in raw.split(",") if c.strip().upper() in ALL_COINS]


def get_coins_config():
    """返回启用币种的配置 dict"""
    return {coin: ALL_COINS[coin] for coin in get_enabled_coins()}


def coin_from_slug(slug):
    """从 slug 中提取币种名称（如 btc-updown-5m-xxx → BTC）"""
    slug_lower = (slug or "").lower()
    for coin, cfg in ALL_COINS.items():
        if slug_lower.startswith(cfg["slug_prefix"]):
            return coin
    # fallback: 检查是否包含币种关键词
    for coin in ALL_COINS:
        if coin.lower() in slug_lower:
            return coin
    return "BTC"  # 最终兜底


def get_binance_symbol(coin):
    """获取 Binance 交易对符号"""
    cfg = ALL_COINS.get(coin)
    return cfg["binance_symbol"] if cfg else f"{coin}USDT"
