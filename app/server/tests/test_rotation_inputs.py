from datetime import date
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import pytest

from lynch_reader.rotation_inputs import (
    AdjustedHistory,
    AdjustmentFeed,
    EtfHistory,
    IndexHistory,
    TradingCalendar,
    bosera_announced_actions,
    efund_notices,
    stitch,
)


def day(value):
    return date.fromisoformat(value)


def test_splice_preserves_first_etf_close_and_prior_index_returns():
    first, anchor, missing, last = map(day, ("2024-04-26", "2024-04-29", "2024-04-30", "2024-05-06"))
    result = stitch(
        {first: 80, anchor: 100, missing: 120, last: 150},
        AdjustedHistory({anchor: 1.02, last: 1.05}),
    )
    assert result.anchor_date == anchor
    assert result.proxy_days == (first,)
    assert result.closes == pytest.approx({first: .816, anchor: 1.02, last: 1.05})
    assert missing not in result.closes


def test_etf_history_fetches_requested_qfq_basis_and_rejects_conflict():
    first, last = day("2026-11-19"), day("2026-11-20")
    adjustment = []

    def get(url):
        query = parse_qs(urlsplit(url).query)
        adjustment.append(query["fqt"][0])
        assert query["secid"] == ["0.159307"]
        return {"rc": 0, "data": {"code": "159307", "klines": [
            "2026-11-19,1.07,1.073,1.08,1.06,1000",
            "2026-11-20,1.07,1.067,1.08,1.06,1000",
        ]}}

    provider = EtfHistory(get=get)
    assert provider.closes("159307.SZ", first, last).closes == {first: 1.073, last: 1.067}
    assert provider.raw_closes("159307.SZ", first, last).as_of == last
    assert adjustment == ["1", "0"]
    invalid = EtfHistory(get=lambda _: {"rc": 0, "data": {"code": "159307", "klines": [
        "2026-11-20,1.07,1.067,1.08,1.06,1000",
        "2026-11-20,1.07,1.000,1.08,1.06,1000",
    ]}})
    with pytest.raises(ValueError, match="conflicting"):
        invalid.closes("159307.SZ", first, last)


def test_index_series_rejects_price_index_mislabeled_total_return():
    start = end = day("2015-12-01")
    fake = {"code": 200, "data": {"indexCode": "399606", "indexEName": "ChiNext",
                                  "item": ["timestamp", "close"], "data": [["2015-12-01", 10679.34]]}}
    with pytest.raises(ValueError, match="total-return"):
        IndexHistory(get=lambda _: fake).closes("399606", start, end)


def season():
    return """<strong>2026年休市安排</strong>
    <table><tbody>
    <tr><td>元旦：</td><td>1月1日（星期四）至1月3日（星期六）休市。1月5日起照常开市。</td></tr>
    <tr><td>春节：</td><td>2月15日至2月23日休市。</td></tr>
    <tr><td>清明节：</td><td>4月4日至4月6日休市。</td></tr>
    <tr><td>劳动节：</td><td>5月1日至5月5日休市。</td></tr>
    <tr><td>端午节：</td><td>6月19日至6月21日休市。</td></tr>
    <tr><td>中秋节：</td><td>9月25日至9月27日休市。</td></tr>
    <tr><td>国庆节：</td><td>10月1日（星期四）至10月7日（星期三）休市。</td></tr>
    </tbody></table>"""


def test_calendar_skips_holidays_and_does_not_invent_unpublished_year():
    provider = TradingCalendar(get=lambda _: season())
    assert provider.sessions(day("2026-09-30"), day("2026-10-09")) == tuple(
        map(day, ("2026-09-30", "2026-10-08", "2026-10-09"))
    )
    with pytest.raises(ValueError, match="2027"):
        provider.sessions(day("2026-12-30"), day("2027-01-04"))


def test_calendar_caches_previously_published_year_for_cross_year_lookup():
    pages = iter([season(), season().replace("2026年", "2027年")])
    provider = TradingCalendar(get=lambda _: next(pages))
    assert provider.sessions(day("2026-12-31"), day("2026-12-31")) == (day("2026-12-31"),)
    assert provider.sessions(day("2026-12-31"), day("2027-01-05")) == tuple(
        map(day, ("2026-12-31", "2027-01-04", "2027-01-05"))
    )


def distributions(symbol, cash, split="<td colspan='5'>暂无拆分信息!</td>"):
    return f"""<title>基金({symbol})</title><table class="w782 comm cfxq">
    <thead><tr><th>年份</th><th>权益登记日</th><th>除息日</th><th>每10份分红</th><th>分红发放日</th></tr></thead>
    <tbody><tr>{cash}</tr></tbody></table>
    <table class="w782 comm fhxq">
    <thead><tr><th>年份</th><th>拆分折算日</th><th>拆分类型</th><th>拆分折算比例</th></tr></thead>
    <tbody><tr>{split}</tr></tbody></table>"""


def test_dividend_feed_distinguishes_no_event_from_missing_data():
    def get(url):
        symbol = url.split("_")[-1].split(".")[0]
        cash = ("<td>2026年</td><td>2026-09-11</td><td>2026-09-14</td>"
                "<td>每10份派现金0.1100元</td><td>2026-09-16</td>" if symbol == "159307"
                else "<td colspan='5'>暂无分红信息!</td>")
        return distributions(symbol, cash)

    feed = AdjustmentFeed(get=get, announcements=lambda _: {}, growth_notices=lambda _: ())
    event = feed.changes(day("2026-09-14"))["159307.SZ"]
    assert event.cash_per_share == pytest.approx(.011)
    assert event.split_ratio == 1
    assert event.price_factor(1.1) == pytest.approx(.99)
    assert feed.changes(day("2026-09-15")) == {}
    with pytest.raises(ValueError, match="distribution"):
        AdjustmentFeed(get=lambda _: "<html>upstream error</html>", announcements=lambda _: {},
                       growth_notices=lambda _: ()).changes(
            day("2026-09-15")
        )


def test_bosera_announcement_publishes_ex_date_before_archive_or_open():
    def get(url):
        if "infoList.json" in url:
            return """{"retCode":"0","data":{"fundCode":"159307","paginator":{"page":1,"total":1},
                    "resultList":[{"title":"博时基金分红公告",
                    "originalTime":"2026-09-09","url":"/column/infoDetail.do?infoid=1"}]}}"""
        return '<a href="/common/downloadFile.do?fileId=abcdef">分红公告 .docx</a>'

    rows = [
        ("基金主代码", "159307"),
        ("本次分红方案（单位：元/10份基金份额）", "0.1100"),
        ("除息日", "2026年9月14日"),
    ]
    body = "".join(
        "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row) + "</w:tr>"
        for row in rows
    )
    document = BytesIO()
    with ZipFile(document, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:tbl>{body}</w:tbl></w:body></w:document>",
        )
    actions = bosera_announced_actions(day("2026-09-13"), get=get, download=lambda _: document.getvalue())
    assert actions[day("2026-09-14")].cash_per_share == pytest.approx(.011)
    feed = AdjustmentFeed(
        get=lambda url: distributions(url.split("_")[-1].split(".")[0], "<td>暂无分红信息!</td>"),
        announcements=lambda _: actions,
        growth_notices=lambda _: (),
    )
    assert feed.changes(day("2026-09-14"))["159307.SZ"].cash_per_share == pytest.approx(.011)
    with pytest.raises(ValueError, match="unverified"):
        bosera_announced_actions(day("2026-09-13"), get=get, download=lambda _: b"upstream error")


def test_efund_action_notice_without_verified_terms_blocks_opening():
    ex = day("2026-09-14")
    reply = {"status": 1, "data": {"total": 2, "pageIndex": 0, "data": [
        {"title": "易方达创业板交易型开放式指数证券投资基金收益分配公告", "publishDate": "2026-09-10 00:00:00"},
        {"title": "基金产品资料概要更新", "publishDate": "2026-09-11 00:00:00"},
    ]}}
    notices = lambda target: efund_notices(target, post=lambda *_: reply)
    assert notices(ex) == (day("2026-09-10"),)
    feed = AdjustmentFeed(announcements=lambda _: {}, growth_notices=notices)
    feed.actions = lambda _: {}
    with pytest.raises(ValueError, match="E Fund"):
        feed.changes(ex)
