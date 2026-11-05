from decimal import Decimal

import pytest

from pilot_calc import total


def test_total_with_tax() -> None:
    assert total([Decimal("10.00"), Decimal("5.50")]) == Decimal("17.36")


def test_negative_tax_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        total([Decimal(1)], Decimal("-0.1"))
