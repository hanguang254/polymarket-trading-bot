import os
import importlib
import sys
import types
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from ai_trader import clob_client


def _install_fake_v2_sdk():
    """Install a minimal py_clob_client_v2 module tree for import-time tests."""
    fake_pkg = types.ModuleType("py_clob_client_v2")

    class FakeOrderType:
        GTC = "GTC"
        FOK = "FOK"
        GTD = "GTD"
        FAK = "FAK"

    class FakeAssetType:
        COLLATERAL = "COLLATERAL"
        CONDITIONAL = "CONDITIONAL"

    @dataclass
    class FakeApiCreds:
        api_key: str
        api_secret: str = ""
        api_passphrase: str = ""

    @dataclass
    class FakeOrderArgs:
        token_id: str
        price: float
        size: float
        side: str
        expiration: int = 0

    @dataclass
    class FakeMarketOrderArgs:
        token_id: str
        amount: float
        side: str
        price: float = 0
        order_type: str = FakeOrderType.FOK
        user_usdc_balance: float = 0

    @dataclass
    class FakeBalanceAllowanceParams:
        asset_type: str = None
        token_id: str = None
        signature_type: int = -1

    @dataclass
    class FakePartialCreateOrderOptions:
        tick_size: str = None
        neg_risk: bool = None

    @dataclass
    class FakeOpenOrderParams:
        id: str = None
        market: str = None
        asset_id: str = None

    @dataclass
    class FakeOrderPayload:
        orderID: str

    @dataclass
    class FakeOrderMarketCancelParams:
        market: str = None
        asset_id: str = None

    fake_pkg.ClobClient = MagicMock(name="FakeV2ClobClient")
    fake_pkg.ApiCreds = FakeApiCreds
    fake_pkg.OrderArgs = FakeOrderArgs
    fake_pkg.MarketOrderArgs = FakeMarketOrderArgs
    fake_pkg.OrderType = FakeOrderType
    fake_pkg.BalanceAllowanceParams = FakeBalanceAllowanceParams
    fake_pkg.AssetType = FakeAssetType
    fake_pkg.PartialCreateOrderOptions = FakePartialCreateOrderOptions
    fake_pkg.OpenOrderParams = FakeOpenOrderParams
    fake_pkg.OrderPayload = FakeOrderPayload
    fake_pkg.OrderMarketCancelParams = FakeOrderMarketCancelParams

    order_builder = types.ModuleType("py_clob_client_v2.order_builder")
    constants = types.ModuleType("py_clob_client_v2.order_builder.constants")
    constants.BUY = "BUY"
    constants.SELL = "SELL"

    installed = {
        "py_clob_client_v2": fake_pkg,
        "py_clob_client_v2.order_builder": order_builder,
        "py_clob_client_v2.order_builder.constants": constants,
    }
    originals = {name: sys.modules.get(name) for name in installed}
    sys.modules.update(installed)
    return originals


def _restore_modules(originals):
    for name, original in originals.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    importlib.reload(clob_client)


class TestClobClientSignatureType(unittest.TestCase):
    def tearDown(self):
        clob_client._client = None

    def test_init_client_uses_proxy_wallet_for_gnosis_safe(self):
        mock_client = MagicMock()
        expected_creds = {"api_key": "test-key"}
        if clob_client._SDK_V2:
            mock_client.create_or_derive_api_key.return_value = expected_creds
        else:
            mock_client.create_or_derive_api_creds.return_value = expected_creds

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
        if clob_client._SDK_V2:
            mock_client.create_or_derive_api_key.assert_called_once_with()
        else:
            mock_client.create_or_derive_api_creds.assert_called_once_with()
        mock_client.set_api_creds.assert_called_once_with(expected_creds)

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


class TestClobClientV2Compatibility(unittest.TestCase):
    def tearDown(self):
        clob_client._client = None
        clob_client._token_cache.clear()

    def test_init_client_uses_v2_create_or_derive_api_key_when_available(self):
        originals = _install_fake_v2_sdk()
        try:
            module = importlib.reload(clob_client)
            mock_client = MagicMock()
            mock_client.create_or_derive_api_key.return_value = module.ApiCreds(
                api_key="test-key",
                api_secret="test-secret",
                api_passphrase="test-pass",
            )

            env = {
                "PRIVATE_KEY": "0xabc123",
                "EOA_WALLET": "0x1111111111111111111111111111111111111111",
                "PROXY_WALLET": "0x2222222222222222222222222222222222222222",
                "CLOB_SIGNATURE_TYPE": "2",
            }

            with patch.dict(os.environ, env, clear=False):
                with patch.object(module, "ClobClient", return_value=mock_client) as mock_ctor:
                    with patch.object(module, "_warmup"), patch.object(module, "_start_keepalive"):
                        module.init_client()

            self.assertEqual(module.SDK_FLAVOR, "py-clob-client-v2")
            mock_ctor.assert_called_once_with(
                "https://clob.polymarket.com",
                key="0xabc123",
                chain_id=137,
                signature_type=2,
                funder="0x2222222222222222222222222222222222222222",
            )
            mock_client.create_or_derive_api_key.assert_called_once_with()
            mock_client.set_api_creds.assert_called_once()
        finally:
            _restore_modules(originals)

    def test_fok_market_order_uses_v2_args_without_legacy_fields(self):
        originals = _install_fake_v2_sdk()
        try:
            module = importlib.reload(clob_client)

            class FakeClient:
                def __init__(self):
                    self.created_args = None

                def create_market_order(self, order_args, options=None):
                    self.created_args = order_args
                    return {"signed": True}

                def post_order(self, order, order_type):
                    return {"status": "MATCHED", "orderID": "oid-1"}

            fake_client = FakeClient()
            module._client = fake_client
            module._token_cache["token-1"] = {"neg_risk": False}

            result = module.place_fok_order("token-1", module.BUY, 0.42, 10)

            self.assertTrue(result["matched"])
            self.assertEqual(fake_client.created_args.amount, 4.2)
            self.assertEqual(fake_client.created_args.price, 0.42)
            self.assertEqual(fake_client.created_args.order_type, module.OrderType.FOK)
        finally:
            _restore_modules(originals)

    def test_v2_cancel_and_open_orders_method_names_are_used(self):
        originals = _install_fake_v2_sdk()
        try:
            module = importlib.reload(clob_client)

            class FakeClient:
                def __init__(self):
                    self.cancel_payload = None
                    self.open_params = None

                def cancel_order(self, payload):
                    self.cancel_payload = payload
                    return {"canceled": ["oid-1"]}

                def get_open_orders(self, params):
                    self.open_params = params
                    return [{"id": "oid-2"}]

            fake_client = FakeClient()
            module._client = fake_client

            self.assertTrue(module.cancel_order("oid-1"))
            self.assertEqual(fake_client.cancel_payload.orderID, "oid-1")

            orders = module.get_orders(asset_id="token-1")
            self.assertEqual(orders, [{"id": "oid-2"}])
            self.assertEqual(fake_client.open_params.asset_id, "token-1")
        finally:
            _restore_modules(originals)
