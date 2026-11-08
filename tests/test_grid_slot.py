"""예보 격자 점검이 수집·감시에 연결된 모습 (collectors/water.py, collectors/monitor.py, core/monitor.py). 이슈 #72(#28 S1).

규칙(2026-11-08 확정): **DB에 격자가 있으면 DB 값이 기준**(계산값과 달라도 예보 계속, 경고만). **격자가 비어 있는 has_forecast
지점만 예보를 건너뛴다**(관측은 계속, slot_done 은 그 지점을 기다리지 않아 같은 회차를 반복하지 않는다). 입력은 모두 가짜 값이다.
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

from croplib_dp.collectors import monitor as M  # noqa: E402
from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.core.monitor import check_health  # noqa: E402
from test_collector import ISSUE, ISSUES, KEY, FakeApi, FakeClock, _dicts  # noqa: E402

TS = pd.Timestamp


def _run(d, stations=None):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    r = W.run_slot("아침", ISSUE, d, KEY, http=api, clock=clock, stations=stations)
    return r, api


def _forecast_calls(api):
    return [u for u in api.calls if "base_date" in u]


def _asos_calls(api):
    return [u for u in api.calls if "startDt" in u]


# ── run_slot: 격자 비어 있는 지점만 예보를 건너뛴다 ───────────────────────────
def test_station_with_an_empty_grid_skips_the_forecast_but_keeps_the_observation():
    d = _dicts()
    d.stations["101"] = dict(d.stations["101"], grid_nx=None, grid_ny=None)
    r, api = _run(d)
    assert _forecast_calls(api) == []  # 예보 호출 0
    assert _asos_calls(api) and r.weather_daily  # 관측은 계속
    assert r.forecast_runs == []  # forecast_runs 에 새 result 값을 만들지 않는다(계약 변경 없음)
    assert (
        len(r.warnings) == 1
        and "101" in r.warnings[0]
        and "격자" in r.warnings[0]
        and "건너뜀" in r.warnings[0]
    )
    assert not r.failed and r.problems == []  # 실패로 세지 않는다


def test_warning_is_printed_in_the_summary_and_the_brief():
    d = _dicts()
    d.stations["101"] = dict(d.stations["101"], grid_nx=None, grid_ny=None)
    r, _ = _run(d)
    assert "## 경고" in W.summary_text(r) and "격자" in W.summary_text(r)
    assert "격자" in W.brief_text(r)


def test_db_grid_that_differs_from_the_calculation_only_warns_and_the_forecast_goes_on_with_the_db_grid():
    d = _dicts()
    d.stations["101"] = dict(d.stations["101"], grid_nx=74)  # 계산값은 73,134
    r, api = _run(d)
    calls = _forecast_calls(api)
    assert calls and all("nx=74&ny=134" in u for u in calls)  # DB 값으로 계속
    assert len(r.forecast_runs) == 1 and r.forecast_runs[0]["result"] == "정시 성공"
    assert (
        len(r.warnings) == 1
        and "74,134" in r.warnings[0]
        and "73,134" in r.warnings[0]
        and "다름" in r.warnings[0]
    )
    assert not r.failed


def test_normal_station_has_no_warning_and_runs_as_before():
    r, api = _run(_dicts())
    assert r.warnings == [] and _forecast_calls(api) and len(r.forecast_runs) == 1


def test_missing_coordinates_with_a_db_grid_is_not_a_warning():
    d = _dicts()
    d.stations["101"] = dict(d.stations["101"], lat=None, lon=None)
    # 좌표가 없으면 예보 계산(service_table)이 쓸 수 없으니 여기서는 점검 결과만 본다
    from croplib_dp.core import grid as G

    assert G.check_station_grid(d.stations["101"]).status == "unchecked"


# ── slot_done: 격자 비어 있는 지점 때문에 같은 회차를 반복하지 않는다 (#28 문제 ④) ──────
def _db(rows):
    def db(url, headers):
        return 200, json.dumps(rows).encode()

    return db


def _d_with_empty_grid_station():
    d = _dicts()
    d.stations["216"] = dict(d.stations["216"], has_forecast=True)  # 격자 있음
    d.stations["999"] = dict(
        stn_id="999",
        name_ko="새곳",
        lat=36.0,
        lon=128.0,
        grid_nx=None,
        grid_ny=None,
        has_forecast=True,
        sort_order=3,
    )
    return d


def test_slot_done_does_not_wait_for_a_station_with_an_empty_grid():
    d = _d_with_empty_grid_station()
    rows = [{"stn_id": "101", "result": "정시 성공"}, {"stn_id": "216", "result": "정시 성공"}]
    assert W.slot_done(
        "https://x.supabase.co", "srv", d, ISSUE, _db(rows)
    )  # 999 는 예보를 안 하므로 기다리지 않음


def test_slot_done_still_needs_every_ready_station_on_time():
    d = _d_with_empty_grid_station()
    rows = [{"stn_id": "101", "result": "정시 성공"}, {"stn_id": "216", "result": "지연 수집"}]
    assert not W.slot_done("https://x.supabase.co", "srv", d, ISSUE, _db(rows))


def test_slot_done_with_no_ready_station_is_not_done():
    d = _dicts()
    d.stations["101"] = dict(d.stations["101"], grid_nx=None, grid_ny=None)
    assert not W.slot_done("https://x.supabase.co", "srv", d, ISSUE, _db([]))


def test_slot_done_waits_for_a_station_whose_db_grid_differs_from_the_calculation():
    d = _dicts()
    d.stations["101"] = dict(d.stations["101"], grid_nx=74)
    assert not W.slot_done("https://x.supabase.co", "srv", d, ISSUE, _db([]))  # 예보를 하는 지점이라 기다린다
    assert W.slot_done(
        "https://x.supabase.co", "srv", d, ISSUE, _db([{"stn_id": "101", "result": "정시 성공"}])
    )


# ── 감시: grid 경고 ───────────────────────────────────────────────────────
NOW = TS("2026-10-26 10:30")
RUNS = [
    dict(
        run_at="2026-10-26 08:00",
        finished_at="2026-10-26 08:10",
        stn_id="101",
        result="정시 성공",
        on_time=True,
    )
]
NAMES = {"101": "춘천", "102": "새곳", "103": "다른곳"}
WEATHER = {"101": dt.date(2026, 10, 25)}


def test_monitor_warns_about_empty_and_differing_grids_with_separate_keys():
    a = check_health(NOW, RUNS, WEATHER, NAMES, grid_status={"101": "ok", "102": "missing", "103": "differs"})
    got = {x.key.split(":")[2]: x for x in a if x.code == "grid"}
    assert set(got) == {"예보 격자 비어 있음", "예보 격자가 계산값과 다름"}
    assert all(x.level == "warn" for x in got.values())
    assert "102 새곳" in got["예보 격자 비어 있음"].text and "건너" in got["예보 격자 비어 있음"].text
    assert (
        "103 다른곳" in got["예보 격자가 계산값과 다름"].text
        and "예보는 계속" in got["예보 격자가 계산값과 다름"].text
    )
    assert "101" not in got["예보 격자 비어 있음"].text.split("—")[1]


def test_monitor_is_quiet_when_grids_are_ok_unchecked_or_not_given():
    assert check_health(NOW, RUNS, WEATHER, NAMES, grid_status={"101": "ok", "102": "unchecked"}) == []
    assert check_health(NOW, RUNS, WEATHER, NAMES, grid_status={}) == []
    assert check_health(NOW, RUNS, WEATHER, NAMES) == []


def test_grid_alert_key_ignores_which_stations_so_comments_do_not_pile_up():
    a = check_health(NOW, RUNS, WEATHER, NAMES, grid_status={"102": "missing"})
    b = check_health(NOW, RUNS, WEATHER, NAMES, grid_status={"102": "missing", "103": "missing"})
    assert [x.key for x in a] == [x.key for x in b]


class _Db:
    """감시가 읽는 표만 흉내. stations 줄에 격자 칸이 들어 있다."""

    def __init__(self, stations, with_grid_columns=True):
        self.urls: list[str] = []
        self.stations, self.with_grid_columns = stations, with_grid_columns

    def __call__(self, url, headers):
        self.urls.append(url)
        u = urllib.parse.urlparse(url)
        table, q = u.path.split("/rest/v1/")[1], dict(urllib.parse.parse_qsl(u.query))
        if table == "forecast_runs":
            return 200, json.dumps(RUNS).encode()
        if table == "stations":
            if "has_forecast" in q or any("has_forecast" in k for k in q):
                rows = [s for s in self.stations if s.get("has_forecast")]
            else:
                rows = self.stations
            if not self.with_grid_columns:
                rows = [{"stn_id": s["stn_id"], "name_ko": s["name_ko"]} for s in rows]
            return 200, json.dumps(rows).encode()
        if table == "heat_daily":
            return 200, json.dumps([]).encode()
        return 200, json.dumps([{"stn_id": "101", "date": "2026-10-25"}]).encode()


STATIONS = [
    dict(
        stn_id="101",
        name_ko="춘천",
        lat=37.90262,
        lon=127.7357,
        grid_nx=73,
        grid_ny=134,
        has_forecast=True,
        is_active=True,
    ),
    dict(
        stn_id="102",
        name_ko="새곳",
        lat=36.0,
        lon=128.0,
        grid_nx=None,
        grid_ny=None,
        has_forecast=True,
        is_active=True,
    ),
    dict(
        stn_id="103",
        name_ko="다른곳",
        lat=37.25746,
        lon=126.983,
        grid_nx=61,
        grid_ny=120,
        has_forecast=True,
        is_active=True,
    ),
]


def test_read_state_computes_grid_status_for_has_forecast_stations_without_extra_calls():
    db = _Db(STATIONS)
    state = M.read_state("https://x.supabase.co", "srv", db, now=NOW)
    assert state["grid_status"] == {"101": "ok", "102": "missing", "103": "differs"}
    st_urls = [u for u in db.urls if "/rest/v1/stations" in u]
    assert len(st_urls) == 2  # 호출 수 그대로(지점 2번)
    collected = next(u for u in st_urls if "has_forecast=eq.true" in u)
    for col in ("lat", "lon", "grid_nx", "grid_ny"):
        assert col in urllib.parse.unquote(collected)


def test_read_state_skips_the_check_when_the_columns_are_not_in_the_answer():
    state = M.read_state("https://x.supabase.co", "srv", _Db(STATIONS, with_grid_columns=False), now=NOW)
    assert state["grid_status"] == {}


def test_run_monitor_opens_one_issue_with_the_grid_warnings():
    state = M.read_state("https://x.supabase.co", "srv", _Db(STATIONS), now=NOW)
    text, action = M.run_monitor(state, None, now=NOW)
    assert (
        "예보 격자 비어 있음" in text
        and "102 새곳" in text
        and "예보 격자가 계산값과 다름" in text
        and "103 다른곳" in text
    )
    assert "srv" not in text and action == "dry"
