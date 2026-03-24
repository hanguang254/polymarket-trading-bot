import unittest
from unittest.mock import patch

from ai_trader import polymarket_api as api


class DummyResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


class TestPolymarketApi(unittest.TestCase):
    @patch("ai_trader.polymarket_api.requests.get")
    def test_get_price_to_beat_api_builds_crypto_price_window_from_slug(self, mock_get):
        mock_get.return_value = DummyResponse(
            status_code=200,
            json_data={"openPrice": 70351.52373576991},
        )

        price = api.get_price_to_beat_api("btc-updown-5m-1774328400", timeout=7)

        self.assertEqual(price, 70351.52373576991)
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        self.assertEqual(call_args.args[0], api.CRYPTO_PRICE_API)
        self.assertEqual(call_args.kwargs["timeout"], 7)
        self.assertEqual(
            call_args.kwargs["params"],
            {
                "symbol": "BTC",
                "eventStartTime": "2026-03-24T05:00:00Z",
                "variant": "fiveminute",
                "endDate": "2026-03-24T05:05:00Z",
            },
        )

    @patch("ai_trader.polymarket_api.get_price_to_beat_browser")
    @patch("ai_trader.polymarket_api.get_price_to_beat_api", return_value=None)
    def test_get_price_to_beat_does_not_fallback_to_html(self, mock_api, mock_browser):
        price, source = api.get_price_to_beat("btc-updown-5m-1774328400")

        self.assertIsNone(price)
        self.assertIsNone(source)
        mock_api.assert_called_once_with("btc-updown-5m-1774328400", timeout=3)
        mock_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
