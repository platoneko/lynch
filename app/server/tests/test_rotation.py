from datetime import date, timedelta
import math

import pytest

from lynch_reader.rotation import (
    Bands,
    MonthInputs,
    PriceBasisChange,
    PriceBasisChanges,
    RotationState,
    SignalDay,
    Target,
    advance,
    build_month_inputs,
    rebase_month_inputs,
)


def month(*, regular_growth=0.52, dividend=True):
    bounds = Bands.from_anchors(80, 100, 120)
    return MonthInputs(
        month=date(2026, 11, 1),
        anchor_date=date(2026, 10, 30),
        regular_growth=regular_growth,
        growth=bounds,
        dividend=bounds if dividend else None,
    )


def signal(session_number, growth, dividend=100):
    closed = date(2026, 11, 2) + timedelta(days=session_number)
    return SignalDay(closed, closed + timedelta(days=1), growth, dividend, session_number)


def test_high_reentry_moves_one_step_per_day_and_restarts_on_upper():
    rules = month(regular_growth=0.4)
    exit_ = advance(RotationState(), signal(1, 120), rules)
    assert exit_.target == Target(0.0, 1.0, 0.0)
    assert "growth_upper" in exit_.reasons
    assert exit_.stage == "high_exit"

    quarter = advance(exit_.state, signal(2, 70), rules)
    assert quarter.target.growth == pytest.approx(0.10)
    assert quarter.reasons == ("reentry_quarter",)
    assert quarter.stage == "reentry_quarter"

    half = advance(quarter.state, signal(3, 70), rules)
    assert half.target.growth == pytest.approx(0.20)
    assert half.reasons == ("reentry_half",)

    restarted = advance(half.state, signal(4, 120), rules)
    assert restarted.target == Target(0.0, 1.0, 0.0)
    assert restarted.state.rebuild == 0

    quarter = advance(restarted.state, signal(5, 100), rules)
    half = advance(quarter.state, signal(6, math.sqrt(80 * 100)), rules)
    fully_back = advance(half.state, signal(7, 80), rules)
    assert fully_back.target == Target(0.45, 0.55, 0)
    assert fully_back.reasons == ("reentry_complete", "low_45")


def test_low_levels_respect_five_sessions_and_regular_weight_floor():
    rules = month()
    low = advance(RotationState(), signal(1, 64), rules)
    assert low.target == Target(0.52, 0.48, 0)
    assert low.stage == "low_45"
    assert low.state.last_add_session == 1

    too_early = advance(low.state, signal(5, 63), rules)
    assert too_early.action == "hold"
    assert too_early.target == low.target
    assert too_early.reasons == ()

    lower = advance(too_early.state, signal(6, 64), rules)
    lowest = advance(lower.state, signal(11, 64), rules)
    assert lower.target.growth == pytest.approx(0.55)
    assert lowest.target.growth == pytest.approx(0.65)
    assert lowest.target.dividend == pytest.approx(0.35)
    assert lowest.target.cash == 0
    assert lowest.reasons == ("low_65",)

    reset = advance(lowest.state, signal(12, 100), rules)
    assert reset.target == Target(0.52, 0.48, 0)
    assert reset.reasons == ("low_reset",)


@pytest.mark.parametrize("dividend", [True, False])
def test_exit_redirect_changes_with_dividend_upper(dividend):
    rules = month(regular_growth=0.4, dividend=dividend)
    cash = advance(RotationState(), signal(1, 120, 120), rules)
    assert cash.target == Target(0, 0.6, 0.4)
    assert "diversification_to_cash" in cash.reasons

    red = advance(cash.state, signal(2, 110, 119), rules)
    if dividend:
        assert red.target == Target(0, 1, 0)
        assert red.reasons == ("diversification_to_dividend",)
        cash_again = advance(red.state, signal(3, 110, 120), rules)
        assert cash_again.target == cash.target
    else:
        assert red.action == "hold"
        assert red.target == cash.target


def test_dividend_exit_at_five_percent_above_upper_and_restore_below_upper():
    rules = month(regular_growth=0.4)
    before = advance(RotationState(), signal(1, 100, math.nextafter(126, 0)), rules)
    assert before.target == Target(0.4, 0.6, 0)
    exit_ = advance(before.state, signal(2, 100, 126), rules)
    assert exit_.target == Target(0.4, 0, 0.6)
    assert exit_.reasons == ("dividend_overheat_exit",)

    both = advance(exit_.state, signal(3, 120, 120), rules)
    assert both.target == Target(0, 0, 1)
    stay_out = advance(both.state, signal(4, 121, 123), rules)
    assert stay_out.action == "hold"
    assert stay_out.target == both.target
    back = advance(stay_out.state, signal(5, 121, math.nextafter(120, 0)), rules)
    assert back.target == Target(0, 1, 0)
    assert "dividend_overheat_reentry" in back.reasons


def test_fixed_offset_rebases_the_dividend_exit_without_triggering_it():
    rules = month(regular_growth=0.4)
    before = advance(RotationState(), signal(1, 100, 125.75), rules)
    shifted = rebase_month_inputs(rules, PriceBasisChanges("example", dividend=PriceBasisChange(1, -10)))
    assert shifted.dividend_exit == pytest.approx(116)
    equivalent = advance(before.state, signal(2, 100, 115.75), shifted)
    assert equivalent.action == "hold"
    assert equivalent.target == before.target
    reached = advance(equivalent.state, signal(3, 100, 116), shifted)
    assert reached.target == Target(0.4, 0, 0.6)


def test_dividend_rebases_all_frozen_triggers_without_creating_a_buy():
    rules = month(regular_growth=0.4)
    initial = advance(RotationState(), signal(1, 84), rules)
    action = PriceBasisChanges("2026-11-04:159915", growth=PriceBasisChange(1.0, -6.0))
    adjusted = rebase_month_inputs(rules, action)
    assert adjusted.anchor_date == rules.anchor_date
    assert adjusted.dividend == rules.dividend
    assert adjusted.growth == Bands(74, 94, 114, math.sqrt(80 * 100) - 6, 66, 58)
    assert rebase_month_inputs(adjusted, action) == adjusted

    same_price_after_dividend = advance(initial.state, signal(2, 78), adjusted)
    assert same_price_after_dividend.action == "hold"
    assert same_price_after_dividend.target == initial.target
    market_drop = advance(same_price_after_dividend.state, signal(3, 74), adjusted)
    assert market_drop.action == "rebalance"
    assert market_drop.target.growth == pytest.approx(0.45)


def test_month_end_uses_new_month_anchors_without_future_prices():
    dates = tuple(
        day for n in range(730)
        if (day := date(2023, 12, 2) + timedelta(days=n)).weekday() < 5
    )

    class Calendar:
        def sessions(self, start, end):
            return tuple(day for day in dates if start <= day <= end)

    growth = {day: 100 * math.exp(i / 1000 + 0.20 * math.sin(i / 28)) for i, day in enumerate(dates)}
    dividend = {day: 100 * math.exp(i / 2000 + 0.10 * math.sin(i / 30)) for i, day in enumerate(dates)}
    execution = date(2025, 11, 3)
    anchor = date(2025, 10, 31)
    growth_to_close = {day: close for day, close in growth.items() if day <= anchor}
    dividend_to_close = {day: close for day, close in dividend.items() if day <= anchor}
    frozen = build_month_inputs(execution, growth_to_close, dividend_to_close, Calendar())
    assert frozen == build_month_inputs(execution, growth, dividend, Calendar())
    assert frozen.month == date(2025, 11, 1)
    assert frozen.anchor_date == anchor
    assert frozen.growth is not None
    assert frozen.dividend is not None
    assert 0.2 <= frozen.regular_growth <= 0.8

    last = RotationState(target=Target(0.5, 0.5, 0))
    action = advance(last, SignalDay(anchor, execution, frozen.growth.middle, frozen.dividend.middle, 100), frozen)
    assert action.action == "rebalance"
    assert action.reasons == ("month_start",)
    assert action.target.growth == frozen.regular_growth
