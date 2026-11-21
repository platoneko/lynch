"""Deterministic daily decisions for the ChiNext / dividend ETF rotation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from math import isfinite, log, sqrt
from statistics import stdev
from typing import Literal, Protocol

import pandas as pd

DIVIDEND_EXIT_MULTIPLIER = 1.05


class SessionCalendar(Protocol):
    def sessions(self, start: date, end: date) -> Sequence[date]: ...


@dataclass(frozen=True)
class Target:
    growth: float
    dividend: float
    cash: float


@dataclass(frozen=True)
class RotationState:
    high: bool = False
    rebuild: int = 0
    low: int = 0
    last_add_session: int | None = None
    redirect_to_dividend: bool = False
    target: Target | None = None
    dividend_exited: bool = False


@dataclass(frozen=True)
class SignalDay:
    signal_date: date
    execution_date: date
    growth_close: float
    dividend_close: float
    session_number: int


@dataclass(frozen=True)
class Bands:
    lower: float
    middle: float
    upper: float
    half_reentry: float
    low_90: float
    low_80: float

    @classmethod
    def from_anchors(cls, lower: float, middle: float, upper: float) -> "Bands":
        return cls(lower, middle, upper, sqrt(lower * middle), lower * 0.90, lower * 0.80)


@dataclass(frozen=True)
class MonthInputs:
    month: date
    anchor_date: date
    regular_growth: float
    growth: Bands | None
    dividend: Bands | None
    applied_events: tuple[str, ...] = ()
    rebased_dividend_exit: float | None = None

    @property
    def dividend_exit(self) -> float | None:
        if self.rebased_dividend_exit is not None:
            return self.rebased_dividend_exit
        return self.dividend.upper * DIVIDEND_EXIT_MULTIPLIER if self.dividend is not None else None


@dataclass(frozen=True)
class PriceBasisChange:
    scale: float
    offset: float = 0.0

    def convert(self, price: float) -> float:
        return price * self.scale + self.offset


@dataclass(frozen=True)
class PriceBasisChanges:
    event_id: str
    growth: PriceBasisChange | None = None
    dividend: PriceBasisChange | None = None


@dataclass(frozen=True)
class Decision:
    state: RotationState
    action: Literal["rebalance", "hold"]
    reasons: tuple[str, ...]
    stage: str

    @property
    def target(self) -> Target:
        assert self.state.target is not None
        return self.state.target


LOW_STAGES = ("low_45", "low_55", "low_65")
LOW_FLOORS = (0.45, 0.55, 0.65)
HIGH_STAGES = ("high_exit", "reentry_quarter", "reentry_half")


def build_month_inputs(
    execution_day: date,
    growth: Mapping[date, float],
    dividend: Mapping[date, float],
    calendar: SessionCalendar,
) -> MonthInputs:
    month = execution_day.replace(day=1)
    previous_month_end = month - timedelta(days=1)
    previous_sessions = calendar.sessions(previous_month_end.replace(day=1), previous_month_end)
    if not previous_sessions:
        raise ValueError("no trading session in the preceding month")
    anchor = max(previous_sessions)

    overlap = sorted(day for day in growth.keys() & dividend.keys() if day <= anchor)[-64:]
    if len(overlap) != 64 or overlap[-1] != anchor:
        raise ValueError("rotation requires 64 common closes through the preceding month end")
    growth_vol = _daily_volatility(growth, overlap)
    dividend_vol = _daily_volatility(dividend, overlap)
    total_vol = growth_vol + dividend_vol
    if not isfinite(total_vol) or total_vol <= 0:
        raise ValueError("cannot allocate rotation by zero or invalid volatility")
    regular_growth = min(0.8, max(0.2, dividend_vol / total_vol))
    return MonthInputs(month, anchor, regular_growth, _fit_bands(growth, anchor), _fit_bands(dividend, anchor))


def rebase_month_inputs(month: MonthInputs, adjustments: PriceBasisChanges) -> MonthInputs:
    """Move saved triggers onto the new price basis without refitting or moving strategy state."""
    if adjustments.event_id in month.applied_events:
        return month
    dividend_exit = month.dividend_exit
    if dividend_exit is not None and adjustments.dividend is not None:
        dividend_exit = adjustments.dividend.convert(dividend_exit)
    return replace(
        month,
        growth=_rebase_bands(month.growth, adjustments.growth),
        dividend=_rebase_bands(month.dividend, adjustments.dividend),
        applied_events=(*month.applied_events, adjustments.event_id),
        rebased_dividend_exit=dividend_exit,
    )


def advance(state: RotationState, signal: SignalDay, month: MonthInputs) -> Decision:
    if not (month.anchor_date <= signal.signal_date < signal.execution_date
            and signal.execution_date.replace(day=1) == month.month):
        raise ValueError("rotation signal and execution month do not match")
    if not all(isfinite(close) and close > 0 for close in (signal.growth_close, signal.dividend_close)):
        raise ValueError("rotation requires two valid closing prices")

    reasons: list[str] = []
    if state.target is None or signal.signal_date < month.month:
        reasons.append("month_start")

    high, rebuild = state.high, state.rebuild
    low, last_add = state.low, state.last_add_session
    growth, price = month.growth, signal.growth_close
    redirect = month.dividend is not None and signal.dividend_close < month.dividend.upper
    dividend_exited = state.dividend_exited
    dividend_exit = month.dividend_exit
    if dividend_exit is not None:
        if dividend_exited and redirect:
            dividend_exited = False
            reasons.append("dividend_overheat_reentry")
        elif not dividend_exited and signal.dividend_close >= dividend_exit:
            dividend_exited = True
            reasons.append("dividend_overheat_exit")

    if growth is not None:
        if price >= growth.upper:
            if not high or rebuild:
                reasons.append("growth_upper")
            high, rebuild = True, 0
        elif high and rebuild == 0 and price <= growth.middle:
            rebuild = 1
            reasons.append("reentry_quarter")
        elif high and rebuild == 1 and price <= growth.half_reentry:
            rebuild = 2
            reasons.append("reentry_half")
        elif high and rebuild == 2 and price <= growth.lower:
            high, rebuild = False, 0
            reasons.append("reentry_complete")

        if high:
            low = 0
        elif low and price >= growth.middle:
            low = 0
            reasons.append("low_reset")
        elif price <= growth.lower and (last_add is None or signal.session_number - last_add >= 5):
            eligible_level = 1 + int(price <= growth.low_90) + int(price <= growth.low_80)
            if low < eligible_level:
                low += 1
                last_add = signal.session_number
                reasons.append(LOW_STAGES[low - 1])

    if high and (not state.high or redirect != state.redirect_to_dividend):
        reasons.append("diversification_to_dividend" if redirect else "diversification_to_cash")

    target = state.target
    if reasons:
        target = _allocate(month.regular_growth, high, rebuild, low, redirect)
        if dividend_exited:
            target = Target(target.growth, 0.0, 1.0 - target.growth)
    next_state = RotationState(high, rebuild, low, last_add, redirect, target, dividend_exited)
    stage = HIGH_STAGES[rebuild] if high else LOW_STAGES[low - 1] if low else "regular"
    return Decision(next_state, "rebalance" if reasons else "hold", tuple(reasons), stage)


def _allocate(regular_growth: float, high: bool, rebuild: int, low: int, redirect: bool) -> Target:
    if high:
        growth = regular_growth * rebuild / 4
        if redirect:
            return Target(growth, 1 - growth, 0.0)
        return Target(growth, 1 - regular_growth, regular_growth - growth)
    growth = max(regular_growth, LOW_FLOORS[low - 1]) if low else regular_growth
    return Target(growth, 1 - growth, 0.0)


def _daily_volatility(closes: Mapping[date, float], sessions: Sequence[date]) -> float:
    levels = [log(closes[day]) for day in sessions]
    return stdev(after - before for before, after in zip(levels[:-1], levels[1:], strict=True))


def _fit_bands(closes: Mapping[date, float], anchor: date) -> Bands | None:
    from frameworks.axes.etf_duration import fit

    history = pd.Series({day: close for day, close in closes.items() if day <= anchor}, dtype=float).sort_index()
    history.index = pd.DatetimeIndex(history.index)
    result = fit(history, require_order=False)
    if not result["ordered"]:
        return None
    anchors = result["anchors"]
    return Bands.from_anchors(anchors["L"], anchors["M"], anchors["H"])


def _rebase_bands(bands: Bands | None, change: PriceBasisChange | None) -> Bands | None:
    if bands is None or change is None:
        return bands
    if not isfinite(change.scale) or change.scale <= 0 or not isfinite(change.offset):
        raise ValueError("price-basis change must preserve price order")
    # Convert old triggers directly: recomputing derived levels would move them under an additive change.
    values = tuple(change.convert(value) for value in (
        bands.lower, bands.middle, bands.upper, bands.half_reentry, bands.low_90, bands.low_80,
    ))
    if not all(isfinite(value) and value > 0 for value in values):
        raise ValueError("price-basis change produced an invalid trigger")
    return Bands(*values)
