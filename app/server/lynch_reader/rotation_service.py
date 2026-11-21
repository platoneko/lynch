"""Publish rotation close decisions and opening price-basis changes independently."""

from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import json
import logging
from math import isclose, isfinite
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Protocol

from .daily_cache import write_json
from .market_hours import DAILY_BAR_SETTLED, LIVE_HOURS, MARKET_TZ
from .rotation import (MonthInputs, PriceBasisChange, PriceBasisChanges, RotationState,
                       SignalDay, Target, advance, build_month_inputs, rebase_month_inputs)

log = logging.getLogger(__name__)
TZ = MARKET_TZ["A"]
CHECK_ACTIONS_AT = time(9)
FINAL_OPEN_CHECK = time(9, 13)
STATE_VERSION = 1


@dataclass(frozen=True)
class ClosingReturn:
    adjusted: float
    previous_adjusted: float
    price: float


class RotationMarket(Protocol):
    def sessions(self, start: date, end: date) -> tuple[date, ...]: ...

    def adjustment(self, day: date, previous: tuple[float, float]) -> PriceBasisChanges | None: ...

    def closes(self, previous: date, day: date) -> tuple[ClosingReturn, ClosingReturn] | None: ...


class SessionList:
    def __init__(self, days):
        self.days = sorted(set(days))

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        return tuple(self.days[bisect_left(self.days, start):bisect_right(self.days, end)])


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _history(data: dict, key: str) -> dict[date, float]:
    closes = {date.fromisoformat(day): float(value) for day, value in data[key].items()}
    if not closes or not all(isfinite(value) and value > 0 for value in closes.values()):
        raise ValueError("rotation history requires positive finite closes")
    return closes


class RotationService:
    def __init__(self, path: Path, market: RotationMarket, seed: Callable[[], dict],
                 *, build_month=build_month_inputs):
        self.path, self.market, self.seed = path, market, seed
        self.build_month = build_month
        self._refresh_lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._month: MonthInputs | None = None
        self._data: dict = {}
        self._waiting: str | None = None
        try:
            data = _read(path)
            if data["version"] != STATE_VERSION:
                raise ValueError("unsupported rotation checkpoint version")
            self._data = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError) as exc:
            log.warning("rotation checkpoint unavailable: %s", exc)

    @classmethod
    def at(cls, root: Path, market: RotationMarket):
        return cls(root / "checkpoint.json", market, lambda: _read(root / "history.json"))

    def start(self) -> None:
        self._stop.clear()
        self._thread = Thread(target=self._run, name="rotation-updater", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh(datetime.now(TZ))
            except Exception:  # Keep the previous committed decision on a failed refresh.
                log.exception("rotation refresh failed")
            self._stop.wait(60)

    def snapshot(self, now: datetime) -> dict:
        data = self._data
        local = now.astimezone(TZ)
        today = local.date().isoformat()
        recommendation = data.get("recommendation")
        if recommendation is not None:
            execution = recommendation["execution_date"]
            passed = execution < today or (execution == today and local.time() >= LIVE_HOURS["A"][1][1])
            recommendation = {**recommendation, "execution_passed": passed}
        waiting = self._waiting
        next_session = data.get("next_session")
        calendar_through = data.get("calendar_through")
        if next_session is not None and next_session < today and calendar_through and calendar_through < today:
            waiting = "calendar"
        final_open_check_due = local.time() >= FINAL_OPEN_CHECK
        if (next_session is not None and next_session <= today and today in data["sessions"]
                and local.time() >= CHECK_ACTIONS_AT
                and (data.get("actions_checked") != today
                     or final_open_check_due and data.get("actions_finalized") != today)):
            waiting = "adjustment"
        if waiting is None and next_session is not None and (
            next_session < today or (next_session == today and local.time() >= CHECK_ACTIONS_AT)
        ):
            if data.get("actions_checked") != next_session:
                waiting = "adjustment"
            elif next_session < today or local.time() >= LIVE_HOURS["A"][1][1]:
                waiting = "closes"
        return {
            "status": "empty" if recommendation is None else "waiting" if waiting else "ready",
            "recommendation": recommendation,
            "waiting_reason": waiting,
            "current_bands": None if waiting in ("adjustment", "calendar") else data.get("current_bands"),
            "backtests": data.get("backtests", []),
        }

    def refresh(self, now: datetime) -> None:
        with self._refresh_lock:
            self._refresh(now.astimezone(TZ))

    def _refresh(self, local: datetime) -> None:
        data = self._data
        try:
            source = data or self.seed()
            growth = _history(source, "growth")
            dividend = _history(source, "dividend")
            last_available = min(max(growth), max(dividend))
            calendar = SessionList(map(date.fromisoformat, source["sessions"]))
            if not calendar.days:
                raise ValueError("rotation needs a dated trading calendar")
            coverage = date.fromisoformat(source.get("calendar_through", calendar.days[-1].isoformat()))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("rotation history unavailable: %s", exc)
            self._waiting = "history"
            return

        today = local.date()
        need_after = max(today, last_available)
        if calendar.days[-1] <= need_after:
            try:
                first = coverage + timedelta(days=1)
                deadline = need_after + timedelta(days=20)
                while calendar.days[-1] <= need_after and first <= deadline:
                    end = date(first.year, 12, 31)
                    calendar = SessionList((*calendar.days, *self.market.sessions(first, end)))
                    coverage = end
                    first = end + timedelta(days=1)
                if calendar.days[-1] <= need_after:
                    raise ValueError("no confirmed next trading session")
            except (OSError, ValueError) as exc:
                log.warning("rotation calendar unavailable: %s", exc)
                self._waiting = "calendar"
                return

        if not data:
            data = self._replay(source, growth, dividend, calendar, local)
            if not data:
                return
            data["calendar_through"] = coverage.isoformat()
            self._commit(data)
        elif data.get("calendar_through") != coverage.isoformat():
            data = {**data, "calendar_through": coverage.isoformat(),
                    "sessions": [day.isoformat() for day in calendar.days]}
            self._commit(data)

        while True:
            day = date.fromisoformat(data["next_session"])
            if day > today or (day == today and local.time() < CHECK_ACTIONS_AT):
                self._waiting = None
                return
            final_check = day < today or local.time() >= FINAL_OPEN_CHECK
            if (day < today or data.get("actions_checked") != day.isoformat()
                    or final_check and data.get("actions_finalized") != day.isoformat()):
                data = self._open(data, day, final_check=final_check)
                if data is None:
                    return
            if day == today and local.time() < DAILY_BAR_SETTLED["A"]:
                self._waiting = None
                return
            data = self._close(data, day, growth, dividend, calendar, current=day == today)
            if data is None:
                return

    def _replay(self, source, growth, dividend, calendar, local):
        start = date.fromisoformat(source["replay_start"])
        dates = calendar.days
        state = RotationState()
        result = {}
        for i in range(max(0, bisect_left(dates, start) - 1), len(dates) - 1):
            day, execution = dates[i:i + 2]
            if day not in growth or day not in dividend or day > local.date():
                break
            if day == local.date() and local.time() < DAILY_BAR_SETTLED["A"]:
                break
            month = self._inputs(execution, growth, dividend, calendar)
            result = _decision(state, month, day, execution, i, (growth[day], dividend[day]),
                               (growth[day], dividend[day]))
            state = _state(result["state"])
        if not result:
            self._waiting = "history"
            return {}
        return {**result, "version": STATE_VERSION, "backtests": source.get("backtests", []),
                "growth": _encode_history(growth), "dividend": _encode_history(dividend),
                "sessions": [day.isoformat() for day in dates]}

    def _inputs(self, execution, growth, dividend, calendar):
        if self._month is None or self._month.month != execution.replace(day=1):
            self._month = self.build_month(execution, growth, dividend, calendar)
        return self._month

    def _open(self, data, day, *, final_check):
        try:
            opening = list(data["raw_closes"])
            adjustment = self.market.adjustment(day, tuple(opening))
            bands = data["recommendation"]["bands"]
            if adjustment is not None:
                date_text = day.isoformat()
                shifts = {"growth": adjustment.growth, "dividend": adjustment.dividend}
                bands = {**bands, "basis_date": date_text}
                for i, (asset, change) in enumerate(shifts.items()):
                    if change is not None:
                        opening[i] = change.convert(opening[i])
                        if bands[asset] is not None:
                            bands[asset] = {key: change.convert(price) for key, price in bands[asset].items()}
        except (OSError, ValueError) as exc:
            log.warning("rotation opening adjustment unavailable: %s", exc)
            self._waiting = "adjustment"
            return None
        updated = {**data, "actions_checked": day.isoformat(), "opening_reference": opening,
                   "actions_finalized": day.isoformat() if final_check else None,
                   "current_bands": bands, "basis_date": bands["basis_date"]}
        if updated != data:
            self._commit(updated)
        return updated

    def _close(self, data, day, growth, dividend, calendar, *, current):
        previous = date.fromisoformat(data["recommendation"]["signal_date"])
        i = bisect_left(calendar.days, day)
        if i + 1 == len(calendar.days):
            self._waiting = "calendar"
            return None
        try:
            closes = self.market.closes(previous, day)
            if closes is None:
                self._waiting = "closes"
                return None
            raw = tuple(close.price for close in closes)
            if any(not all(isfinite(value) and value > 0 for value in asdict(close).values()) for close in closes):
                raise ValueError("rotation needs valid raw and adjusted closes")
            for close, reference in zip(closes, data["opening_reference"], strict=True):
                if current and not isclose(close.previous_adjusted, reference, abs_tol=.0011):
                    log.warning("rotation opening adjustment disagrees with adjusted close for %s", day)
                    self._commit({**data, "actions_checked": None})
                    self._waiting = "adjustment"
                    return None
                if current and not isclose(close.adjusted, close.price, abs_tol=.0011):
                    raise ValueError("current adjusted and raw closes use different price bases")
            for history, close, reference in zip((growth, dividend), closes, data["opening_reference"], strict=True):
                # The previous close on this session's ex-price basis is its point-in-time
                # forward-adjusted close; today's provider archive may include later payouts.
                ratio = close.adjusted / close.previous_adjusted if current else close.price / reference
                history[day] = history[previous] * ratio
            execution = calendar.days[i + 1]
            month = self._inputs(execution, growth, dividend, calendar)
            basis = data.get("basis_date") if data["current_bands"]["anchor_date"] == month.anchor_date.isoformat() else None
            result = _decision(_state(data["state"]), month, day, execution, i,
                               (growth[day], dividend[day]), raw, basis)
        except (OSError, ValueError) as exc:
            log.warning("rotation closing decision unavailable: %s", exc)
            self._waiting = "closes"
            return None
        updated = {**data, **result, "growth": _encode_history(growth),
                   "dividend": _encode_history(dividend),
                   "sessions": [day.isoformat() for day in calendar.days]}
        self._commit(updated)
        return updated

    def _commit(self, data):
        try:
            write_json(self.path, data)
        except OSError:
            self._month = None
            raise
        self._data = data


def _encode_history(history):
    return {day.isoformat(): close for day, close in history.items()}


def _state(data):
    return RotationState(**{**data, "target": Target(**data["target"])})


def _decision(state, month, day, execution, i, closes, raw, basis_date=None):
    decision = advance(state, SignalDay(day, execution, *closes, i), month)
    growth, dividend = (PriceBasisChange(quote / close) for quote, close in zip(raw, closes, strict=True))
    display = rebase_month_inputs(month, PriceBasisChanges("display", growth, dividend))
    bands = {
        "growth": asdict(display.growth) if display.growth is not None else None,
        "dividend": asdict(display.dividend) if display.dividend is not None else None,
        "anchor_date": month.anchor_date.isoformat(),
        "basis_date": basis_date,
    }
    return {
        "state": asdict(decision.state),
        "recommendation": {
            "signal_date": day.isoformat(), "execution_date": execution.isoformat(),
            "action": decision.action, "reasons": decision.reasons, "stage": decision.stage,
            "target": asdict(decision.target), "regular_growth": month.regular_growth,
            "growth_close": raw[0], "dividend_close": raw[1], "bands": bands,
        },
        "current_bands": bands, "next_session": execution.isoformat(), "basis_date": basis_date,
        "raw_closes": raw,
    }
