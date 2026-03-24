import os
import unittest
from unittest.mock import MagicMock, patch

from ai_trader import clob_client


class TestClobClientSignatureType(unittest.TestCase):
    def tearDown(self):
        clob_client._client = None

    def test_init_client_uses_proxy_wallet_for_gnosis_safe(self):
        mock_client = MagicMock()
        mock_client.create_or_derive_api_creds.return_value = {"api_key": "test-key"}

        env = {
            "PRIVATE_KEY": "0xabc123",
            "EOA_WALLET": "0x1111111111111111111111111111111111111111",
            "PROXY_WALLET": "0x2222222222222222222222222222222222222222",
            "CLOB_SIGNATURE_TYPE": "2",
        }

        with patch.dict(os.environ, env, clear=False):
            with patch.object(clob_client, "ClobClient", return_value=mock_client) as mock_ctor:
                with patch.object(clob_client, "_warmup"), patch.object(clob_client, "_start_keepalive"):
                    clob_client.init_client()

        mock_ctor.assert_called_once_with(
            "https://clob.polymarket.com",
            key="0xabc123",
            chain_id=137,
            signature_type=2,
            funder="0x2222222222222222222222222222222222222222",
        )
        mock_client.set_api_creds.assert_called_once_with({"api_key": "test-key"})

    def test_get_balance_inherits_client_signature_type(self):
        mock_client = MagicMock()
        mock_client.get_balance_allowance.return_value = {"balance": "9000000"}
        clob_client._client = mock_client

        balance = clob_client.get_balance()

        self.assertEqual(balance, 9.0)
        params = mock_client.get_balance_allowance.call_args.args[0]
        self.assertEqual(params.asset_type, clob_client.AssetType.COLLATERAL)
        self.assertEqual(params.token_id, "")
        self.assertEqual(params.signature_type, -1)

    def test_get_token_balance_inherits_client_signature_type(self):
        mock_client = MagicMock()
        mock_client.get_balance_allowance.return_value = {"balance": "1234500"}
        clob_client._client = mock_client

        balance = clob_client.get_token_balance("token-1")

        self.assertEqual(balance, 1.2345)
        params = mock_client.get_balance_allowance.call_args.args[0]
        self.assertEqual(params.asset_type, clob_client.AssetType.CONDITIONAL)
        self.assertEqual(params.token_id, "token-1")
        self.assertEqual(params.signature_type, -1)

    def test_update_token_allowance_inherits_client_signature_type(self):
        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {"status": "ok"}
        clob_client._client = mock_client

        ok = clob_client.update_token_allowance("token-1")

        self.assertTrue(ok)
        params = mock_client.update_balance_allowance.call_args.args[0]
        self.assertEqual(params.asset_type, clob_client.AssetType.CONDITIONAL)
        self.assertEqual(params.token_id, "token-1")
        self.assertEqual(params.signature_type, -1)
