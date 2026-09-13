"""Look-through NAV for index ETFs.

Fair value is current NAV scaled by sleeve earnings ratios and the gap
between each sleeve's fair multiple and the *index* PE (not a top-holder
PE average — that overweighting of expensive names is what produced the
false `stretched` reading on 520900 / 513530).
"""
from __future__ import annotations


def scale(nav: float, fee_drag: float, s: float) -> float:
    """s = Σ w · eps_ratio · pe_fair / pe_index."""
    return nav * fee_drag * s
