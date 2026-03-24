import unittest

from ai_trader.fees import effective_fee_rate, estimate_buy_fill, estimate_sell_fill, infer_fee_exponent


class TestPolymarketFees(unittest.TestCase):
    def test_infer_crypto_exponent_from_current_fee_rate(self):
        self.assertEqual(infer_fee_exponent(2500, market_category="crypto"), 2.0)
        self.assertEqual(infer_fee_exponent(720, market_category="crypto"), 1.0)

    def test_estimate_buy_fill_converts_gross_shares_to_net_shares(self):
        summary = estimate_buy_fill(
            price=0.71,
            gross_size=7.098589,
            fee_rate_bps=2500,
            gross_cost=5.039998,
            net_size=7.033124,
        )

        self.assertAlmostEqual(summary["net_size"], 7.033124, places=6)
        self.assertAlmostEqual(summary["effective_entry_price"], 5.039998 / 7.033124, places=6)
        self.assertGreater(summary["fee_shares"], 0.06)
        self.assertGreater(summary["fee_usdc"], 0.04)

    def test_estimate_sell_fill_deducts_usdc_fee(self):
        effective_rate = effective_fee_rate(0.50, 2500, market_category="crypto")
        self.assertAlmostEqual(effective_rate, 0.015625, places=6)

        summary = estimate_sell_fill(price=0.50, size=10, fee_rate_bps=2500, gross_proceeds=5.0)
        self.assertAlmostEqual(summary["fee_usdc"], 0.0781, places=4)
        self.assertAlmostEqual(summary["net_proceeds"], 4.9219, places=4)
        self.assertAlmostEqual(summary["net_price"], 0.49219, places=5)


if __name__ == "__main__":
    unittest.main()
