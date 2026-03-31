"""Tests for GridEngine — pure calculation, no mocks needed."""

from gridbot.grid_engine import GridEngine
from gridbot.types import OrderSide


class TestClientOrderId:
    """Deterministic order ID generation (section 7.3)."""

    def test_same_inputs_produce_same_id(self):
        id1 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        id2 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        assert id1 == id2

    def test_different_price_produces_different_id(self):
        id1 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        id2 = GridEngine.make_client_order_id("BTC-PERP", 50100.0, OrderSide.BUY, "abc123", 1)
        assert id1 != id2

    def test_different_config_hash_produces_different_id(self):
        id1 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        id2 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "def456", 1)
        assert id1 != id2

    def test_different_epoch_produces_different_id(self):
        id1 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        id2 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 2)
        assert id1 != id2


class TestConfigHash:
    def test_deterministic(self):
        h1 = GridEngine.compute_config_hash(50000.0, 2.5, 20.0)
        h2 = GridEngine.compute_config_hash(50000.0, 2.5, 20.0)
        assert h1 == h2

    def test_different_anchor(self):
        h1 = GridEngine.compute_config_hash(50000.0, 2.5, 20.0)
        h2 = GridEngine.compute_config_hash(51000.0, 2.5, 20.0)
        assert h1 != h2
