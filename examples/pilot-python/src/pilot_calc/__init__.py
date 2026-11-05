"""Tiny invoice calculator used as a pilot project."""

from decimal import ROUND_HALF_UP, Decimal


def total(prices: list[Decimal], tax_rate: Decimal = Decimal("0.12")) -> Decimal:
    """Sum of prices with tax, rounded to cents."""
    if tax_rate < 0:
        raise ValueError("tax rate cannot be negative")
    subtotal = sum(prices, Decimal(0))
    return (subtotal * (1 + tax_rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
