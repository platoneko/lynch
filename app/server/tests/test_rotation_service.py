from datetime import date, datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest

from lynch_reader import rotation_service
from lynch_reader.rotation import Bands, MonthInputs, PriceBasisChange, PriceBasisChanges
from lynch_reader.rotation_service import BACKTEST_POLICY, ClosingReturn, RotationService

TZ = ZoneInfo("Asia/Shanghai")
FRIDAY = date(2026, 11, 20)
MONDAY = date(2026, 11, 23)
TUESDAY = date(2026, 11, 24)
WEDNESDAY = date(2026, 11, 25)


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


class Market:
    def __init__(self):
        self.prices = {}
        self.ex_date = False
        self.unconfirmed = False

    def sessions(self, start, end):
        first, last = start, end
        return tuple(first + timedelta(days=i) for i in range((last - first).days + 1)
                     if (first + timedelta(days=i)).weekday() < 5)

    def adjustment(self, day, previous):
        if self.unconfirmed:
            raise OSError("publisher unavailable")
        if self.ex_date and day == MONDAY:
            return PriceBasisChanges("dividend-test", growth=None, dividend=PriceBasisChange(.98))
        return None

    def closes(self, previous, day):
        return self.prices.get(day)


def seeded(tmp_path, market):
    seed = {
        "replay_start": FRIDAY.isoformat(),
        "growth": {FRIDAY.isoformat(): 100},
        "dividend": {FRIDAY.isoformat(): 100},
        "sessions": [d.isoformat() for d in market.sessions(FRIDAY, WEDNESDAY)],
        "backtests": [{"label": "当前规则"}],
        "backtests_policy": BACKTEST_POLICY,
    }
    def month(execution_day, *_):
        return MonthInputs(execution_day.replace(day=1), date(2026, 10, 30), .35,
                           Bands.from_anchors(90, 100, 110), Bands.from_anchors(90, 100, 110))
    return RotationService(tmp_path / "rotation.json", market, lambda: seed, build_month=month)


def test_ex_date_open_rebases_bands_before_creating_close_signal(tmp_path):
    market = Market()
    service = seeded(tmp_path, market)
    service.refresh(at(FRIDAY, 16))
    original = service.snapshot(at(FRIDAY, 16))
    assert original["current_bands"]["dividend"]["middle"] == 100
    assert original["backtests_policy"] == BACKTEST_POLICY
    assert original["backtests"] == [{"label": "当前规则 · 红利超上沿 5% 清仓"}]
    market.ex_date = True
    service.refresh(at(MONDAY, 9, 14))
    mid = service.snapshot(at(MONDAY, 11))
    assert mid["current_bands"]["dividend"]["middle"] == 98
    assert mid["current_bands"]["dividend"]["exit"] == pytest.approx(115.5 * .98)
    assert mid["recommendation"] == original["recommendation"]
    assert mid["current_bands"]["basis_date"] == "2026-11-23"
    market.prices[MONDAY] = (ClosingReturn(100, 100, 100), ClosingReturn(98, 98, 98))
    service.refresh(at(MONDAY, 16))
    close = service.snapshot(at(MONDAY, 16))
    assert close["recommendation"]["signal_date"] == "2026-11-23"
    assert close["recommendation"]["action"] == "hold"
    assert close["current_bands"]["dividend"]["middle"] == 98


def test_missing_history_is_processed_in_order_after_restart(tmp_path):
    market = Market()
    service = seeded(tmp_path, market)
    service.refresh(at(FRIDAY, 16))
    market.prices[TUESDAY] = (ClosingReturn(100, 100, 100), ClosingReturn(100, 100, 100))
    service.refresh(at(TUESDAY, 16))
    waiting = service.snapshot(at(TUESDAY, 16))
    assert waiting["status"] == "waiting"
    assert waiting["recommendation"]["signal_date"] == "2026-11-20"
    assert waiting["recommendation"]["execution_passed"]
    market.prices[MONDAY] = (ClosingReturn(100, 100, 100), ClosingReturn(100, 100, 100))
    restarted = seeded(tmp_path, market)
    restarted.refresh(at(TUESDAY, 16))
    assert restarted.snapshot(at(TUESDAY, 16))["recommendation"]["signal_date"] == "2026-11-24"
    committed = (tmp_path / "rotation.json").read_bytes()
    restarted.refresh(at(TUESDAY, 16))
    assert (tmp_path / "rotation.json").read_bytes() == committed


def test_adopts_rule_from_legacy_checkpoint_and_preserves_exit_on_restart(tmp_path):
    market = Market()
    service = seeded(tmp_path, market)
    service.refresh(at(FRIDAY, 16))
    checkpoint = tmp_path / "rotation.json"
    legacy = json.loads(checkpoint.read_text())
    legacy["recommendation"].pop("dividend_exited", None)
    legacy.pop("backtests_policy")
    legacy["recommendation"]["bands"]["dividend"].pop("exit")
    legacy["current_bands"]["dividend"].pop("exit")
    legacy["backtests"] = [{"label": "旧规则"}]
    checkpoint.write_text(json.dumps(legacy))
    market.prices[MONDAY] = (ClosingReturn(110, 100, 110), ClosingReturn(116, 100, 116))
    migrated = seeded(tmp_path, market)
    migrated.refresh(at(MONDAY, 16))
    snapshot = migrated.snapshot(at(MONDAY, 16))
    assert snapshot["backtests"] == []
    assert snapshot["backtests_policy"] is None
    assert snapshot["recommendation"]["dividend_exited"]
    assert snapshot["recommendation"]["target"] == {"growth": 0, "dividend": 0, "cash": 1}
    assert snapshot["current_bands"]["dividend"]["exit"] == pytest.approx(115.5)
    assert "dividend_exited" not in json.loads(checkpoint.read_text())["state"]

    market.prices[TUESDAY] = (ClosingReturn(111, 110, 111), ClosingReturn(112, 116, 112))
    restarted = seeded(tmp_path, market)
    restarted.refresh(at(TUESDAY, 16))
    held = restarted.snapshot(at(TUESDAY, 16))["recommendation"]
    assert held["action"] == "hold" and held["dividend_exited"]
    assert held["target"]["cash"] == 1

    market.prices[WEDNESDAY] = (ClosingReturn(111, 111, 111), ClosingReturn(109, 112, 109))
    restarted.refresh(at(WEDNESDAY, 16))
    restored = restarted.snapshot(at(WEDNESDAY, 16))["recommendation"]
    assert not restored["dividend_exited"]
    assert restored["target"]["dividend"] == 1


def test_resume_when_old_engine_preserved_zero_dividend_target_without_marker(tmp_path):
    market = Market()
    service = seeded(tmp_path, market)
    service.refresh(at(FRIDAY, 16))
    checkpoint = tmp_path / "rotation.json"
    legacy = json.loads(checkpoint.read_text())
    for value in (legacy["state"], legacy["recommendation"]):
        value["target"] = {"growth": .35, "dividend": 0, "cash": .65}
    legacy["recommendation"].pop("dividend_exited")
    checkpoint.write_text(json.dumps(legacy))
    market.prices[MONDAY] = (ClosingReturn(100, 100, 100), ClosingReturn(112, 100, 112))
    resumed = seeded(tmp_path, market)
    resumed.refresh(at(MONDAY, 16))
    held = resumed.snapshot(at(MONDAY, 16))["recommendation"]
    assert held["dividend_exited"]
    assert held["target"]["dividend"] == 0

    market.prices[TUESDAY] = (ClosingReturn(100, 100, 100), ClosingReturn(109, 112, 109))
    resumed.refresh(at(TUESDAY, 16))
    recovered = resumed.snapshot(at(TUESDAY, 16))["recommendation"]
    assert recovered["reasons"] == ("dividend_overheat_reentry",)
    assert recovered["target"]["dividend"] == .65


def test_missing_adjustment_hides_stale_bands_and_keeps_target(tmp_path):
    market = Market()
    service = seeded(tmp_path, market)
    service.refresh(at(FRIDAY, 16))
    market.unconfirmed = True
    service.refresh(at(MONDAY, 9, 20))
    result = service.snapshot(at(MONDAY, 10))
    assert result["waiting_reason"] == "adjustment"
    assert result["current_bands"] is None
    assert result["recommendation"]["signal_date"] == "2026-11-20"


def test_opening_recheck_uses_new_notice_and_hides_unknown_basis_on_later_day(tmp_path):
    market = Market()
    service = seeded(tmp_path, market)
    service.refresh(at(FRIDAY, 16))
    service.refresh(at(MONDAY, 9))
    assert service.snapshot(at(MONDAY, 9, 15))["current_bands"] is None
    market.ex_date = True
    service.refresh(at(MONDAY, 9, 14))
    assert service.snapshot(at(MONDAY, 10))["current_bands"]["dividend"]["middle"] == 98
    assert service.snapshot(at(MONDAY, 15))["waiting_reason"] == "closes"
    service.refresh(at(TUESDAY, 9, 14))
    assert service.snapshot(at(TUESDAY, 10))["current_bands"] is None


def test_backfill_uses_that_days_reference_before_later_dividends(tmp_path):
    market = Market()
    service = seeded(tmp_path, market)
    service.refresh(at(FRIDAY, 16))
    market.prices[MONDAY] = (ClosingReturn(60, 50, 110), ClosingReturn(100, 100, 100))
    service.refresh(at(TUESDAY, 10))
    result = service.snapshot(at(TUESDAY, 10))["recommendation"]
    assert result["signal_date"] == "2026-11-23"
    assert result["growth_close"] == 110
    persisted = (tmp_path / "rotation.json").read_text()
    assert '"2026-11-23": 110' in persisted


def test_overdue_close_rechecks_notice_finalized_before_action_was_available(tmp_path):
    market = Market()
    service = seeded(tmp_path, market)
    service.refresh(at(MONDAY, 15, 40))
    market.ex_date = True
    market.prices[MONDAY] = (ClosingReturn(100, 100, 100), ClosingReturn(98, 98, 98))
    service.refresh(at(TUESDAY, 11))
    result = service.snapshot(at(TUESDAY, 11))
    assert result["recommendation"]["signal_date"] == MONDAY.isoformat()
    assert result["recommendation"]["bands"]["basis_date"] == MONDAY.isoformat()
    assert result["recommendation"]["action"] == "hold"


def test_failed_month_end_publication_refits_corrected_close(tmp_path, monkeypatch):
    month_end = date(2026, 11, 30)
    market = Market()
    seed = {
        "replay_start": FRIDAY.isoformat(),
        "growth": {d.isoformat(): 100 for d in market.sessions(FRIDAY, date(2026, 11, 27))},
        "dividend": {d.isoformat(): 100 for d in market.sessions(FRIDAY, date(2026, 11, 27))},
        "sessions": [d.isoformat() for d in market.sessions(FRIDAY, date(2026, 12, 3))],
        "backtests": [],
    }
    def month(execution_day, growth, *_):
        middle = growth[month_end] if execution_day.month == 12 else 100
        return MonthInputs(execution_day.replace(day=1), month_end if execution_day.month == 12 else date(2026, 10, 30),
                           .35, Bands.from_anchors(90, middle, 110), Bands.from_anchors(90, 100, 110))

    service = RotationService(tmp_path / "rotation.json", market, lambda: seed, build_month=month)
    service.refresh(at(month_end, 9, 14))
    original_write = rotation_service.write_json
    monkeypatch.setattr(rotation_service, "write_json",
                        lambda *_: (_ for _ in ()).throw(OSError("simulated write failure")))
    market.prices[month_end] = (ClosingReturn(101, 100, 101), ClosingReturn(100, 100, 100))
    with pytest.raises(OSError, match="simulated write failure"):
        service.refresh(at(month_end, 16))
    monkeypatch.setattr(rotation_service, "write_json", original_write)
    market.prices[month_end] = (ClosingReturn(102, 100, 102), ClosingReturn(100, 100, 100))
    service.refresh(at(month_end, 16))
    assert service.snapshot(at(month_end, 16))["current_bands"]["growth"]["middle"] == 102


def test_verified_calendar_tail_survives_waiting_for_next_year(tmp_path):
    dec29, dec30, dec31 = (date(2026, 12, d) for d in (29, 30, 31))
    market = Market()
    known = market.sessions

    def sessions(start, end):
        if start.year == 2027:
            raise ValueError("next exchange calendar unavailable")
        return known(start, end)

    market.sessions = sessions
    seed = {
        "replay_start": dec29.isoformat(), "sessions": [dec29.isoformat(), dec30.isoformat()],
        "calendar_through": dec30.isoformat(), "growth": {dec29.isoformat(): 100},
        "dividend": {dec29.isoformat(): 100},
    }
    service = RotationService(tmp_path / "rotation.json", market, lambda: seed,
                              build_month=lambda execution, *_: MonthInputs(
                                  execution.replace(day=1), date(2026, 11, 30), .35, None, None))
    service.refresh(at(dec29, 16))
    service.refresh(at(dec31, 10))
    stored = json.loads((tmp_path / "rotation.json").read_text())
    assert stored["calendar_through"] == dec31.isoformat()
    assert stored["sessions"][-1] == dec31.isoformat()
    assert service.snapshot(at(dec31, 10))["current_bands"] is None
