"""ASOS 하루 값 → 일 기상·ETo (core/daily_weather.py).

규칙은 cropwater `cropwater_multi.compute_station`·`compute_water_balance`와 같다.
  - 실제 수증기압 ea: 증기압 → 이슬점 → 평균습도 순
  - 일사 Rs: 관측 일사 → 없으면 일조시간으로 추정(FAO-56 식35, 앙스트롬 a=0.25, b=0.50)
  - 빠진 날짜는 행을 넣고, ETo를 못 구한 날은 앞 7일 ETo 평균으로 채운다(filled)
입력은 모두 가짜 값이다.
"""

import datetime as dt

import pytest

from croplib_dp.core.daily_weather import StationMeta, daily_weather, num
from croplib_dp.core.fao56 import daylight_hours, eto_penman_monteith, extra_radiation, svp, wind_2m

STN = StationMeta(lat=37.9, elev_m=75.8, wind_height_m=10.0)


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


def _pm(rs=20.0, ea=1.2, day=dt.date(2026, 5, 1)):
    J = day.timetuple().tm_yday
    return eto_penman_monteith(25.0, 12.0, rs, wind_2m(2.0, 10.0), ea, 75.8, 37.9, J, 1000.0)


def test_num_handles_blank_and_dash():
    assert num("1.5") == 1.5 and num(" 2 ") == 2.0
    assert num("") is None and num("-") is None and num(None) is None and num("null") is None


def test_full_day_matches_penman_monteith():
    r = daily_weather([_item("2026-05-01", sumRn="3.5")], STN)[0]
    assert r["date"] == dt.date(2026, 5, 1)
    assert r["eto_mm"] == pytest.approx(_pm())
    assert r["u2_ms"] == pytest.approx(wind_2m(2.0, 10.0))
    assert (r["tmax_c"], r["tmin_c"], r["tavg_c"]) == (25.0, 12.0, 18.0)
    assert (r["rh_mean_pct"], r["rh_min_pct"], r["wind_ms"], r["sunshine_h"]) == (60.0, 35.0, 2.0, 8.0)
    assert r["rs_mj"] == 20.0 and r["rain_mm"] == 3.5 and r["filled"] is False


def test_blank_rain_means_no_rain():
    """ASOS 일강수는 비가 오지 않은 날 비어 있다 → 0"""
    assert daily_weather([_item("2026-05-01")], STN)[0]["rain_mm"] == 0.0


def test_ea_falls_back_to_dew_point_then_mean_humidity():
    r = daily_weather([_item("2026-05-01", avgPv="")], STN)[0]
    assert r["eto_mm"] == pytest.approx(_pm(ea=svp(9.0)))
    r = daily_weather([_item("2026-05-01", avgPv="", avgTd="")], STN)[0]
    es = (svp(25.0) + svp(12.0)) / 2
    assert r["eto_mm"] == pytest.approx(_pm(ea=0.60 * es))


def test_rs_from_sunshine_when_radiation_missing():
    day = dt.date(2026, 5, 1)
    J = day.timetuple().tm_yday
    rs = (0.25 + 0.5 * 8.0 / daylight_hours(37.9, J)) * extra_radiation(37.9, J)
    r = daily_weather([_item("2026-05-01", sumGsr="")], STN)[0]
    assert r["rs_mj"] == pytest.approx(rs)
    assert r["eto_mm"] == pytest.approx(_pm(rs=rs))


def test_missing_row_inserted_and_filled_with_7day_mean():
    items = [_item(f"2026-05-{d:02d}") for d in range(1, 9) if d != 8] + [_item("2026-05-09")]
    out = daily_weather(items, STN)
    assert [r["date"].day for r in out] == list(range(1, 10))
    gap = out[7]
    assert gap["filled"] is True and gap["rain_mm"] is None and gap["tmax_c"] is None
    assert gap["eto_mm"] == pytest.approx(sum(r["eto_mm"] for r in out[:7]) / 7)
    assert out[8]["filled"] is False


def test_row_without_temperature_is_filled_but_keeps_rain():
    items = [_item("2026-05-01"), _item("2026-05-02", maxTa="", sumRn="12.0")]
    out = daily_weather(items, STN)
    assert out[1]["filled"] is True and out[1]["rain_mm"] == 12.0
    assert out[1]["eto_mm"] == pytest.approx(out[0]["eto_mm"])


def test_first_day_without_eto_is_zero():
    """앞에 자료가 없으면 채울 값이 없다 → 0 (cropwater와 같음)"""
    out = daily_weather([_item("2026-05-01", maxTa="")], STN)
    assert out[0]["eto_mm"] == 0.0 and out[0]["filled"] is True


def test_filled_values_feed_later_means():
    """채운 값도 다음 날의 7일 평균에 들어간다 (cropwater compute_water_balance와 같음)"""
    items = [_item("2026-05-01"), _item("2026-05-02", maxTa=""), _item("2026-05-03", maxTa="")]
    out = daily_weather(items, STN)
    assert out[2]["eto_mm"] == pytest.approx((out[0]["eto_mm"] + out[1]["eto_mm"]) / 2)


def test_order_and_duplicates():
    """순서가 섞여 와도 날짜순, 같은 날이 두 번 오면 마지막 값"""
    out = daily_weather([_item("2026-05-02"), _item("2026-05-01"), _item("2026-05-02", sumRn="4.0")], STN)
    assert [r["date"].day for r in out] == [1, 2] and out[1]["rain_mm"] == 4.0


def test_empty_input():
    assert daily_weather([], STN) == []
