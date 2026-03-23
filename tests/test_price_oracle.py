import unittest
from unittest.mock import patch

from ai_trader.price_oracle import get_onchain_price


class TestPriceOracle(unittest.TestCase):
    @patch("ai_trader.price_oracle.get_pyth_price")
    @patch("ai_trader.price_oracle.pyth_stream.get_price")
    @patch("ai_trader.price_oracle.chainlink_stream.get_price")
    def test_prefers_chainlink_before_pyth(self, mock_cl, mock_pyth_stream, mock_pyth_rest):
        mock_cl.return_value = 68888.0
        mock_pyth_stream.return_value = 68880.0
        mock_pyth_rest.return_value = 68870.0

        price, source = get_onchain_price("BTC", with_source=True)

        self.assertEqual(price, 68888.0)
        self.assertEqual(source, "CL")
        mock_pyth_stream.assert_not_called()
        mock_pyth_rest.assert_not_called()

    @patch("ai_trader.price_oracle.get_pyth_price")
    @patch("ai_trader.price_oracle.pyth_stream.get_price")
    @patch("ai_trader.price_oracle.chainlink_stream.get_price")
    def test_falls_back_to_pyth_stream(self, mock_cl, mock_pyth_stream, mock_pyth_rest):
        mock_cl.return_value = None
        mock_pyth_stream.return_value = 2081.25
        mock_pyth_rest.return_value = 2081.10

        price, source = get_onchain_price("ETH", with_source=True)

        self.assertEqual(price, 2081.25)
        self.assertEqual(source, "Pyth")
        mock_pyth_rest.assert_not_called()

    @patch("ai_trader.price_oracle.get_pyth_price")
    @patch("ai_trader.price_oracle.pyth_stream.get_price")
    @patch("ai_trader.price_oracle.chainlink_stream.get_price")
    def test_falls_back_to_pyth_rest_when_stream_missing(self, mock_cl, mock_pyth_stream, mock_pyth_rest):
        mock_cl.return_value = None
        mock_pyth_stream.return_value = None
        mock_pyth_rest.return_value = 608.5

        price, source = get_onchain_price("BNB", with_source=True)

        self.assertEqual(price, 608.5)
        self.assertEqual(source, "Pyth")

    @patch("ai_trader.price_oracle.get_pyth_price")
    @patch("ai_trader.price_oracle.pyth_stream.get_price")
    @patch("ai_trader.price_oracle.chainlink_stream.get_price")
    def test_returns_none_when_no_onchain_source_available(self, mock_cl, mock_pyth_stream, mock_pyth_rest):
        mock_cl.return_value = None
        mock_pyth_stream.return_value = None
        mock_pyth_rest.return_value = None

        price, source = get_onchain_price("BTC", with_source=True)

        self.assertIsNone(price)
        self.assertIsNone(source)


if __name__ == "__main__":
    unittest.main()
