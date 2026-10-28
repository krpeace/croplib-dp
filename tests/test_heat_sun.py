"""적산온도·적산일사 (core/heat_sun.py).

cropweather `tests/test_temp.py`의 calc_gdd·도달일 테스트를 그대로 가져와 같은 숫자가 나오는지 확인하고,
CropMon 표(heat_daily·sun_daily)에 넣는 한 해 표·평년·며칠 빠른가를 더 시험한다. 입력은 모두 가짜 값이다.
"""

import datetime as dt

import pytest

from croplib_dp.core.heat_sun import (
    calc_gdd,
    gdd_reaching_dates,
    lead_days,
    normals_by_day,
    year_table,
)


# ── cropweather tests/test_temp.py 그대로 ───────────────────────────────
class TestCalcGDD:
    def test_maxmin_normal(self):
        assert calc_gdd(13.5, 20, 8, 10, method="maxmin") == 4.0

    def test_maxmin_below_base(self):
        assert calc_gdd(5.0, 8, 2, 10, method="maxmin") == 0.0

    def test_avg_normal(self):
        assert calc_gdd(15, 20, 10, 10, method="avg") == 5.0

    def test_avg_below_base(self):
        assert calc_gdd(8, 12, 4, 10, method="avg") == 0.0

    def test_aquacrop_above_upper(self):
        assert calc_gdd(36, 40, 32, 10, 35, method="aquacrop") == 25.0

    def test_aquacrop_normal_range(self):
        assert calc_gdd(20, 25, 15, 10, 35, method="aquacrop") == 10.0

    def test_none_handling_maxmin(self):
        assert calc_gdd(None, 20, 10, 10, method="maxmin") == 5.0
        assert calc_gdd(15, None, 10, 10, method="maxmin") is None

    def test_none_handling_avg(self):
        assert calc_gdd(None, 20, 10, 10, method="avg") is None

    def test_unknown_method(self):
        with pytest.raises(ValueError):
            calc_gdd(15, 20, 10, 10, method="xx")


def test_reaching_dates_synthetic():
    rows = [
        {"year": 2024, "month": 4, "day": d, "avg_ta": 20.0, "max_ta": 25.0, "min_ta": 15.0}
        for d in range(1, 31)
    ]
    reaching = gdd_reaching_dates(rows, t_base=10, milestones=[100, 200, 300, 500])
    assert reaching[2024][100] == "4-10"
    assert reaching[2024][200] == "4-20"
    assert reaching[2024][300] == "4-30"
    assert reaching[2024][500] is None


# ── 평년 (같은 날의 평균) ────────────────────────────────────────────────
def test_normals_by_day_mean_of_years_and_feb29():
    D = dt.date
    vals = {
        D(2016, 1, 1): 2.0,
        D(2017, 1, 1): 4.0,
        D(2018, 1, 1): None,  # 결측은 빼고 평균
        D(2016, 2, 29): 6.0,
        D(2020, 2, 29): 8.0,
        D(2026, 1, 1): 100.0,  # 평년 기간 밖
    }
    n = normals_by_day(vals, 2016, 2025)
    assert n[(1, 1)] == 3.0
    assert n[(2, 29)] == 7.0
    assert (3, 1) not in n


# ── 며칠 빠른가 ──────────────────────────────────────────────────────────
def test_lead_days():
    normal_cum = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]  # 하루 1씩
    assert lead_days(5.0, normal_cum, 5) == 0  # 평년과 같음
    assert lead_days(8.0, normal_cum, 5) == 3  # 평년은 3일 뒤에야 닿음 → 3일 빠름
    assert lead_days(3.0, normal_cum, 5) == -2  # 평년은 이틀 전에 닿았음 → 2일 늦음
    assert lead_days(99.0, normal_cum, 5) is None  # 평년이 그해 안에 못 닿음
    assert lead_days(None, normal_cum, 5) is None
    assert lead_days(0.0, [0, 0, 0, 1], 2) == 0  # 둘 다 아직 0


# ── 한 해 표 ─────────────────────────────────────────────────────────────
def test_year_table_cum_normal_fill_and_future():
    D = dt.date
    normal = {(1, d): 1.0 for d in range(1, 32)} | {(m, d): 1.0 for m in range(2, 13) for d in range(1, 32)}
    obs = {D(2026, 1, 1): 2.0, D(2026, 1, 2): None, D(2026, 1, 3): 3.0}  # 1/2 결측
    rows = year_table(2026, obs, normal, last_obs=D(2026, 1, 3))
    assert len(rows) == 365 and rows[0]["date"] == D(2026, 1, 1) and rows[-1]["date"] == D(2026, 12, 31)
    r1, r2, r3, r4 = rows[:4]
    assert (r1["day"], r1["cum"], r1["filled"]) == (2.0, 2.0, False)
    assert (r2["day"], r2["cum"], r2["filled"]) == (1.0, 3.0, True)  # 결측은 평년 값으로 채움
    assert (r3["day"], r3["cum"]) == (3.0, 6.0)
    assert (r4["day"], r4["cum"]) == (None, None)  # 아직 오지 않은 날
    assert [r["normal_cum"] for r in rows[:4]] == [1.0, 2.0, 3.0, 4.0]
    assert r3["lead"] == 3  # 누적 6 = 평년 6일째(1/6) → 3일 빠름
    assert r4["lead"] is None and rows[-1]["normal_cum"] == 365.0


def test_year_table_missing_normal_day_counts_zero_and_no_obs():
    D = dt.date
    rows = year_table(2026, {}, {}, last_obs=None)
    assert rows[0]["normal_day"] is None and rows[0]["normal_cum"] == 0.0
    assert all(r["cum"] is None for r in rows)
    assert rows[0]["date"] == D(2026, 1, 1)


def test_common_crop_base_5c_boundary_and_milestones_1000_2000():
    """작물 공통 적산온도(기준 5℃, 단계 1,000·2,000 ℃·일): 평균 5℃ 이하 날은 0, 도달일 계산."""
    assert calc_gdd(5.0, 8.0, 2.0, t_base=5.0) == 0.0  # (8+2)/2 = 5.0 -> 0
    assert calc_gdd(3.0, 6.0, 0.0, t_base=5.0) == 0.0  # 5℃ 미만 -> 0
    assert calc_gdd(5.5, 8.0, 3.0, t_base=5.0) == pytest.approx(0.5)  # 5℃ 바로 위

    # 하루 20 ℃·일씩(평균 25℃, 기준 5℃): 50일째 1,000 도달, 100일째 2,000 도달
    start = dt.date(2026, 3, 1)
    rows = []
    for i in range(120):
        d = start + dt.timedelta(days=i)
        rows.append(
            {"year": d.year, "month": d.month, "day": d.day, "avg_ta": 25.0, "max_ta": 30.0, "min_ta": 20.0}
        )
    reaching = gdd_reaching_dates(rows, t_base=5.0, milestones=[1000, 2000])
    d1000 = start + dt.timedelta(days=49)
    d2000 = start + dt.timedelta(days=99)
    assert reaching[2026][1000] == f"{d1000.month}-{d1000.day}"
    assert reaching[2026][2000] == f"{d2000.month}-{d2000.day}"
