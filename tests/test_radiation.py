"""일사(Rs) 유효성·대체 순서·연속 채움 한도 (core/radiation.py, core/daily_weather.py). 이슈 #44(#28 S0-a).

입력은 모두 가짜 값이다. 지점 이름·번호는 실제 관측소를 닮았지만 값은 시험용이다.
규칙:
  1. 일 단위: 일사가 0.05 × Ra 보다 작으면 결측으로 본다
  2. 달력 월 단위: 자료가 10일 이상이고 그중 무효가 90% 이상이면 그 달 일사를 전부 무효로 본다
  3. 대체 순서: 유효한 일사 → 일조시간(식35) → 기온차(식50, kRs는 지점별) → 비움
  4. 앞 7일 평균으로 채운 날이 연속 7일을 넘으면 8일째부터 ETo를 비운다(filled = True)
"""

import datetime as dt

import pytest

from croplib_dp.core.daily_weather import StationMeta, daily_weather
from croplib_dp.core.fao56 import eto_penman_monteith, extra_radiation, rs_from_sunshine, wind_2m
from croplib_dp.core.radiation import (
    KRS_INLAND,
    clean_observed_rs,
    rs_floor,
)
from croplib_dp.core.solar import ra_rso, rs_s1

LAT, ELEV = 37.9, 75.8
STN = StationMeta(lat=LAT, elev_m=ELEV, wind_height_m=10.0)


def _item(day, **kw):
    it = dict(
        tm=day,
        maxTa="25.0",
        minTa="12.0",
        avgTa="18.0",
        avgRhm="60.0",
        minRhm="35.0",
        avgWs="2.0",
        avgPv="12.0",
        avgTd="9.0",
        avgPa="1000.0",
        sumGsr="20.0",
        sumSsHr="8.0",
        sumRn="",
    )
    it.update(kw)
    return it


def _doy(day: dt.date) -> int:
    return day.timetuple().tm_yday


def _pm(rs, day):
    return eto_penman_monteith(25.0, 12.0, rs, wind_2m(2.0, 10.0), 1.2, ELEV, LAT, _doy(day), 1000.0)


def _temp_rs(day, krs):
    ra, rso = ra_rso(LAT, ELEV, _doy(day))
    return float(rs_s1(25.0, 12.0, ra, rso, krs)[0])


MAY1 = dt.date(2026, 5, 1)


# ── 1. 일 단위 규칙 ───────────────────────────────────────────────────────
def test_floor_is_five_percent_of_extraterrestrial_radiation():
    assert rs_floor(LAT, MAY1) == pytest.approx(0.05 * extra_radiation(LAT, _doy(MAY1)))


def test_zero_radiation_with_sunshine_equals_sunshine_only():
    """일사 0.0은 관측값이 아니다 → 일조시간만 준 경우와 같은 ETo"""
    zero = daily_weather([_item("2026-05-01", sumGsr="0.0")], STN)[0]
    only_ss = daily_weather([_item("2026-05-01", sumGsr="")], STN)[0]
    assert zero["eto_mm"] == pytest.approx(only_ss["eto_mm"])
    assert zero["rs_mj"] == pytest.approx(rs_from_sunshine(LAT, _doy(MAY1), 8.0))
    assert zero["rs_source"] == "sunshine" and zero["filled"] is False


def test_zero_radiation_summer_eto_is_not_cut_by_two_thirds():
    """이슈 #44 배경: 여름 맑은 날 0.0을 그대로 쓰면 ETo가 크게 줄었다"""
    day = dt.date(2026, 8, 4)
    bad = _item("2026-08-04", sumGsr="0.0", sumSsHr="11.0")
    got = daily_weather([bad], STN)[0]["eto_mm"]
    as_if_zero = _pm(0.0, day)
    assert got > as_if_zero * 1.5


def test_zero_radiation_no_sunshine_uses_temperature_range():
    r = daily_weather([_item("2026-05-01", sumGsr="0.0", sumSsHr="")], STN)[0]
    assert r["rs_source"] == "temp_range" and r["filled"] is False
    assert r["rs_mj"] == pytest.approx(_temp_rs(MAY1, KRS_INLAND))
    assert r["eto_mm"] == pytest.approx(_pm(_temp_rs(MAY1, KRS_INLAND), MAY1))


def test_temperature_range_krs_is_per_station_and_default_is_inland():
    item = _item("2026-05-01", sumGsr="", sumSsHr="")
    inland = daily_weather([item], STN)[0]
    coast = daily_weather([item], StationMeta(lat=LAT, elev_m=ELEV, wind_height_m=10.0, krs=0.19))[0]
    assert KRS_INLAND == 0.16 and StationMeta(lat=LAT, elev_m=ELEV).krs == 0.16
    assert inland["rs_mj"] == pytest.approx(_temp_rs(MAY1, 0.16))
    assert coast["rs_mj"] == pytest.approx(_temp_rs(MAY1, 0.19))
    assert coast["rs_mj"] > inland["rs_mj"] and coast["eto_mm"] != pytest.approx(inland["eto_mm"])


def test_yeongju_like_tiny_radiation_is_invalid_in_august():
    """영주형: 8월에 1.0 MJ 는 하한(0.05×Ra) 아래 → 무효, 일조시간으로 대체"""
    day = dt.date(2026, 8, 4)
    assert 1.0 < rs_floor(LAT, day)
    r = daily_weather([_item("2026-08-04", sumGsr="1.0")], STN)[0]
    assert r["rs_source"] == "sunshine"


def test_normal_radiation_is_kept_as_observed():
    r = daily_weather([_item("2026-05-01", sumGsr="20.0")], STN)[0]
    assert r["rs_source"] == "observed" and r["rs_mj"] == 20.0
    assert r["eto_mm"] == pytest.approx(_pm(20.0, MAY1))


def test_value_just_above_floor_is_valid_and_just_below_is_invalid():
    fl = rs_floor(LAT, MAY1)
    assert daily_weather([_item("2026-05-01", sumGsr=f"{fl + 0.01:.2f}")], STN)[0]["rs_source"] == "observed"
    assert daily_weather([_item("2026-05-01", sumGsr=f"{fl - 0.01:.2f}")], STN)[0]["rs_source"] == "sunshine"


def test_missing_negative_or_garbage_radiation_is_not_observed():
    for bad in ("", "-", "-3.0", "abc"):
        assert daily_weather([_item("2026-05-01", sumGsr=bad)], STN)[0]["rs_source"] == "sunshine", bad


def test_no_radiation_no_sunshine_no_temperature_gives_no_source_and_fills():
    items = [_item("2026-05-01"), _item("2026-05-02", sumGsr="", sumSsHr="", maxTa="", minTa="")]
    out = daily_weather(items, STN)
    assert out[1]["rs_source"] is None and out[1]["rs_mj"] is None
    assert out[1]["filled"] is True and out[1]["eto_mm"] == pytest.approx(out[0]["eto_mm"])


# ── 2. 달력 월 단위 규칙 ──────────────────────────────────────────────────
def _feb(zero_days, good_day=None, good=4.57, year=2026):
    out = {}
    for k in range(28):
        d = dt.date(year, 2, 1) + dt.timedelta(days=k)
        out[d] = 0.0 if d.day in zero_days else good
    if good_day is not None:
        out[dt.date(year, 2, good_day)] = good
    return out


def test_jeongseon_february_one_good_day_among_zeros_is_whole_month_invalid():
    """정선군 2월: 27일은 0.0, 2/26 하루만 4.57 → 그 하루도 버린다"""
    feb26 = dt.date(2026, 2, 26)
    assert 4.57 > rs_floor(LAT, feb26)  # 일 단위로만 보면 유효한 값이다
    series = _feb(zero_days=set(range(1, 29)) - {26}, good_day=26)
    cleaned = clean_observed_rs(series, LAT)
    assert all(v is None for v in cleaned.values())


def test_same_case_through_daily_weather_uses_sunshine_for_that_day():
    items = []
    for d in range(1, 29):
        items.append(_item(f"2026-02-{d:02d}", sumGsr=("4.57" if d == 26 else "0.0")))
    out = daily_weather(items, STN)
    assert {r["rs_source"] for r in out} == {"sunshine"}


def test_sensor_repaired_next_month_uses_radiation_again():
    """센서 수리 사례: 1월 전부 0.0, 2월부터 정상 → 2월부터 관측 일사를 쓴다"""
    series = {dt.date(2026, 1, d): 0.0 for d in range(1, 32)}
    series.update({dt.date(2026, 2, d): 6.0 for d in range(1, 29)})
    cleaned = clean_observed_rs(series, LAT)
    assert all(cleaned[d] is None for d in series if d.month == 1)
    assert all(cleaned[d] == 6.0 for d in series if d.month == 2)


def test_repair_through_daily_weather_switches_source_at_month_boundary():
    items = [_item(f"2026-01-{d:02d}", sumGsr="0.0") for d in range(1, 32)]
    items += [_item(f"2026-02-{d:02d}", sumGsr="6.0") for d in range(1, 29)]
    out = daily_weather(items, STN)
    assert {r["rs_source"] for r in out if r["date"].month == 1} == {"sunshine"}
    assert {r["rs_source"] for r in out if r["date"].month == 2} == {"observed"}


def test_month_with_fewer_than_ten_days_uses_daily_rule_only():
    """자료 9일(8일 0.0 + 하루 정상) → 일 단위 규칙만: 정상 하루는 살아남는다"""
    series = {dt.date(2026, 2, d): 0.0 for d in range(1, 9)}
    series[dt.date(2026, 2, 9)] = 4.57
    cleaned = clean_observed_rs(series, LAT)
    assert cleaned[dt.date(2026, 2, 9)] == 4.57
    assert all(cleaned[dt.date(2026, 2, d)] is None for d in range(1, 9))


def test_month_ratio_boundary_ninety_percent():
    """자료 10일: 무효 9일(90%) → 그 달 전체 무효 / 무효 8일(80%) → 정상 2일 유지"""
    nine = {dt.date(2026, 2, d): 0.0 for d in range(1, 10)} | {dt.date(2026, 2, 10): 4.57}
    assert all(v is None for v in clean_observed_rs(nine, LAT).values())
    eight = {dt.date(2026, 2, d): 0.0 for d in range(1, 9)} | {
        dt.date(2026, 2, 9): 4.57,
        dt.date(2026, 2, 10): 4.57,
    }
    got = clean_observed_rs(eight, LAT)
    assert got[dt.date(2026, 2, 9)] == 4.57 and got[dt.date(2026, 2, 10)] == 4.57


def test_missing_days_are_not_counted_as_data():
    """값이 없는 날(None)은 '자료'로 세지 않는다: 자료 9일 + 빈 날 19일 → 10일 미만"""
    series = {dt.date(2026, 2, d): None for d in range(1, 29)}
    for d in range(1, 9):
        series[dt.date(2026, 2, d)] = 0.0
    series[dt.date(2026, 2, 9)] = 4.57
    assert clean_observed_rs(series, LAT)[dt.date(2026, 2, 9)] == 4.57


def test_months_are_judged_independently_and_years_separately():
    series = {dt.date(2025, 2, d): 0.0 for d in range(1, 29)}
    series.update({dt.date(2026, 2, d): 6.0 for d in range(1, 29)})  # 같은 2월이라도 해가 다르면 따로
    cleaned = clean_observed_rs(series, LAT)
    assert all(cleaned[d] is None for d in series if d.year == 2025)
    assert all(cleaned[d] == 6.0 for d in series if d.year == 2026)


def test_healthy_station_is_unchanged():
    series = {dt.date(2026, 5, d): 15.0 + d % 5 for d in range(1, 32)}
    assert clean_observed_rs(series, LAT) == series


# ── 3. 연속 채움 한도 ─────────────────────────────────────────────────────
def _blank(day):
    return _item(day, sumGsr="", sumSsHr="", maxTa="", minTa="")


def test_fill_up_to_seven_consecutive_days_then_blank_from_eighth():
    items = [_item("2026-05-01")] + [_blank(f"2026-05-{d:02d}") for d in range(2, 12)]
    out = daily_weather(items, STN)
    assert all(r["filled"] and r["eto_mm"] is not None for r in out[1:8])  # 2~8일: 7일 채움
    assert all(r["filled"] and r["eto_mm"] is None for r in out[8:])  # 9일부터 비움
    assert out[1]["eto_mm"] == pytest.approx(out[0]["eto_mm"])


def test_normal_day_in_between_restarts_the_count():
    items = [_item("2026-05-01")] + [_blank(f"2026-05-{d:02d}") for d in range(2, 9)]  # 7일 채움
    items += [_item("2026-05-09")] + [_blank(f"2026-05-{d:02d}") for d in range(10, 17)]  # 다시 7일 채움
    out = daily_weather(items, STN)
    assert all(r["eto_mm"] is not None for r in out)
    assert out[8]["filled"] is False


def test_missing_rows_count_toward_the_limit_too():
    """ASOS 행이 아예 없는 날(빠진 날짜)도 같은 한도를 쓴다"""
    items = [_item("2026-05-01"), _item("2026-05-12")]
    out = daily_weather(items, STN)
    gap = out[1:-1]
    assert [r["eto_mm"] is not None for r in gap] == [True] * 7 + [False] * 3


def test_limit_does_not_change_first_day_zero_rule():
    """앞에 자료가 없는 첫날은 지금처럼 0.0 으로 채운다(기존 규칙). 한도 세기는 그날부터 시작"""
    out = daily_weather([_blank("2026-05-01")], STN)
    assert out[0]["eto_mm"] == 0.0 and out[0]["filled"] is True


def test_values_after_a_blank_run_are_computed_normally():
    items = [_item("2026-05-01")] + [_blank(f"2026-05-{d:02d}") for d in range(2, 12)] + [_item("2026-05-12")]
    out = daily_weather(items, STN)
    assert out[-1]["filled"] is False and out[-1]["eto_mm"] == pytest.approx(_pm(20.0, dt.date(2026, 5, 12)))
