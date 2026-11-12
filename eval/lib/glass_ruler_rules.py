"""Validate when a historical no-fade glass valuation remains applicable."""
from __future__ import annotations

from math import isclose, isfinite


def _finite(value: object, name: str) -> float:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f'invalid glass {name}')
        return float(value)
    except OverflowError as exc:
        raise ValueError(f'invalid glass {name}') from exc


def check_glass_ruler(glass: dict, *, numerator_year: int, realization_offset: float,
                      projected_returns: tuple[float, ...]) -> dict:
    """Reject an unsupported changed horizon or return path; never invent a fade.

    The historical option uses the visible-year return indefinitely. If the
    shared cap would make steady return lower, a separately specified fade
    model must be constructed rather than silently altering this old formula.
    """
    try:
        alignment = glass['t_align']
        fall_years = glass['fall_years_gcs']
        cap = _finite(glass['ss_return_cap_gcs'], 'steady return cap')
        if (isinstance(numerator_year, bool) or not isinstance(numerator_year, int)
                or isinstance(alignment, bool) or not isinstance(alignment, int) or alignment <= 0
                or isinstance(fall_years, bool) or not isinstance(fall_years, int) or fall_years <= 0
                or not 0 < cap <= 1):
            raise ValueError('invalid glass ruler years or cap')
        offset = _finite(realization_offset, 'realization offset')
        if offset < 0 or not isclose(alignment - numerator_year, offset, rel_tol=0, abs_tol=1e-9):
            raise ValueError('glass alignment year conflicts with company realization; rebuild the model')
        if not projected_returns:
            raise ValueError('missing glass projected returns')
        returns = [_finite(value, 'projected return') for value in projected_returns]
        if any(value > cap + 1e-12 for value in returns):
            raise ValueError('glass return exceeds shared steady cap; rebuild with an explicit fall period')
        return {'alignment_year': alignment, 'steady_return_cap': cap, 'fall_years': fall_years,
                'return_path': 'steady_equals_forward_no_fade'}
    except (KeyError, TypeError) as exc:
        raise ValueError('missing glass ruler inputs') from exc
