"""When each market's quotes can move, by weekday trading hours (holidays not modelled).

Two readers: the quote cache skips a Longbridge call when no market in the pool has
traded since the last one, and a US row dates its regular price (which has no trade
time) at the latest regular close to pick the newest price across sessions. A holiday counts as a trading day: it costs
extra fetches, and on a US holiday the prior day's after-hours price is not shown.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as clock
from zoneinfo import ZoneInfo

# Keyed by `market_bucket` ids.
MARKET_TZ = {"A": ZoneInfo("Asia/Shanghai"), "HK": ZoneInfo("Asia/Hong_Kong"), "US": ZoneInfo("America/New_York")}
# Local hours in which a trading day's quotes can move, as (day offset, time) pairs:
# opening auctions included, lunch breaks not cut out. A US day runs from the
# previous evening's overnight session through post-market.
LIVE_HOURS = {
    "A": ((0, clock(9, 15)), (0, clock(15, 0))),
    "HK": ((0, clock(9, 0)), (0, clock(16, 10))),
    "US": ((-1, clock(20, 0)), (0, clock(20, 0))),
}
# Closing auctions and late prints settle a little after the bell.
SETTLE_GRACE = timedelta(minutes=15)
US_OPEN = clock(9, 30)
US_CLOSE = clock(16, 0)


def traded_between(market: str | None, t0: float, t1: float) -> bool:
    """Whether `market`'s quotes could have moved in (t0, t1]; True for a market without known hours."""
    if market not in LIVE_HOURS:
        return True
    tz = MARKET_TZ[market]
    (start_off, start_at), (end_off, end_at) = LIVE_HOURS[market]
    a, b = datetime.fromtimestamp(t0, tz), datetime.fromtimestamp(t1, tz)
    # A US day's hours start the evening before, so the day after `b` can reach back into range.
    d = a.date()
    while d <= b.date() + timedelta(days=1):
        opens = datetime.combine(d + timedelta(days=start_off), start_at, tz)
        settles = datetime.combine(d + timedelta(days=end_off), end_at, tz) + SETTLE_GRACE
        if _is_weekday(d) and opens < b and settles > a:
            return True
        d += timedelta(days=1)
    return False


def us_regular_close(t: float) -> datetime:
    """Close of the latest US regular session that had opened by `t`."""
    now = datetime.fromtimestamp(t, MARKET_TZ["US"])
    d = now.date()
    while not (_is_weekday(d) and datetime.combine(d, US_OPEN, now.tzinfo) <= now):
        d -= timedelta(days=1)
    return datetime.combine(d, US_CLOSE, now.tzinfo)


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5
