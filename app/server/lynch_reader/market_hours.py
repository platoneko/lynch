"""When each market's quotes can move, by weekday trading hours (holidays not modelled).

The quote cache also asks whether a market is in its opening auction, whose last price is no trade.
Three readers of the hours: the quote cache skips a Longbridge call when no market in the pool has
traded since the last one, a US row dates its regular price (which has no trade
time) at the latest regular close to pick the newest price across sessions, and the anomaly baselines read each
market's daily bars once its session's bar settles. A holiday counts as a trading day: it costs
extra fetches, and on a US holiday the prior day's after-hours price is not shown.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as clock
from zoneinfo import ZoneInfo

# Keyed by `market_bucket` ids.
MARKET_TZ = {"A": ZoneInfo("Asia/Shanghai"), "HK": ZoneInfo("Asia/Hong_Kong"), "US": ZoneInfo("America/New_York")}
# Local opening auctions, [start, matched): until the match the quote's last price is the
# auction's indicative price, not a trade (HK's pre-opening session matches by 9:22).
OPENING_AUCTION = {"A": (clock(9, 15), clock(9, 25)), "HK": (clock(9, 0), clock(9, 22))}
# Local hours in which a trading day's quotes can move, as (day offset, time) pairs:
# from the opening auction's match, lunch breaks not cut out. A US day runs from the
# previous evening's overnight session through post-market.
LIVE_HOURS = {
    "A": ((0, OPENING_AUCTION["A"][1]), (0, clock(15, 0))),
    "HK": ((0, OPENING_AUCTION["HK"][1]), (0, clock(16, 10))),
    "US": ((-1, clock(20, 0)), (0, clock(20, 0))),
}
# Closing auctions and late prints settle a little after the bell.
SETTLE_GRACE = timedelta(minutes=15)
US_OPEN = clock(9, 30)
US_CLOSE = clock(16, 0)
# A session's daily bar is read 23 minutes after the regular close: past the closing auction and late
# prints, and off the hour and half hour.
DAILY_BAR_SETTLED = {"A": clock(15, 23), "HK": clock(16, 23), "US": clock(16, 23)}


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


def in_opening_auction(market: str | None, t: float) -> bool:
    """Whether `market` is in its opening auction at `t`, before the match; False for a market without one."""
    if market not in OPENING_AUCTION:
        return False
    now = datetime.fromtimestamp(t, MARKET_TZ[market])
    start, matched = OPENING_AUCTION[market]
    return _is_weekday(now.date()) and start <= now.time() < matched


def us_regular_close(t: float) -> datetime:
    """Close of the latest US regular session that had opened by `t`."""
    now = datetime.fromtimestamp(t, MARKET_TZ["US"])
    d = now.date()
    while not (_is_weekday(d) and datetime.combine(d, US_OPEN, now.tzinfo) <= now):
        d -= timedelta(days=1)
    return datetime.combine(d, US_CLOSE, now.tzinfo)


def daily_bar_settled(market: str, t: float) -> datetime:
    """When the latest weekday session whose daily bar had settled by `t` settled; its local date is the bar's."""
    tz = MARKET_TZ[market]
    now = datetime.fromtimestamp(t, tz)
    d = now.date()
    while not (_is_weekday(d) and datetime.combine(d, DAILY_BAR_SETTLED[market], tz) <= now):
        d -= timedelta(days=1)
    return datetime.combine(d, DAILY_BAR_SETTLED[market], tz)


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5
