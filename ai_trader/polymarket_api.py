import json
import re
from datetime import datetime, timezone

import requests


CRYPTO_PRICE_API = "https://polymarket.com/api/crypto/crypto-price"
EVENT_API = "https://gamma-api.polymarket.com/events"
MARKET_WINDOW_SECONDS = 300
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}

def normalize_orderbook(bids, asks):
    """规范化订单簿排序：bids降序(最优买价在前)，asks升序(最优卖价在前)

    Polymarket CLOB API 返回的排序不固定，有时 bids 升序、asks 降序，
    导致 bids[0]/asks[0] 取到最差价格而非最优价格。
    所有使用订单簿的代码应先调用此函数。
    """
    sorted_bids = sorted(bids, key=lambda x: float(x["price"]), reverse=True)
    sorted_asks = sorted(asks, key=lambda x: float(x["price"]))
    return sorted_bids, sorted_asks


def _level_to_price_size(level):
    if isinstance(level, dict):
        price = level.get("price")
        size = level.get("size")
    else:
        price = getattr(level, "price", None)
        size = getattr(level, "size", None)
    if price is None or size is None:
        return None
    return {"price": price, "size": size}


def extract_orderbook_levels(book):
    if not book:
        return [], []

    if isinstance(book, dict):
        raw_bids = book.get("bids") or []
        raw_asks = book.get("asks") or []
    else:
        raw_bids = getattr(book, "bids", None) or []
        raw_asks = getattr(book, "asks", None) or []

    bids = [_level_to_price_size(level) for level in raw_bids]
    asks = [_level_to_price_size(level) for level in raw_asks]
    return normalize_orderbook(
        [level for level in bids if level is not None],
        [level for level in asks if level is not None],
    )


def _slug_start_timestamp(slug):
    """从 5m market slug 提取开始时间戳。"""
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


def _timestamp_to_iso_utc(ts):
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def get_price_to_beat_params(slug, variant="fiveminute", market_seconds=MARKET_WINDOW_SECONDS):
    """构造 Polymarket crypto-price 接口参数。"""
    from ai_trader.coins import coin_from_slug

    start_ts = _slug_start_timestamp(slug)
    if start_ts is None:
        return None

    return {
        "symbol": coin_from_slug(slug).upper(),
        "eventStartTime": _timestamp_to_iso_utc(start_ts),
        "variant": variant,
        "endDate": _timestamp_to_iso_utc(start_ts + market_seconds),
    }


def get_price_to_beat_api(slug, timeout=3, variant="fiveminute"):
    """用 crypto-price 接口获取当前 market 的 PTB(openPrice)。"""
    params = get_price_to_beat_params(slug, variant=variant)
    if not params:
        return None

    try:
        resp = requests.get(
            CRYPTO_PRICE_API,
            params=params,
            timeout=timeout,
            headers=DEFAULT_HEADERS,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        open_price = data.get("openPrice")
        if open_price is None:
            return None
        price = float(open_price)
        if 0.01 < price < 10_000_000:
            return price
    except Exception:
        pass
    return None


def get_price_to_beat_browser(slug, timeout=10):
    """从 HTML 提取 Price to Beat"""
    url = f"https://polymarket.com/event/{slug}"
    try:
        resp = requests.get(url, timeout=timeout, headers=DEFAULT_HEADERS)
        if resp.status_code == 200:
            match = re.search(r'"priceToBeat":([\d.]+)', resp.text)
            if match:
                price = float(match.group(1))
                if 0.01 < price < 10_000_000:
                    return price
    except Exception:
        pass
    return None


def get_price_to_beat(slug, timeout=3):
    """统一 PTB 获取入口：仅使用 crypto-price API。"""
    price = get_price_to_beat_api(slug, timeout=timeout)
    if price is not None:
        return price, "crypto-price"
    return None, None


def get_current_markets():
    """获取当前进行中的 5分钟市场"""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    base_5m = (now_ts // 300) * 300
    
    markets = []
    from ai_trader.coins import get_coins_config
    coins_cfg = get_coins_config()
    for coin, cfg in coins_cfg.items():
        prefix = cfg["slug_prefix"]
        slug = f"{prefix}-{base_5m}"
        
        try:
            resp = requests.get(
                EVENT_API,
                params={"slug": slug},
                timeout=3
            )
            if resp.status_code == 200:
                events = resp.json()
                if events and not events[0].get('closed'):
                    event = events[0]
                    market = event['markets'][0]
                    
                    ptb = get_price_to_beat_api(slug, timeout=3)
                    
                    prices = json.loads(market['outcomePrices'])
                    
                    markets.append({
                        'slug': slug,
                        'coin': coin,
                        'end_time': event['endDate'],
                        'price_to_beat': ptb,
                        'up_odds': float(prices[0]),
                        'down_odds': float(prices[1])
                    })
        except:
            pass
    
    return markets
