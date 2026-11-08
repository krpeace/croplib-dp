"""수집 감시의 관측 물 상태 점검 (core/monitor.py, collectors/monitor.py). 이슈 #76(#28 S2, 결정 D2).

관측만 하는 지점은 `forecast_runs` 줄이 없으므로 `soil_water_daily` 최신 날짜로 본다. 지점마다 묻지 않고 **한 번 조회**하고,
기존 관측(`weather_daily`) 조회도 지점마다 → **한 번**으로 줄인다(호출 수 감소). 스위치(`OBS_ALL_STATIONS`)가 꺼져 있으면
새 점검은 하지 않는다. 입력은 모두 가짜 값이다.
"""

# ruff: noqa: E501

import datetime as dt
import json
import urllib.parse

import pandas as pd

from croplib_dp.collectors import monitor as M
from croplib_dp.core.monitor import check_health

TS = pd.Timestamp
D = dt.date
NOW = TS("2026-10-26 18:30")  # 어제 = 10/25
YESTERDAY = "2026-10-25"
NAMES = {"101": "춘천", "216": "태백", "301": "가", "302": "나", "303": "다"}
RUNS = [
    dict(
        run_at="2026-10-26 08:00",
        finished_at="2026-10-26 08:10",
        stn_id="101",
        result="정시 성공",
        on_time=True,
    )
]


# ── 규칙 ────────────────────────────────────────────────────────────────
def test_obs_alert_lists_stations_without_yesterday_with_how_many_days():
    obs = {"301": D(2026, 10, 25), "302": D(2026, 10, 23), "303": None}
    a = [x for x in check_health(NOW, RUNS, {"101": D(2026, 10, 25)}, NAMES, obs_last=obs) if x.code == "obs"]
    assert len(a) == 1 and a[0].level == "warn"
    t = a[0].text
    assert t.startswith("관측 물 상태 어제(10/25) 값 없음 — ")
    assert "302 나(2일째)" in t and "303 다(5일 넘게)" in t and "301" not in t.split("—")[1]


def test_obs_alert_is_quiet_before_18_when_all_present_or_not_given():
    obs = {"301": None}
    assert not [
        x for x in check_health(TS("2026-10-26 12:00"), RUNS, {}, NAMES, obs_last=obs) if x.code == "obs"
    ]
    assert not [
        x for x in check_health(NOW, RUNS, {}, NAMES, obs_last={"301": D(2026, 10, 25)}) if x.code == "obs"
    ]
    assert not [x for x in check_health(NOW, RUNS, {}, NAMES, obs_last={}) if x.code == "obs"]
    assert not [x for x in check_health(NOW, RUNS, {}, NAMES) if x.code == "obs"]


def test_obs_alert_key_ignores_which_stations_so_comments_do_not_pile_up():
    a = check_health(NOW, RUNS, {}, NAMES, obs_last={"301": None})
    b = check_health(NOW, RUNS, {}, NAMES, obs_last={"301": None, "302": D(2026, 10, 20)})
    ka = [x.key for x in a if x.code == "obs"]
    assert ka and ka == [x.key for x in b if x.code == "obs"]


def test_obs_alert_with_many_stations_is_shortened_like_the_other_alerts():
    names = {str(300 + i): f"곳{i}" for i in range(40)}
    obs = {s: None for s in names}
    t = next(x.text for x in check_health(NOW, RUNS, {}, names, obs_last=obs) if x.code == "obs")
    assert "40곳 — 앞 10곳" in t and t.endswith("…")
    key = next(x.key for x in check_health(NOW, RUNS, {}, names, obs_last=obs) if x.code == "obs")
    assert key == "warn:obs:관측 물 상태 어제(10/25) 값 없음"


def test_obs_alert_does_not_replace_the_asos_alert_for_the_representative_stations():
    a = check_health(NOW, RUNS, {"101": D(2026, 10, 20)}, NAMES, obs_last={"301": None})
    assert {x.code for x in a} == {"asos", "obs"}


# ── 읽기 ────────────────────────────────────────────────────────────────
STATIONS = [
    {"stn_id": "101", "name_ko": "춘천", "has_forecast": True, "is_active": True},
    {"stn_id": "216", "name_ko": "태백", "has_forecast": True, "is_active": True},
    {"stn_id": "301", "name_ko": "가", "has_forecast": False, "is_active": True},
    {"stn_id": "302", "name_ko": "나", "has_forecast": False, "is_active": True},
    {"stn_id": "303", "name_ko": "다", "has_forecast": False, "is_active": True},
    {"stn_id": "999", "name_ko": "폐지", "has_forecast": False, "is_active": False},
]


class Db:
    """가짜 Supabase REST: 요청 주소를 기록한다. 관측·토양 표는 지점별 마지막 날(없으면 줄 없음)을 주고 조건대로 거른다."""

    def __init__(self, weather=None, soil=None):
        self.weather = weather if weather is not None else {"101": YESTERDAY, "216": YESTERDAY}
        self.soil = soil if soil is not None else {"301": YESTERDAY, "302": YESTERDAY, "303": YESTERDAY}
        self.urls: list[str] = []

    def __call__(self, url, headers):
        self.urls.append(url)
        u = urllib.parse.urlparse(url)
        table, q = u.path.split("/rest/v1/")[1], dict(urllib.parse.parse_qsl(u.query))
        if table == "forecast_runs":
            return 200, json.dumps(RUNS).encode()
        if table == "stations":
            rows = STATIONS
            if q.get("has_forecast") == "eq.true":
                rows = [s for s in rows if s["has_forecast"]]
            if q.get("is_active") == "eq.true":
                rows = [s for s in rows if s["is_active"]]
            return 200, json.dumps([{"stn_id": s["stn_id"], "name_ko": s["name_ko"]} for s in rows]).encode()
        if table == "heat_daily":
            return 200, json.dumps([{"stn_id": s["stn_id"]} for s in STATIONS if s["is_active"]]).encode()
        if table == "soil_types":
            return 200, json.dumps(
                [
                    {"soil_code": "sand", "is_default": False, "sort_order": 2},
                    {"soil_code": "loam", "is_default": True, "sort_order": 1},
                ]
            ).encode()
        if table in ("weather_daily", "soil_water_daily"):
            src = self.weather if table == "weather_daily" else self.soil
            gte = q["date"].removeprefix("gte.")
            ids = (
                q["stn_id"].removeprefix("in.(").removesuffix(")").split(",")
                if q.get("stn_id", "").startswith("in.(")
                else list(src)
            )
            if table == "soil_water_daily":
                assert q["soil_code"] == "eq.loam"
            rows = [{"stn_id": s, "date": src[s]} for s in ids if s in src and src[s] >= gte]
            return 200, json.dumps(rows).encode()
        raise AssertionError(table)


def _state(db, **kw):
    return M.read_state("https://x.supabase.co", "srv", db, now=NOW, **kw)


def _of(db, table):
    return [u for u in db.urls if f"/rest/v1/{table}?" in u]


def test_weather_is_read_in_one_call_for_the_collected_stations_only():
    db = Db()
    s = _state(db)
    w = _of(db, "weather_daily")
    assert len(w) == 1  # 지점 수와 상관없이 한 번 (전에는 지점마다)
    q = urllib.parse.unquote(w[0])
    assert "stn_id=in.(101,216)" in q and f"date=gte.{YESTERDAY}" in q
    assert s["weather_last"] == {"101": D(2026, 10, 25), "216": D(2026, 10, 25)}


def test_weather_last_is_none_for_a_collected_station_without_a_row():
    s = _state(Db(weather={"101": YESTERDAY}))
    assert s["weather_last"] == {"101": D(2026, 10, 25), "216": None}
    alerts = check_health(NOW, s["runs"], s["weather_last"], s["names"])
    assert [(a.level, a.code) for a in alerts] == [("warn", "asos")] and "216 태백" in alerts[0].text


def test_switch_off_makes_no_observation_check_and_no_extra_calls():
    db = Db()
    s = _state(db)
    assert s.get("obs_last") is None
    assert not _of(db, "soil_water_daily") and not _of(db, "soil_types")
    assert len(db.urls) == 1 + 2 + 1 + 1  # 회차, 지점 2번, 관측 한 번, 적산온도 한 번


def test_switch_on_reads_the_soil_table_once_for_the_observation_only_stations():
    db = Db(soil={"301": YESTERDAY, "302": "2026-10-23"})
    s = _state(db, obs_all=True)
    assert len(_of(db, "soil_types")) == 1 and len(_of(db, "soil_water_daily")) == 1
    q = urllib.parse.unquote(_of(db, "soil_water_daily")[0])
    assert (
        "soil_code=eq.loam" in q and "date=gte.2026-10-21" in q and "select=stn_id,date" in q
    )  # 기본 토성 하나, 5일치
    assert "stn_id=eq" not in q
    # 관측만 하는 지점 = 운영 중이면서 예보 지점이 아닌 곳 (대표 101·216 과 폐지 999 는 제외)
    assert s["obs_last"] == {"301": D(2026, 10, 25), "302": D(2026, 10, 23), "303": None}
    assert len(db.urls) == 1 + 2 + 1 + 1 + 1 + 1


def test_switch_on_keeps_the_latest_date_inside_the_window_and_none_when_absent():
    db = Db(soil={"301": YESTERDAY, "302": "2026-10-22"})
    s = _state(db, obs_all=True)
    assert (
        s["obs_last"]["301"] == D(2026, 10, 25)
        and s["obs_last"]["302"] == D(2026, 10, 22)
        and s["obs_last"]["303"] is None
    )


def test_run_monitor_reports_the_observation_alert_and_stays_quiet_when_all_present():
    s = _state(Db(soil={"301": YESTERDAY, "302": "2026-10-22"}), obs_all=True)
    text, action = M.run_monitor(s, None, now=NOW)
    assert (
        "관측 물 상태 어제(10/25) 값 없음" in text
        and "302 나(3일째)" in text
        and "303 다(5일 넘게)" in text
        and action == "dry"
    )
    ok = _state(Db(soil={"301": YESTERDAY, "302": YESTERDAY, "303": YESTERDAY}), obs_all=True)
    text, _ = M.run_monitor(ok, None, now=NOW)
    assert "이상 없음" in text


def test_no_observation_only_stations_means_no_alert_and_no_soil_call():
    class OnlyRep(Db):
        def __call__(self, url, headers):
            if "/rest/v1/stations" in url and "is_active=eq.true" in url:
                self.urls.append(url)
                return 200, json.dumps(
                    [{"stn_id": "101", "name_ko": "춘천"}, {"stn_id": "216", "name_ko": "태백"}]
                ).encode()
            return super().__call__(url, headers)

    db = OnlyRep()
    s = _state(db, obs_all=True)
    assert s["obs_last"] == {} and not _of(db, "soil_water_daily")


def test_monitor_workflow_python_reads_the_switch_from_the_environment(monkeypatch):
    from croplib_dp.collectors import water as W

    assert W.obs_all_enabled({"OBS_ALL_STATIONS": "true"}) and not W.obs_all_enabled({})
    seen = {}
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "srv")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("OBS_ALL_STATIONS", "true")

    def fake_state(base, key, http=None, now=None, include_krei=False, obs_all=False):
        seen["obs_all"] = obs_all
        return dict(
            runs=RUNS, names=NAMES, weather_last={"101": D(2026, 10, 25)}, heat_last={"101": D(2026, 10, 25)}
        )

    monkeypatch.setattr(M, "read_state", fake_state)
    monkeypatch.setattr(M, "db_size_state", lambda *a, **k: {})
    monkeypatch.setattr(M, "run_monitor", lambda state, sink, now=None: ("ok", "dry"))
    assert M.main([]) == 0 and seen["obs_all"] is True
