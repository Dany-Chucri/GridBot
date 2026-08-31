"""Shared price/tick-size rounding.

Grid/anchor arithmetic (anchor * (1 +/- i * step_bps / 10_000)) produces
float noise that must be snapped to a clean multiple of the asset's tick
size at the point a price is first computed, before it's used for order
identity (GridEngine.make_client_order_id) or sent to the exchange
(OrderManager). Rounding only at the exchange boundary is not enough: the
reconcile diff (OrderManager._compute_diff) matches desired orders against
already-resting orders by price signature, and vol-driven step_bps drifts
slightly every cycle, so an unrounded price for "the same" grid level never
compares equal to itself across cycles. That makes every cycle look like a
brand new order set, cancel-and-replace forever instead of recognizing
already-resting orders.
"""

from decimal import Decimal, ROUND_HALF_EVEN


def round_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest multiple of tick_size."""
    if tick_size <= 0:
        return price
    ticks = (Decimal(str(price)) / Decimal(str(tick_size))).quantize(
        Decimal("1"), rounding=ROUND_HALF_EVEN
    )
    return float(ticks * Decimal(str(tick_size)))


def round_to_size(size: float, size_decimals: int) -> float:
    """Round an order size to the asset's supported decimal precision.

    Hyperliquid's wire encoder rejects any size that would lose precision
    (float_to_wire raises), so vol-scaled and inventory-skewed sizes have to
    be snapped to szDecimals before they reach order identity (the reconcile
    diff matches desired against resting orders by size) or the exchange. A
    full-size level happens to be clean; a skewed or partial-position size is
    an arbitrary-precision float that would otherwise fail the whole batch.
    """
    if size_decimals < 0:
        return size
    quantum = Decimal(1).scaleb(-size_decimals)
    return float(Decimal(str(size)).quantize(quantum, rounding=ROUND_HALF_EVEN))
