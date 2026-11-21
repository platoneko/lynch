"""Read dated market inputs for the two Shenzhen ETFs."""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from io import BytesIO
from math import isfinite
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

SYMBOLS = ("159915.SZ", "159307.SZ")
SHANGHAI = ZoneInfo("Asia/Shanghai")
CALENDAR_URL = "https://www.sse.com.cn/disclosure/dealinstruc/closed/"
ETF_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
ETF_BACKUP_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
DISTRIBUTIONS_URL = "https://fundf10.eastmoney.com/fhsp_{}.html"
CSI_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
CNINDEX_URL = "https://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
BOSERA = "https://www.bosera.com"
EFUND_NOTICE_URL = "https://api.efunds.com.cn/xcowch/front/contents"


def _read(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    if url.startswith(CSI_URL):
        headers["Referer"] = "https://www.csindex.com.cn/"
    with urlopen(Request(url, headers=headers), timeout=10) as response:
        return response.read().decode("utf-8")


def _json(url: str):
    return json.loads(_read(url))


def _post_json(url: str, body: dict):
    request = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def _price(value) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid price")
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise ValueError("price must be positive and finite")
    return result


def _record(result: dict[date, float], day: date, value: float) -> None:
    if day in result and result[day] != value:
        raise ValueError(f"conflicting prices for {day.isoformat()}")
    result[day] = value


def _days(start: date, end: date):
    if start > end:
        raise ValueError("start exceeds end")
    return (start + timedelta(days=n) for n in range((end - start).days + 1))


@dataclass(frozen=True)
class AdjustedHistory:
    closes: dict[date, float]
    source: str = ""

    @property
    def as_of(self) -> date | None:
        return max(self.closes, default=None)


@dataclass(frozen=True)
class StitchedHistory:
    closes: dict[date, float]
    proxy_days: tuple[date, ...]
    anchor_date: date


def stitch(index: Mapping[date, float], etf: AdjustedHistory) -> StitchedHistory:
    """Backcast index prices onto the first overlapping ETF close's scale."""
    common = index.keys() & etf.closes.keys()
    if not common:
        raise ValueError("index and ETF histories do not overlap")
    anchor = min(common)
    scale = _price(etf.closes[anchor]) / _price(index[anchor])
    # Some ETF observations predate the index data. Real closes take precedence.
    earliest_real = min(etf.closes)
    proxies = {day: _price(price) * scale for day, price in sorted(index.items()) if day < earliest_real}
    observed = {day: _price(price) for day, price in etf.closes.items()}
    return StitchedHistory(dict(sorted((proxies | observed).items())), tuple(proxies), anchor)


class IndexHistory:
    def __init__(self, get: Callable = _json):
        self._get = get

    def closes(self, instrument: str, start: date, end: date) -> dict[date, float]:
        if start > end:
            raise ValueError("start exceeds end")
        result: dict[date, float] = {}
        for year in range(start.year, end.year + 1):
            first, last = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
            if instrument == "399606":
                query = urlencode({"indexCode": instrument, "startDate": first, "endDate": last,
                                   "frequency": "day"})
                payload = self._get(f"{CNINDEX_URL}?{query}")
                data = payload["data"]
                if data["indexCode"] != instrument or data["indexEName"] != "ChiNext TRI":
                    raise ValueError("unexpected total-return index identity")
                dt, close = data["item"].index("timestamp"), data["item"].index("close")
                rows = ((date.fromisoformat(row[dt]), row[close]) for row in data["data"])
            elif instrument == "H20955":
                query = urlencode({"indexCode": instrument, "startDate": first.strftime("%Y%m%d"),
                                   "endDate": last.strftime("%Y%m%d")})
                payload = self._get(f"{CSI_URL}?{query}")
                data = payload["data"]
                if not isinstance(data, list):
                    raise ValueError("missing total-return index observations")
                if any(row["indexCode"] != instrument or "全收益" not in row["indexNameCn"] for row in data):
                    raise ValueError("unexpected total-return index identity")
                rows = ((date.fromisoformat(row["tradeDate"]), row["close"]) for row in data)
            else:
                raise ValueError(f"unsupported total-return index {instrument}")
            for day, value in rows:
                if first <= day <= last:
                    _record(result, day, _price(value))
        return dict(sorted(result.items()))


class EtfHistory:
    """Public forward-adjusted and unadjusted daily bars for the strategy pair."""

    def __init__(self, get: Callable = _json):
        self._get = get

    def closes(self, symbol: str, start: date, end: date) -> AdjustedHistory:
        return self._closes(symbol, start, end, adjusted=True)

    def raw_closes(self, symbol: str, start: date, end: date) -> AdjustedHistory:
        return self._closes(symbol, start, end, adjusted=False)

    def latest(self, symbol: str, count: int = 5, *, as_of: date | None = None) -> AdjustedHistory:
        if count < 1:
            raise ValueError("count must be positive")
        end = as_of or datetime.now(SHANGHAI).date()
        history = self.closes(symbol, end - timedelta(days=count * 3 + 14), end)
        return AdjustedHistory(dict(sorted(history.closes.items())[-count:]), history.source)

    def _closes(self, symbol: str, start: date, end: date, *, adjusted: bool) -> AdjustedHistory:
        if symbol not in SYMBOLS:
            raise ValueError(f"unsupported strategy symbol {symbol}")
        if start > end:
            raise ValueError("start exceeds end")
        try:
            try:
                return AdjustedHistory(self._eastmoney(symbol, start, end, adjusted), "eastmoney")
            except OSError:
                # Refetch the whole request; avoid merging prices from different bases.
                return AdjustedHistory(self._tencent(symbol, start, end, adjusted), "tencent")
        except (AttributeError, IndexError, KeyError, TypeError) as error:
            raise ValueError(f"malformed ETF bars for {symbol}") from error

    def _eastmoney(self, symbol: str, start: date, end: date, adjusted: bool) -> dict[date, float]:
        code = symbol.split(".")[0]
        query = urlencode({
            "secid": f"0.{code}", "klt": 101, "fqt": int(adjusted),
            "beg": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"), "lmt": 10000,
            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56",
        })
        payload = self._get(f"{ETF_URL}?{query}")
        data = payload.get("data")
        if payload.get("rc") != 0 or not isinstance(data, dict) or data.get("code") != code:
            raise ValueError(f"bad ETF response identity for {symbol}")
        rows = data.get("klines")
        if not isinstance(rows, list):
            raise ValueError(f"missing ETF bars for {symbol}")
        return self._bars((row.split(",") for row in rows), start, end)

    def _tencent(self, symbol: str, start: date, end: date, adjusted: bool) -> dict[date, float]:
        code = f"sz{symbol.split('.')[0]}"
        result: dict[date, float] = {}
        first = start
        while first <= end:
            last = min(first + timedelta(days=366), end)
            adjustment = "qfq" if adjusted else ""
            param = f"{code},day,{first},{last},500,{adjustment}"
            payload = self._get(f"{ETF_BACKUP_URL}?{urlencode({'param': param})}")
            data = payload["data"].get(code)
            key = "qfqday" if adjusted else "day"
            if payload.get("code") != 0 or not isinstance(data, dict) or not isinstance(data.get(key), list):
                raise ValueError(f"missing {key} ETF bars for {symbol}")
            for day, close in self._bars(data[key], first, last).items():
                _record(result, day, close)
            first = last + timedelta(days=1)
        return dict(sorted(result.items()))

    @staticmethod
    def _bars(rows, start: date, end: date) -> dict[date, float]:
        result: dict[date, float] = {}
        for row in rows:
            day = date.fromisoformat(row[0])
            volume = float(row[5])
            if not isfinite(volume) or volume < 0:
                raise ValueError(f"invalid ETF volume for {day.isoformat()}")
            if start <= day <= end and volume > 0:
                _record(result, day, _price(row[2]))
        return dict(sorted(result.items()))


class _Tables(HTMLParser):
    def __init__(self, page: str):
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[set[str], list[list[str]]]] = []
        self._depth = 0
        self._rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._rows = []
                self.tables.append((set((dict(attrs).get("class") or "").split()), self._rows))
        elif self._depth == 1 and tag == "tr":
            self._row = []
        elif self._depth == 1 and tag in ("td", "th"):
            self._cell = []

    def handle_data(self, data):
        if self._depth == 1 and self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if self._depth == 1 and tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif self._depth == 1 and tag == "tr" and self._row is not None and self._rows is not None:
            if self._row:
                self._rows.append(self._row)
            self._row = None
        if tag == "table" and self._depth:
            self._depth -= 1


_HOLIDAY_NAMES = ("元旦", "春节", "清明", "劳动节", "端午", "中秋", "国庆")
_CLOSED = re.compile(r"(\d+)月(\d+)日(?:（[^）]+）)?(?:至(\d+)月(\d+)日(?:（[^）]+）)?)?休市")
_ANNUAL = re.compile(r"<strong[^>]*>\s*(\d{4})年休市安排")


class TradingCalendar:
    """Official exchange A-share closures for the currently published year."""

    def __init__(self, get: Callable = _read):
        self._get = get
        self._closed: dict[int, set[date]] = {}

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        dates = _days(start, end)
        years = set(range(start.year, end.year + 1))
        if not years <= self._closed.keys():
            page = self._get(CALENDAR_URL)
            headings = list(_ANNUAL.finditer(page))
            ends = [heading.start() for heading in headings[1:]] + ([len(page)] if headings else [])
            for heading, limit in zip(headings, ends, strict=True):
                year = int(heading[1])
                self._closed[year] = self._closures(page[heading.end():limit], year)
        missing = years - self._closed.keys()
        if missing:
            raise ValueError(f"official A-share trading calendar unavailable for {min(missing)}")
        closed = set().union(*(self._closed[year] for year in years))
        return tuple(day for day in dates if day.weekday() < 5 and day not in closed)

    @staticmethod
    def _closures(page: str, year: int) -> set[date]:
        holiday_rows = [
            row for _, rows in _Tables(page).tables for row in rows
            if len(row) == 2 and any(name in row[0] for name in _HOLIDAY_NAMES)
        ]
        names = "".join(row[0] for row in holiday_rows)
        if not all(name in names for name in _HOLIDAY_NAMES):
            raise ValueError("official A-share holiday table is incomplete")
        closed: set[date] = set()
        for label, detail in holiday_rows:
            match = _CLOSED.search(detail)
            if match is None:
                raise ValueError(f"unrecognized official closure for {label}")
            month, day, last_month, last_day = match.groups()
            first = date(year, int(month), int(day))
            end_month, end_day = int(last_month or month), int(last_day or day)
            end_year = year + ((end_month, end_day) < (first.month, first.day))
            last = date(end_year, end_month, end_day)
            closed.update(_days(first, last))
        return closed


@dataclass(frozen=True)
class CashAction:
    ex_date: date
    cash_per_share: float = 0
    split_ratio: float = 1

    def price_factor(self, previous_raw_close: float) -> float:
        close = _price(previous_raw_close)
        factor = (close - self.cash_per_share) / close / _price(self.split_ratio)
        return _price(factor)


_CASH = re.compile(r"每(\d+)份派现金([\d.]+)元")
_SHARES = re.compile(r"([\d.]+):([\d.]+)")
_CASH_HEADER = ["年份", "权益登记日", "除息日", "每10份分红", "分红发放日"]
_SPLIT_HEADER = ["年份", "拆分折算日", "拆分类型", "拆分折算比例"]
_NOTICE_TITLE = re.compile(r"分红公告|收益分配公告|份额折算公告|基金拆分公告|除权除息公告")
_BOSERA_DOC = re.compile(r'href="(/common/downloadFile\.do\?fileId=[A-Fa-f0-9]+)"[^>]*>[^<]+\.docx</a>')
_CN_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _download_notice(url: str) -> bytes:
    limit = 1_000_000
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("fund announcement attachment exceeds size limit")
    return body


def _cash_notice_document(body: bytes) -> CashAction:
    try:
        with ZipFile(BytesIO(body)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 1_000_000:
                raise ValueError("fund announcement text exceeds size limit")
            xml = archive.read(info)
        root = ElementTree.fromstring(xml)
        fields = {}
        for row in root.findall(".//w:tr", _DOCX_NS):
            cells = ["".join(node.text or "" for node in cell.findall(".//w:t", _DOCX_NS)).strip()
                     for cell in row.findall("w:tc", _DOCX_NS)]
            if len(cells) == 2:
                fields[cells[0]] = cells[1]
        ex_date = _CN_DATE.fullmatch(fields["除息日"])
        if fields["基金主代码"] != "159307" or ex_date is None:
            raise ValueError("fund code or ex-date cannot be verified")
        amount = _price(fields["本次分红方案（单位：元/10份基金份额）"])
        return CashAction(date(*map(int, ex_date.groups())), amount / 10)
    except (BadZipFile, KeyError, ValueError, ElementTree.ParseError) as error:
        raise ValueError("unverified Bosera corporate action announcement") from error


def bosera_announced_actions(
    day: date, *, get: Callable = _read, download: Callable = _download_notice,
) -> dict[date, CashAction]:
    """Use issuer releases when an announced cash distribution is not in the archive yet."""
    params = {"classid": "0002000200030005", "fundCode": "159307",
              "startDate": day - timedelta(days=30), "endDate": day}
    payouts: dict[date, float] = {}
    page_no = 1
    while True:
        query = urlencode({**params, "pageNo": page_no})
        payload = get(f"{BOSERA}/column/infoList.json?{query}")
        notices, pages = _bosera_notice_page(payload, page_no)
        for published, title, path in notices:
            if not params["startDate"] <= published <= day or not _NOTICE_TITLE.search(title):
                continue
            if not path.startswith("/column/infoDetail.do?"):
                raise ValueError("unverified Bosera corporate action notice URL")
            attachment = _BOSERA_DOC.search(get(f"{BOSERA}{path}"))
            if not attachment:
                raise ValueError("unverified Bosera corporate action announcement format")
            action = _cash_notice_document(download(f"{BOSERA}{attachment[1]}"))
            _record(payouts, action.ex_date, action.cash_per_share)
        if page_no >= pages:
            break
        page_no += 1
    return {day: CashAction(day, value) for day, value in payouts.items()}


def _bosera_notice_page(payload: str, page_no: int) -> tuple[list[tuple[date, str, str]], int]:
    try:
        detail = json.loads(payload)
        data = detail["data"]
        if detail["retCode"] != "0" or data["fundCode"] != "159307":
            raise ValueError("unverified Bosera corporate action listing")
        pagination = data["paginator"]
        pages = pagination["total"]
        if pagination["page"] != page_no or not 0 <= pages <= 10:
            raise ValueError("unverified Bosera corporate action pagination")
        notices = [(date.fromisoformat(row["originalTime"]), row["title"], row["url"])
                   for row in data["resultList"]]
        if any(not isinstance(title, str) or not isinstance(path, str) for _, title, path in notices):
            raise ValueError("unverified Bosera corporate action notice")
        return notices, pages
    except (KeyError, TypeError) as error:
        raise ValueError("unverified Bosera corporate action listing") from error


def efund_notices(day: date, *, post: Callable = _post_json) -> tuple[date, ...]:
    """Unresolved issuer notices block publication while the action terms are unavailable."""
    start = day - timedelta(days=30)
    payload = post(EFUND_NOTICE_URL, {
        "siteID": "1", "catalogAlias": "xxplflwj,xxpldqgg,xxpllsgg", "title": "",
        "fundCode": "159915", "isIncludeTransFund": "Y", "prop1": f"{start},{day}",
        "pageSize": 30, "pageIndex": 0,
    })
    try:
        page = payload["data"]
        rows = page["data"]
        if payload["status"] != 1 or page["pageIndex"] != 0 or page["total"] != len(rows):
            raise ValueError("incomplete E Fund announcement listing")
        dates = []
        for row in rows:
            published = datetime.fromisoformat(row["publishDate"]).date()
            if start <= published <= day and _NOTICE_TITLE.search(row["title"]):
                dates.append(published)
        return tuple(dates)
    except (KeyError, TypeError) as error:
        raise ValueError("unverified E Fund announcement listing") from error


class AdjustmentFeed:
    """Read recognized fund distribution and split tables, failing closed on layout errors."""

    def __init__(self, get: Callable = _read, announcements: Callable = bosera_announced_actions,
                 growth_notices: Callable = efund_notices):
        self._get = get
        self._announcements = announcements
        self._growth_notices = growth_notices

    def changes(self, day: date) -> dict[str, CashAction]:
        archives = {symbol: self.actions(symbol) for symbol in SYMBOLS}
        results = {symbol: actions.get(day) for symbol, actions in archives.items()}
        for publication in self._growth_notices(day):
            if not any(publication <= ex_day <= day for ex_day in archives["159915.SZ"]):
                raise ValueError("E Fund corporate action announced but opening terms are unavailable")
        if announced := self._announcements(day).get(day):
            archived = results["159307.SZ"]
            if archived and archived.cash_per_share not in (0, announced.cash_per_share):
                raise ValueError("conflicting issuer and archive cash distribution")
            results["159307.SZ"] = CashAction(day, announced.cash_per_share,
                                              archived.split_ratio if archived else 1)
        return {symbol: action for symbol, action in results.items() if action is not None}

    def actions(self, symbol: str) -> dict[date, CashAction]:
        code = symbol.split(".")[0]
        if symbol not in SYMBOLS:
            raise ValueError(f"unsupported strategy symbol {symbol}")
        page = self._get(DISTRIBUTIONS_URL.format(code))
        title = re.search(r"<title>([^<]+)</title>", page)
        if not title or f"({code})" not in title[1]:
            raise ValueError(f"unrecognized fund distribution page for {symbol}")
        tables = _Tables(page).tables
        cash_rows = self._table(tables, "cfxq", _CASH_HEADER, "暂无分红信息!")
        split_rows = self._table(tables, "fhxq", _SPLIT_HEADER, "暂无拆分信息!")
        payouts: dict[date, float] = {}
        splits: dict[date, float] = {}
        for _, _, day, description, _ in cash_rows:
            event = _CASH.fullmatch(description)
            if not event:
                raise ValueError(f"unrecognized dividend amount for {symbol}")
            cash = _price(event[2]) / _price(event[1])
            _record(payouts, date.fromisoformat(day), cash)
        for _, day, _, description in split_rows:
            event = _SHARES.fullmatch(description)
            if not event:
                raise ValueError(f"unrecognized fund split ratio for {symbol}")
            _record(splits, date.fromisoformat(day), _price(event[2]) / _price(event[1]))
        return {day: CashAction(day, payouts.get(day, 0), splits.get(day, 1))
                for day in sorted(payouts.keys() | splits.keys())}

    @staticmethod
    def _table(tables, kind, header, empty):
        matching = [rows for classes, rows in tables if kind in classes and rows and rows[0] == header]
        if len(matching) != 1 or len(matching[0]) < 2:
            raise ValueError("fund distribution table is missing or unrecognized")
        rows = matching[0][1:]
        if rows == [[empty]]:
            return []
        if any(len(row) != len(header) for row in rows):
            raise ValueError("fund distribution table contains malformed rows")
        return rows
