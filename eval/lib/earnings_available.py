"""Earnings-available alert pure logic (no I/O, no Feishu, no longbridge).

Window semantics (market-local calendar day):
  - regular: d in {0, 1}  → every run with a hit notifies
  - extend:  d in {2..6} → notify only on first confirm for that (E, period)
  - outside: silent; may seed/update baseline period

Dual confirm: calendar earn date E in window AND latest report period advanced
vs state baseline (or already flagged for this cycle).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

MarketPool = Literal["ha", "us"]

TZ_BJT = ZoneInfo("Asia/Shanghai")
TZ_NYT = ZoneInfo("America/New_York")

REGULAR_MAX_D = 1
EXTEND_MAX_D = 6  # inclusive: day 0 .. day 6 (= T+0 plus up to +5 after)

_HA_SUFFIXES = (".HK", ".SH", ".SZ", ".SS")
_US_SUFFIXES = (".US",)


@dataclass(frozen=True)
class UniverseItem:
    ticker: str
    name: str


@dataclass(frozen=True)
class Hit:
    ticker: str
    name: str
    earn_date: str
    days: int
    period: str
    window: Literal["regular", "extend"]


@dataclass
class TickerState:
    last_seen_period: str | None = None
    flagged_earn_date: str | None = None
    flagged_period: str | None = None
    pushed_regular: bool = False
    pushed_extend: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_seen_period": self.last_seen_period,
            "flagged_earn_date": self.flagged_earn_date,
            "flagged_period": self.flagged_period,
            "pushed_regular": self.pushed_regular,
            "pushed_extend": self.pushed_extend,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> TickerState:
        if not raw:
            return cls()
        return cls(
            last_seen_period=_str_or_none(raw.get("last_seen_period")),
            flagged_earn_date=_str_or_none(raw.get("flagged_earn_date")),
            flagged_period=_str_or_none(raw.get("flagged_period")),
            pushed_regular=bool(raw.get("pushed_regular")),
            pushed_extend=bool(raw.get("pushed_extend")),
        )


def _str_or_none(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def market_tz(ticker: str) -> ZoneInfo:
    if ticker.endswith(_US_SUFFIXES):
        return TZ_NYT
    if ticker.endswith(_HA_SUFFIXES):
        return TZ_BJT
    raise ValueError(f"unsupported market suffix: {ticker}")


def pool_of(ticker: str) -> MarketPool:
    if ticker.endswith(_US_SUFFIXES):
        return "us"
    if ticker.endswith(_HA_SUFFIXES):
        return "ha"
    raise ValueError(f"unsupported market suffix: {ticker}")


def filter_universe(items: list[UniverseItem], pool: MarketPool) -> list[UniverseItem]:
    return [it for it in items if pool_of(it.ticker) == pool]


def today_in_tz(tz: ZoneInfo, override: date | None = None) -> date:
    if override is not None:
        return override
    return datetime.now(tz).date()


def parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def days_since_earn(earn: date, today: date) -> int:
    return (today - earn).days


def normalize_symbol(ticker: str) -> str:
    """Align framework tickers with Longbridge calendar symbols (0700.HK → 700.HK)."""
    if "." not in ticker:
        return ticker
    code, mkt = ticker.rsplit(".", 1)
    if mkt == "HK" and code.isdigit():
        return f"{int(code)}.{mkt}"
    return ticker


def calendar_lookup_key(ticker: str) -> str:
    return normalize_symbol(ticker)


@dataclass
class EvaluateResult:
    hits: list[Hit] = field(default_factory=list)
    state: dict[str, TickerState] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def evaluate(
    universe: list[UniverseItem],
    *,
    earn_dates: dict[str, str],
    latest_periods: dict[str, str],
    state: dict[str, TickerState],
    today_by_ticker: dict[str, date],
) -> EvaluateResult:
    """Decide notifies and return updated state (caller persists).

    earn_dates / latest_periods keys are framework tickers.
    Missing calendar or period → skip (logged via skipped).
    """
    out = EvaluateResult(state={k: TickerState(**v.to_dict()) for k, v in state.items()})

    for item in universe:
        t = item.ticker
        st = out.state.get(t) or TickerState()
        period = _str_or_none(latest_periods.get(t))
        earn_s = _str_or_none(earn_dates.get(t))
        today = today_by_ticker.get(t)
        if today is None:
            out.skipped.append(f"{t}: missing today")
            out.state[t] = st
            continue

        # Bootstrap: seed baseline, never notify on first sight.
        if st.last_seen_period is None:
            if period:
                st.last_seen_period = period
            out.state[t] = st
            if not earn_s or not period:
                out.skipped.append(f"{t}: bootstrap/seed")
            else:
                out.skipped.append(f"{t}: bootstrap seed period={period}")
            continue

        if not earn_s:
            if period:
                st.last_seen_period = period
            out.state[t] = st
            out.skipped.append(f"{t}: no earn_date in window")
            continue

        earn = parse_iso_date(earn_s)
        if earn is None:
            out.skipped.append(f"{t}: bad earn_date {earn_s!r}")
            out.state[t] = st
            continue

        d = days_since_earn(earn, today)
        if d < 0 or d > EXTEND_MAX_D:
            if period:
                st.last_seen_period = period
            # New cycle far away — drop flags tied to other dates.
            if st.flagged_earn_date and st.flagged_earn_date != earn_s:
                st.flagged_earn_date = None
                st.flagged_period = None
                st.pushed_regular = False
                st.pushed_extend = False
            out.state[t] = st
            out.skipped.append(f"{t}: outside window d={d}")
            continue

        if not period:
            out.state[t] = st
            out.skipped.append(f"{t}: no latest period")
            continue

        # New calendar event → fresh push flags for this cycle.
        if st.flagged_earn_date is not None and st.flagged_earn_date != earn_s:
            st.pushed_regular = False
            st.pushed_extend = False
            st.flagged_earn_date = None
            st.flagged_period = None

        is_new = period != st.last_seen_period
        is_flagged = st.flagged_earn_date == earn_s and st.flagged_period == period
        if not (is_new or is_flagged):
            out.state[t] = st
            out.skipped.append(f"{t}: period unchanged ({period})")
            continue

        window: Literal["regular", "extend"] = "regular" if d <= REGULAR_MAX_D else "extend"
        notify = False
        if window == "regular":
            notify = True
            st.pushed_regular = True
        else:
            # First confirm only — skip if already pushed in regular or extend.
            if not (st.pushed_regular or st.pushed_extend):
                notify = True
                st.pushed_extend = True

        st.flagged_earn_date = earn_s
        st.flagged_period = period
        st.last_seen_period = period
        out.state[t] = st

        if notify:
            out.hits.append(
                Hit(
                    ticker=t,
                    name=item.name,
                    earn_date=earn_s,
                    days=d,
                    period=period,
                    window=window,
                )
            )
        else:
            out.skipped.append(f"{t}: extend already pushed")

    out.hits.sort(key=lambda h: (h.days, h.ticker))
    return out


def format_message(hits: list[Hit], *, as_of: date, pool: MarketPool) -> tuple[str, list[str]]:
    pool_label = "H/A" if pool == "ha" else "美股"
    title = f"[财报可看·{pool_label}] {as_of.isoformat()} · {len(hits)} 只"
    if not hits:
        return title, []
    lines: list[str] = []
    for h in hits:
        name = (h.name or "").strip()
        label = f"{h.ticker} {name}" if name and name.upper() != h.ticker.upper() else h.ticker
        win = "常规" if h.window == "regular" else "顺延"
        lines.append(
            f"{label} · {h.period} · 日历 {h.earn_date} · T+{h.days}（{win}）"
        )
    return title, lines


def state_to_jsonable(state: dict[str, TickerState]) -> dict[str, Any]:
    return {k: v.to_dict() for k, v in sorted(state.items())}


def state_from_jsonable(raw: dict[str, Any] | None) -> dict[str, TickerState]:
    if not raw:
        return {}
    out: dict[str, TickerState] = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            out[str(k)] = TickerState.from_dict(v)
    return out
