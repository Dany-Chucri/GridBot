"""Tests for shared price/size rounding."""

from gridbot.pricing import round_to_size, round_to_tick


class TestRoundToTick:
    def test_snaps_to_nearest_multiple(self):
        assert round_to_tick(78573.3658405173, 1.0) == 78573.0
        assert round_to_tick(78573.6, 1.0) == 78574.0

    def test_sub_dollar_tick(self):
        assert round_to_tick(2500.017, 0.01) == 2500.02

    def test_non_positive_tick_is_passthrough(self):
        assert round_to_tick(123.456, 0.0) == 123.456


class TestRoundToSize:
    def test_snaps_arbitrary_precision_to_sz_decimals(self):
        # The exact value that failed float_to_wire in production.
        assert round_to_size(0.00015597843404667523, 5) == 0.00016

    def test_full_lot_size_is_unchanged(self):
        assert round_to_size(0.001, 5) == 0.001

    def test_eth_four_decimals(self):
        assert round_to_size(0.0123456, 4) == 0.0123

    def test_sub_lot_rounds_to_zero(self):
        assert round_to_size(0.000004, 5) == 0.0

    def test_negative_decimals_is_passthrough(self):
        assert round_to_size(0.123456789, -1) == 0.123456789
