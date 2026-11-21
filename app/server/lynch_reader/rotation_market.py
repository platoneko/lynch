"""Read the two ETF series and their opening adjustments for the rotation."""

from datetime import date

from .rotation import PriceBasisChange, PriceBasisChanges
from .rotation_inputs import SYMBOLS, AdjustmentFeed, EtfHistory, TradingCalendar
from .rotation_service import ClosingReturn

class EtfRotationMarket:
    def __init__(self, calendar=None, history=None, actions=None):
        self.calendar = calendar if calendar is not None else TradingCalendar()
        self.history = history if history is not None else EtfHistory()
        self.actions = actions if actions is not None else AdjustmentFeed()

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        return self.calendar.sessions(start, end)

    def adjustment(self, day: date, previous: tuple[float, float]) -> PriceBasisChanges | None:
        actions = self.actions.changes(day)
        if not actions:
            return None
        shifts = [
            PriceBasisChange(action.price_factor(close)) if (action := actions.get(symbol)) else None
            for symbol, close in zip(SYMBOLS, previous, strict=True)
        ]
        return PriceBasisChanges(day.isoformat(), *shifts)

    def closes(self, previous: date, day: date) -> tuple[ClosingReturn, ClosingReturn] | None:
        returns = []
        for symbol in SYMBOLS:
            adjusted = self.history.closes(symbol, previous, day).closes
            raw = self.history.raw_closes(symbol, day, day).closes
            if day not in raw or previous not in adjusted or day not in adjusted:
                return None
            returns.append(ClosingReturn(adjusted[day], adjusted[previous], raw[day]))
        return returns[0], returns[1]
