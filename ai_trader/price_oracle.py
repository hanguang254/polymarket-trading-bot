"""
链上价格源统一入口。

默认优先使用 Chainlink（与 Polymarket 结算更一致），
当 Chainlink 暂时没有 fresh price 时，回退到 Pyth。
"""

from ai_trader.polymarket_rtds import chainlink_stream
from ai_trader.pyth_api import get_pyth_price, pyth_stream


def get_onchain_price(coin, prefer="chainlink", with_source=False):
    """获取链上价格，返回 price 或 (price, source)。"""
    source = None
    price = None

    if prefer == "pyth":
        price = pyth_stream.get_price(coin)
        if price is not None:
            source = "Pyth"
        else:
            price = get_pyth_price(coin)
            if price is not None:
                source = "Pyth"
            else:
                price = chainlink_stream.get_price(coin)
                if price is not None:
                    source = "CL"
    else:
        price = chainlink_stream.get_price(coin)
        if price is not None:
            source = "CL"
        else:
            price = pyth_stream.get_price(coin)
            if price is not None:
                source = "Pyth"
            else:
                price = get_pyth_price(coin)
                if price is not None:
                    source = "Pyth"

    if with_source:
        return price, source
    return price
