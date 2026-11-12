"""Checks for relationships between explicitly supplied commodity snapshots."""

from math import isfinite


def axis_basis_snapshot(snapshot):
    """Keep commodity inputs and exclude presentation names from basis checks."""
    metadata = {key: value for key, value in snapshot['axis_metadata'].items()
                if key not in ('title', 'label')}
    return {**snapshot, 'axis_metadata': metadata}


def check_aluminium_price_upside(*, aluminium_axis, gold_copper_axis):
    """Compare the historical H/M price ratios without deciding the macro state.

    The guard uses SHFE aluminium prices, not the smelting spread. Both ratios
    are dimensionless, so no conversion is needed. Callers bind the snapshots
    and decide how a failed guard affects their own output.
    """
    try:
        aluminium_prices = aluminium_axis["translated_price"]
        aluminium_unit = aluminium_axis["units"]["translated_price"]
        copper_prices = gold_copper_axis["prices"]["copper"]
        copper_unit = gold_copper_axis["units"]["copper"]
    except (KeyError, TypeError) as exc:
        raise ValueError("both axis snapshots must include their price and unit") from exc
    if aluminium_unit != "CNY_PER_METRIC_TONNE" or copper_unit != "USD_PER_METRIC_TONNE":
        raise ValueError("cross-axis comparison requires aluminium and copper prices in their original units")
    pairs = []
    for name, values in (("aluminium", aluminium_prices), ("copper", copper_prices)):
        if not isinstance(values, dict) or not {"M", "H"}.issubset(values):
            raise ValueError(f"{name}: M and H prices are required")
        mid, high = values["M"], values["H"]
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not isfinite(value) or value <= 0 for value in (mid, high)):
            raise ValueError(f"{name}: prices must be finite and positive")
        if high <= mid or not isfinite(high / mid):
            raise ValueError(f"{name}: high price must exceed mid with a finite ratio")
        pairs.append((mid, high))
    (al_mid, al_high), (cu_mid, cu_high) = pairs
    return al_high / al_mid < cu_high / cu_mid
