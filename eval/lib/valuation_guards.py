"""Reusable technical guards for valuation provenance and option status."""
from math import isfinite


def require_axis_alignment(axis: dict, expected_year: int) -> None:
    """Require a shared alignment year to match the stock's supported path."""
    if not isinstance(axis, dict):
        raise ValueError('axis alignment data must be a mapping')
    year = axis.get('t_align')
    if (isinstance(year, bool) or not isinstance(year, (int, float)) or not isfinite(year)
            or year != int(year)):
        raise ValueError('axis alignment year must be a finite whole year')
    if (isinstance(expected_year, bool) or not isinstance(expected_year, int)):
        raise ValueError('expected alignment year must be a whole year')
    if year != expected_year:
        raise ValueError('axis alignment year differs from the historical model; rebuild the forecast path')


def option_reconciliation_status(increment: float, overlap_resolved: bool, path_refreshed: bool) -> str:
    """Identify a conditional option whose supporting evidence needs review."""
    if (isinstance(increment, bool) or not isinstance(increment, (int, float))
            or not isfinite(increment) or type(overlap_resolved) is not bool
            or type(path_refreshed) is not bool):
        raise ValueError('option evidence status requires a finite increment and boolean decisions')
    if increment <= 0 or not overlap_resolved or not path_refreshed:
        return 'requires_reconciliation'
    return 'conditional'


def nonnegative_option_value(value: float) -> float | None:
    """Expose a nonnegative optional credit, or None when the input is underwater."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError('option value must be a finite number')
    return value if value >= 0 else None
