"""작물 공통 물 상태의 "창(최근 N일) 재계산" (core/soil_water.py). 이슈 #76(#28 S2), 사용자 조건(2026-11-08):

- 창 시작 전날의 건조량(Dr)·심층침투(DP)·채움 표시를 DB 줄에서 받아 **이어서** 계산한다.
- 같은 입력에서 "창 재계산"의 창 안 값은 "전체 재계산"의 창 안 값과 **똑같다**.
- 이어 계산할 시작점이 필요한데 없으면 `MissingPrior` 로 알린다(조용히 0으로 시작하지 않는다).
입력은 모두 가짜 값이다.
"""

import datetime as dt

import pytest

from croplib_dp.core.soil_water import MissingPrior, soil_water_rows, window_needs_prior
from croplib_dp.core.water_balance import Soil, observed_water_balance

FC, WP = 0.25, 0.12
D = dt.date


def _days(start: dt.date, end: dt.date) -> list[dict]:
    """가짜 일 기상값. 비가 드문드문 크게 오고(물 고임·DP 발생), 며칠은 ETo 를 비워 채움(filled) 표시가
    퍼지게 한다."""
    out, d = [], start
    while d <= end:
        i = d.toordinal()
        rain = 40.0 if i % 11 == 0 else (6.0 if i % 5 == 0 else 0.0)
        filled = i % 29 in (3, 4)
        out.append(dict(date=d, eto_mm=2.0 + (i % 7) * 0.6, rain_mm=rain, filled=filled))
        d += dt.timedelta(days=1)
    return out


FULL_START, FULL_END = D(2026, 1, 1), D(2026, 11, 7)


def _prior_from(rows: list[dict], day: dt.date) -> dict:
    r = next(x for x in rows if x["date"] == day)
    return dict(dr_mm=r["dr_mm"], dp_mm=r["dp_mm"], filled=r["filled"])


@pytest.mark.parametrize(
    "end",
    [D(2026, 3, 20), D(2026, 4, 30), D(2026, 6, 15), D(2026, 7, 29), D(2026, 9, 9), D(2026, 11, 7)],
)
def test_window_rows_equal_the_full_recalculation_inside_the_window(end):
    days = _days(FULL_START, end)
    full = soil_water_rows(days, FC, WP)
    ws = end - dt.timedelta(days=13)
    prior = _prior_from(full, ws - dt.timedelta(days=1))
    got = soil_water_rows(days, FC, WP, window_start=ws, prior=prior)
    want = [r for r in full if r["date"] >= ws]
    assert [r["date"] for r in got] == [r["date"] for r in want] and len(got) == 14
    assert got == want  # 모든 열(7일 합·상태·days_to_dry·filled 포함)이 같다


def test_the_equality_holds_for_every_window_start_through_the_season():
    days = _days(FULL_START, FULL_END)
    full = soil_water_rows(days, FC, WP)
    for k in range(0, 300, 7):
        ws = D(2026, 3, 2) + dt.timedelta(days=k)
        if ws > FULL_END:
            break
        prior = _prior_from(full, ws - dt.timedelta(days=1))
        got = soil_water_rows(days, FC, WP, window_start=ws, prior=prior)
        assert got == [r for r in full if r["date"] >= ws], ws


def test_the_window_includes_rain_pool_and_filled_days_so_the_check_is_not_empty():
    days = _days(FULL_START, FULL_END)
    full = soil_water_rows(days, FC, WP)
    assert {r["status"] for r in full} >= {"pool", "winter"}
    assert any(r["filled"] for r in full)


def test_prior_with_filled_flag_carries_the_taint_like_the_full_run():
    """채운 값에 기댄 날(filled)은 Dr 이 0이 될 때까지 이어진다. 시작점의 filled 를 넘기지 않으면 창
    첫날들이 달라진다."""
    days = _days(FULL_START, D(2026, 8, 31))
    full = soil_water_rows(days, FC, WP)
    # 창 시작 전날이 filled 인 날을 찾는다
    ws = next(
        r["date"] + dt.timedelta(days=1)
        for r in full
        if r["filled"] and r["status"] != "winter" and r["date"] > D(2026, 4, 1) and r["dr_mm"] > 1.0
    )
    prior = _prior_from(full, ws - dt.timedelta(days=1))
    assert prior["filled"] is True
    got = soil_water_rows(days, FC, WP, window_start=ws, prior=prior)
    want = [r for r in full if r["date"] >= ws]
    assert got[:3] == want[:3]
    wrong = soil_water_rows(days, FC, WP, window_start=ws, prior=dict(prior, filled=False))
    assert [r["filled"] for r in wrong[:1]] != [r["filled"] for r in want[:1]] or wrong != want


def test_pool_status_uses_the_prior_days_dp():
    """물 고임 판정은 전날 DP 도 본다. 5/10 큰 비(120 mm, DP 큼) → 5/11 비가 ETo 보다 조금 많아
    Dr 0 이고 DP 는 1 미만: 전날 DP 때문에 pool 이다.
    창이 5/11에서 시작해도 시작점의 DP 를 받아 같은 판정이 나와야 한다."""
    days = []
    d = D(2026, 3, 1)
    while d <= D(2026, 6, 30):
        rain = {D(2026, 5, 10): 120.0, D(2026, 5, 11): 3.5}.get(d, 0.0)
        days.append(dict(date=d, eto_mm=3.0, rain_mm=rain, filled=False))
        d += dt.timedelta(days=1)
    full = soil_water_rows(days, FC, WP)
    day11 = next(r for r in full if r["date"] == D(2026, 5, 11))
    assert day11["dp_mm"] < 1.0 and day11["dr_mm"] < 0.05 and day11["status"] == "pool"  # 전날 DP 로만 pool
    ws = D(2026, 5, 11)
    prior = _prior_from(full, D(2026, 5, 10))
    assert prior["dp_mm"] >= 1.0
    got = soil_water_rows(days, FC, WP, window_start=ws, prior=prior)
    assert got == [r for r in full if r["date"] >= ws]
    assert got[0]["status"] == "pool"
    no_dp = soil_water_rows(days, FC, WP, window_start=ws, prior=dict(prior, dp_mm=None))
    assert no_dp[0]["status"] != "pool"  # 시작점 DP 를 못 받으면 판정이 달라진다 = 위 시험이 의미가 있다


# ── 시작점이 필요한가 ──────────────────────────────────────────────────────
def test_window_needs_prior_only_when_it_starts_inside_the_active_period():
    # 3/1 이후 시작 → 필요
    assert window_needs_prior(D(2026, 7, 1), D(2026, 7, 14))
    assert window_needs_prior(D(2026, 11, 20), D(2026, 12, 3))  # 11월 날짜가 창에 있다
    assert window_needs_prior(D(2026, 3, 2), D(2026, 3, 15))
    # 3/1 이 창의 첫 활동일이면 Dr = 0 으로 새로 시작 → 필요 없음
    assert not window_needs_prior(D(2026, 2, 20), D(2026, 3, 5))
    assert not window_needs_prior(D(2026, 3, 1), D(2026, 3, 14))
    # 겨울만 있는 창 → 필요 없음
    assert not window_needs_prior(D(2026, 12, 5), D(2026, 12, 18))
    assert not window_needs_prior(D(2026, 1, 5), D(2026, 1, 18))
    assert not window_needs_prior(D(2027, 1, 20), D(2027, 2, 2))
    # 12월 1일 창(전날 11/30 은 활동기지만 창 안에 활동일이 없다) → 필요 없음
    assert not window_needs_prior(D(2026, 12, 1), D(2026, 12, 14))


def test_missing_prior_raises_instead_of_starting_from_zero_silently():
    days = _days(FULL_START, D(2026, 7, 14))
    with pytest.raises(MissingPrior):
        soil_water_rows(days, FC, WP, window_start=D(2026, 7, 1), prior=None)
    with pytest.raises(MissingPrior):
        soil_water_rows(
            days, FC, WP, window_start=D(2026, 7, 1), prior=dict(dr_mm=None, dp_mm=None, filled=False)
        )


def test_no_prior_needed_when_the_window_starts_the_season_or_is_winter():
    days = _days(FULL_START, D(2026, 3, 14))
    got = soil_water_rows(days, FC, WP, window_start=D(2026, 3, 1), prior=None)
    assert got == [r for r in soil_water_rows(days, FC, WP) if r["date"] >= D(2026, 3, 1)]
    wdays = _days(D(2026, 1, 1), D(2026, 1, 18))
    rows = soil_water_rows(wdays, FC, WP, window_start=D(2026, 1, 5), prior=None)
    assert len(rows) == 14 and {r["status"] for r in rows} == {"winter"}


def test_window_start_default_keeps_the_old_behaviour():
    days = _days(FULL_START, D(2026, 6, 9))
    assert soil_water_rows(days, FC, WP)[0]["date"] == FULL_START
    assert len(soil_water_rows(days, FC, WP)) == len(days)


def test_tainted0_changes_only_the_start_flag_of_the_water_balance():
    soil = Soil.from_soil(FC, WP, zr_m=0.5, p=0.40, dr0_mm=12.0)
    days = [
        dict(date=D(2026, 6, 1) + dt.timedelta(days=k), eto_mm=3.0, rain_mm=0.0, filled=False)
        for k in range(3)
    ]
    base = observed_water_balance(days, None, soil)
    assert [r["filled"] for r in base] == [False, False, False]
    on = observed_water_balance(days, None, soil, tainted0=True)
    assert [r["filled"] for r in on] == [True, True, True]  # Dr 이 0보다 큰 동안 이어진다
    assert [r["dr_mm"] for r in on] == [r["dr_mm"] for r in base]
