"""어제 ASOS 일자료가 빈 줄로 올 때 (이슈 #100, 2026-11-11 17시 회차 사고). 입력은 모두 가짜 값이다.

2026-11-11 17시 회차: ASOS 11/10(어제) 행이 날짜만 있고 값이 전부 빈 줄로 와서(아침 09:20에는 값이 있다가 오후에 빈 줄) 대표 10곳이
전부 `2026-11-10을 진행할 예보가 없습니다(아침 발표 D+0)`로 실패했다. 어제 관측 ETo가 없으면 마지막 관측일이 하루 앞이 되어 어제가
"지연일"이 되고, 그날의 아침(02시) 발표(17시 회차 기준 39시간 전)가 24시간 창 밖이라 없다.
- A(지연일 대체): 지연일의 아침 예보가 없으면 실패하지 않고 채운 줄(앞 7일 평균 ETo)로 출발해 전망을 만든다. 결과는 기존 `백업` 구분.
- B(필요한 앞선 발표 재시도): 3시간 전과 02시 회차 24시간 전·17시 회차 15시간 전 발표는 30초·90초 뒤 두 번 더 받는다.
- C(빈 줄 = 아직 없음): 모든 값이 빈 행은 행이 없는 날로 본다 — 강수 0.0 가짜 값과 7일 평균 ETo 줄을 DB에 쓰지 않는다.
"""

# ruff: noqa: E501

import datetime as dt
import json
import sys
import urllib.parse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.core.daily_weather import StationMeta, daily_weather  # noqa: E402
from croplib_dp.core.outlook import lag_days_without_forecast  # noqa: E402
from croplib_dp.core.water_balance import observed_water_balance  # noqa: E402
from test_collector import ISSUE, ISSUES, KEY, FakeApi, FakeClock, _asos_item, _dicts  # noqa: E402
from test_s4a import _dicts_with_nonrep  # noqa: E402

TS = pd.Timestamp
ISS17 = [TS("2026-06-09 02:00") + pd.Timedelta(hours=3 * k) for k in range(16)]  # 6/9 02시 ~ 6/10 23시
SLOT17 = TS("2026-06-10 17:00")


class EmptyLastApi(FakeApi):
    """ASOS의 마지막 행(어제)을 날짜만 있고 값이 전부 빈 줄로 돌려준다(2026-11-11 17시 회차와 같음)."""

    def __call__(self, url, timeout):
        status, body = super().__call__(url, timeout)
        if "startDt" in url:
            js = json.loads(body)
            items = js["response"]["body"]["items"]["item"]
            if items:
                last = items[-1]
                items[-1] = {"tm": last["tm"], **{k: "" for k in last if k != "tm"}}
            body = json.dumps(js).encode()
        return status, body


class FlakyApi(FakeApi):
    """지정한 발표(`YYYYMMDDHHMM`)를 앞 n번 시간 초과로 만든다(ReadTimeout 흉내)."""

    def __init__(self, *a, fail=None, **k):
        super().__init__(*a, **k)
        self.fail = dict(fail or {})

    def __call__(self, url, timeout):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if "base_date" in q:
            key = q["base_date"] + q["base_time"]
            if self.fail.get(key, 0) > 0:
                self.fail[key] -= 1
                self.calls.append(url)
                raise TimeoutError("Read timed out. (read timeout=30)")
        return super().__call__(url, timeout)


def _run17(stations=("101",), d=None, api_cls=EmptyLastApi):
    clock = FakeClock("2026-06-10 17:20")
    api = api_cls(clock, ISS17)
    r = W.run_slot("저녁", SLOT17, d or _dicts(), KEY, http=api, clock=clock, stations=list(stations))
    return r, api


# ── 오늘 사고 재현 (A) ───────────────────────────────────────────────────
def test_17_slot_with_an_empty_last_asos_row_does_not_fail_the_representative_station():
    """마지막 행(어제)이 빈 줄이어도 대표 지점은 실패하지 않고 전망을 만든다(수정 전에는 '진행할 예보가 없습니다'로 실패)."""
    r, _ = _run17()
    run = next(x for x in r.forecast_runs if x["stn_id"] == "101")
    assert run["result"] == "백업: 직전 발표", run  # 기존 백업 구분(결과 값 목록 그대로)
    assert run["error"] is None and not r.problems and not r.failed
    assert (
        len([x for x in r.water_outlook if x["stn_id"] == "101"]) == 4 * 3
    )  # 사과 k3·k1 + 배추 k1, 리드 0~3일
    assert len(r.water_outlook_summary) == 3


def test_the_filled_yesterday_row_is_marked_and_uses_the_7_day_mean_eto():
    r, _ = _run17()
    rows = sorted(
        (x for x in r.water_balance_daily if x["scenario_id"] == "101-apple-main-k3-loam-2026"),
        key=lambda x: x["date"],
    )
    last, before = rows[-1], rows[-8:-1]
    assert last["date"] == "2026-06-09" and last["eto_src"] == "없음" and last["filled"] is True
    mean = sum(x["eto_mm"] for x in before) / 7
    assert abs(last["eto_mm"] - mean) < 1e-9  # 앞 7일 평균 ETo
    assert all(x["eto_src"] == "관측" for x in rows[:-1] if x["date"] >= "2026-06-02")


def test_the_empty_row_is_not_written_to_weather_or_soil_rows():
    """C: 빈 줄을 '아직 없음'으로 본다 — 어제(6/9) 줄이 weather_daily·soil_water_daily에 가짜 값(강수 0·평균 ETo)으로 안 쓰인다."""
    d = _dicts_with_nonrep()  # 301 은 계산 조건이 없는 대표 밖 예보 지점
    r, _ = _run17(stations=("101", "301"), d=d)
    for stn in ("101", "301"):
        days = sorted(x["date"] for x in r.weather_daily if x["stn_id"] == stn)
        assert days and days[-1] == "2026-06-08", (stn, days[-3:])
        soil = sorted({x["date"] for x in r.soil_water_daily if x["stn_id"] == stn})
        assert soil and soil[-1] == "2026-06-08"
    run301 = next(x for x in r.forecast_runs if x["stn_id"] == "301")
    assert run301["result"] == "정시 성공"  # 계산 조건이 없는 지점은 전과 같다
    s301 = next(s for s in r.stations if s["stn_id"] == "301")
    assert s301["asos_last"] == "2026-06-08"  # 감시가 "어제 관측 없음"으로 센다


# ── C: 빈 줄 처리 (순수 함수) ────────────────────────────────────────────
META = StationMeta(lat=37.9, elev_m=75.8, wind_height_m=10.0)


def _items(last, empty_at=()):
    out, d = [], dt.date(2026, 6, 1)
    while d <= last:
        it = _asos_item(d)
        if d in empty_at:
            it = {"tm": d.isoformat(), **{k: "" for k in it if k != "tm"}}
        out.append(it)
        d += dt.timedelta(days=1)
    return out


def test_trailing_empty_row_is_dropped_and_a_middle_empty_row_is_a_missing_day():
    rows = daily_weather(_items(dt.date(2026, 6, 9), empty_at={dt.date(2026, 6, 9)}), META)
    assert rows[-1]["date"] == dt.date(2026, 6, 8)  # 끝의 빈 줄은 "아직 없음"
    mid = daily_weather(_items(dt.date(2026, 6, 9), empty_at={dt.date(2026, 6, 4)}), META)
    day4 = next(r for r in mid if r["date"] == dt.date(2026, 6, 4))
    assert (
        day4["rain_mm"] is None and day4["filled"] is True and day4["eto_mm"] is not None
    )  # 행 없는 날과 같다(강수 0.0 아님)
    assert mid[-1]["date"] == dt.date(2026, 6, 9)


def test_a_dry_day_row_with_temperatures_is_not_empty():
    """비 안 온 날은 강수 칸만 빈다 — 기온이 있으면 정상 행이다."""
    rows = daily_weather(_items(dt.date(2026, 6, 3)), META)
    assert rows[-1]["rain_mm"] == 0.0 or rows[-1]["rain_mm"] == 25.0
    assert rows[-1]["date"] == dt.date(2026, 6, 3) and rows[-1]["filled"] is False


# ── A: 물수지·전망 (순수 함수) ───────────────────────────────────────────
def _days(n=10):
    return [
        dict(date=dt.date(2026, 6, 1) + dt.timedelta(days=k), eto_mm=float(k + 1), rain_mm=0.0, filled=False)
        for k in range(n)
    ]


def test_water_balance_end_day_without_a_forecast_uses_the_previous_7_day_eto_mean():
    from croplib_dp.core.water_balance import Soil

    rows = observed_water_balance(
        _days(10), None, Soil(taw_mm=130, raw_mm=65, dr0_mm=0), end=dt.date(2026, 6, 11)
    )
    assert (
        rows[-1]["date"] == dt.date(2026, 6, 11)
        and rows[-1]["eto_src"] == "없음"
        and rows[-1]["filled"] is True
    )
    assert abs(rows[-1]["eto_mm"] - (4 + 5 + 6 + 7 + 8 + 9 + 10) / 7) < 1e-12
    with_fill = observed_water_balance(
        _days(10),
        None,
        Soil(taw_mm=130, raw_mm=65, dr0_mm=0),
        forecast_fill={dt.date(2026, 6, 11): (3.3, 1.0)},
        end=dt.date(2026, 6, 11),
    )
    assert with_fill[-1]["eto_src"] == "예보" and with_fill[-1]["eto_mm"] == 3.3  # 예보가 있으면 전과 같다


def test_lag_days_without_forecast_lists_only_the_days_the_morning_issue_cannot_cover():
    ft = pd.DataFrame(
        [dict(run=TS("2026-06-09 02:00"), run_name="아침", lead_day=0, target=TS("2026-06-09"), ETc_main=3.0)]
    )
    # 마지막 관측일 6/8, 오늘(회차 날짜) 6/10 → 지연일은 6/9. 6/9 아침 D+0 예보가 있다
    assert lag_days_without_forecast(ft, TS("2026-06-08"), TS("2026-06-10 17:00")) == []
    # 마지막 관측일 6/7 → 지연일 6/8·6/9, 6/8 예보는 없다
    assert lag_days_without_forecast(ft, TS("2026-06-07"), TS("2026-06-10 17:00")) == [TS("2026-06-08")]


# ── B: 필요한 앞선 발표만 재시도 ─────────────────────────────────────────
def _run02(fail, stations=("101",)):
    clock = FakeClock("2026-06-10 02:20")
    api = FlakyApi(
        clock, ISSUES, asos_last=dt.date(2026, 6, 8), fail=fail
    )  # 02시 회차: ASOS 어제(6/9)가 아직 없다
    r = W.run_slot("아침", ISSUE, _dicts(), KEY, http=api, clock=clock, stations=list(stations))
    return r, api, clock


def _asked(api, issue_key):
    return sum(1 for u in api.calls if f"base_date={issue_key[:8]}&base_time={issue_key[8:]}" in u)


def test_the_needed_24h_earlier_issue_is_retried_after_a_timeout_and_the_station_succeeds():
    """02시 회차는 어제의 02:00 발표(24시간 전)가 있어야 어제를 채운다. 한 번 시간 초과여도 30초 뒤 다시 받는다."""
    r, api, clock = _run02(fail={"202606090200": 1})
    run = next(x for x in r.forecast_runs if x["stn_id"] == "101")
    assert run["result"] != "실패" and not r.problems
    assert _asked(api, "202606090200") == 2  # 시간 초과 1 + 재시도 성공 1
    assert clock.t - pd.Timestamp("2026-06-10 02:20") >= pd.Timedelta(seconds=30)  # 30초 기다림


def test_retries_stop_after_two_more_tries_and_the_waits_are_30_and_90_seconds():
    assert W.NEEDED_ISSUE_RETRY_S == (30, 90)
    r, api, _ = _run02(fail={"202606090200": 9})  # 계속 시간 초과
    assert _asked(api, "202606090200") == 3  # 첫 시도 + 재시도 2번
    run = next(x for x in r.forecast_runs if x["stn_id"] == "101")
    # 그 발표를 끝내 못 받아도 A(지연일 대체) 덕분에 지점 전체가 실패하지 않고 '백업'으로 끝난다(전에는 '실패')
    assert run["result"] == "백업: 직전 발표" and run["error"] is None and not r.problems


def test_other_earlier_issues_are_not_retried():
    """필요하지 않은 앞선 발표(예: 6/9 11:00)는 전과 같이 한 번만 받는다 — 회차 시간·호출 수를 늘리지 않는다."""
    r, api, _ = _run02(fail={"202606091100": 5})
    assert _asked(api, "202606091100") == 1
    run = next(x for x in r.forecast_runs if x["stn_id"] == "101")
    assert run["result"] != "실패"
