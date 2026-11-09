"""S4-b (#89): 관측만 하는 지점이 예보 지점으로 바뀔 때 같은 날 값이 어긋나는지.

- 관측만 하는 지점은 ASOS를 그해 1/1부터 받아 계산하고(결정 0026), 예보 지점은 3/1(작기·토양 시작)부터 받아 계산한다.
- 같은 ASOS 입력으로 두 방식의 3/1 이후 `weather_daily`·`soil_water_daily`를 비교한다. **고치지 않고** 어긋남을
  보고하는 시험이다. 실제 ASOS 응답으로 한 비교 결과는 결정 0029 "S4-b 결과"에 있다(응답 원본은 저장소에 두지 않음).
- `xfail(strict=True)`는 "바라는 모습"인데 지금은 어긋나는 것이다. 고쳐서 어긋남이 사라지면 시험이 알려 주므로
  그때 `xfail`을 지운다.
입력은 모두 가짜 값이다.
"""

# ruff: noqa: E501

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.core.daily_weather import StationMeta, daily_weather  # noqa: E402
from croplib_dp.core.soil_water import soil_water_rows  # noqa: E402
from test_collector import ISSUE, ISSUES, KEY, FakeApi, FakeClock, _asos_item  # noqa: E402
from test_s4a import _dicts_with_nonrep  # noqa: E402

START = dt.date(2026, 3, 1)
END = dt.date(2026, 6, 9)
META = StationMeta(lat=37.9, elev_m=75.8, wind_height_m=10.0)
SOILS = {"loam": (0.25, 0.12), "sand": (0.12, 0.045), "clay": (0.36, 0.22)}
SEVEN_DAY = ("eto_7d_mm", "rain_7d_mm", "days_to_dry")  # days_to_dry 는 7일 평균 ETo 로 구한다


def _items(eto_gap=()):
    """1/1~6/9 ASOS 일자료. eto_gap 날짜는 최고기온이 비어 ETo를 구할 수 없다(앞 7일 평균으로 채워지는 날)."""
    out, d = [], dt.date(2026, 1, 1)
    while d <= END:
        out.append(_asos_item(d, maxTa="") if d in eto_gap else _asos_item(d))
        d += dt.timedelta(days=1)
    return out


def _both(items):
    full = daily_weather(items, META)  # 관측만 하는 지점 방식: 1/1부터
    rep = daily_weather([i for i in items if i["tm"] >= START.isoformat()], META)  # 예보 지점 방식: 3/1부터
    return full, rep


def _soil(weather, fc, wp):
    return {r["date"]: r for r in soil_water_rows([w for w in weather if w["date"] >= START], fc, wp)}


def test_weather_from_march_is_identical_when_eto_has_no_gap():
    full, rep = _both(_items())
    assert [r for r in full if r["date"] >= START] == rep


def test_soil_rows_are_identical_except_the_7_day_figures_in_the_first_week_of_march():
    full, rep = _both(_items())
    for fc, wp in SOILS.values():
        a = {r["date"]: r for r in soil_water_rows(full, fc, wp) if r["date"] >= START}
        b = _soil(rep, fc, wp)
        assert set(a) == set(b)
        for day in a:
            for col, v in a[day].items():
                if col in SEVEN_DAY and day < dt.date(2026, 3, 8):
                    continue  # 예보 지점 방식은 2월 자료가 없어 3/1~3/7의 7일 합·평균이 짧다
                assert b[day][col] == v, (day, col)
        # 위 예외가 실제로 있다(없어졌다면 예외 규칙을 지워야 한다)
        assert a[START]["eto_7d_mm"] != b[START]["eto_7d_mm"]
        assert a[START]["rain_7d_mm"] is not None and b[START]["rain_7d_mm"] is not None


@pytest.mark.xfail(
    strict=True,
    reason="3/1 근처에 ETo 결측이 있으면 앞 7일 평균으로 채우는데, 3/1부터 받으면 2월 자료가 없어 채운 값이 다르다(S4-b 보고)",
)
def test_filled_eto_in_early_march_matches():
    full, rep = _both(_items(eto_gap={dt.date(2026, 3, 2), dt.date(2026, 3, 3)}))
    assert [r for r in full if r["date"] >= START] == rep


def test_the_filled_eto_difference_stays_inside_the_filled_days():
    """어긋남이 번지지 않는다: 채운 날(3/2·3/3)만 다르고 그 밖의 날 기상값은 같다."""
    full, rep = _both(_items(eto_gap={dt.date(2026, 3, 2), dt.date(2026, 3, 3)}))
    a = {r["date"]: r for r in full if r["date"] >= START}
    b = {r["date"]: r for r in rep}
    differing = {d for d in a if a[d] != b[d]}
    assert differing and differing <= {dt.date(2026, 3, 2), dt.date(2026, 3, 3)}
    assert all(a[d]["filled"] and b[d]["filled"] for d in differing)  # 둘 다 '채운 값' 표시가 있다


def _forecast_run(station):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    return W.run_slot("아침", ISSUE, _dicts_with_nonrep(), KEY, http=api, clock=clock, stations=[station])


@pytest.mark.xfail(
    strict=True,
    reason="대표 밖 예보 지점이 평소 회차마다 weather_daily를 3/1~어제 전부(약 250줄) 다시 올린다 — 소급해 둔 3월 초 줄을 "
    "3/1부터 계산한 값으로 덮어쓰고 쓰기량이 늘어난다. 관측만 하는 지점처럼 최근 14일만 올리는 것이 바람(S4-b 보고)",
)
def test_a_non_representative_forecast_station_uploads_only_the_recent_weather_window():
    r = _forecast_run("301")
    rows = [x for x in r.weather_daily if x["stn_id"] == "301"]
    assert len(rows) <= W.SOIL_UPLOAD_DAYS


def test_the_representative_still_uploads_the_season_from_march():
    """대표 지점(계산 조건·전망이 있음)은 3/1부터 전부 올린다 — 이 규칙은 그대로다."""
    r = _forecast_run("101")
    rows = [x for x in r.weather_daily if x["stn_id"] == "101"]
    assert min(x["date"] for x in rows) <= "2026-04-01" and len(rows) > W.SOIL_UPLOAD_DAYS
