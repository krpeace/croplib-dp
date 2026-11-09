"""관측 물 계산을 운영 지점 전부로 넓히기 (collectors/water.py, collect.yml). 이슈 #76(#28 S2), 사용자 확정 답(2026-11-08).

규칙
- 관측 대상 = `is_active` 운영 지점. **작물 공통 12토성만**, 대표 밖(has_forecast 아님) 사과·배추·전망·`forecast_runs` 는 없다(D1·D2).
- 기본은 꺼짐. Actions 변수 `OBS_ALL_STATIONS=true`일 때만 켜진다. 꺼져 있으면 지금과 똑같다.
- 관측만 하는 지점은 ASOS 실패가 회차 실패가 아니라 경고다. 재시도 2번(30초·90초), 단계 20분 상한.
- 평소 회차에는 새 지점 줄을 최근 14일만 올린다. 창 시작 전날 Dr 을 DB 줄에서 읽어 이어 계산하고, 없으면 그 지점은
  1/1부터 전체 재계산(올리는 건 창만)하고 경고한다. 창 안 값은 전체 재계산과 같다.
- 소급(`--soil-backfill`)은 20곳씩, 새 입력은 workflow_dispatch 에서만(schedule 은 지금과 같다).
- `weather_daily.rs_source` 칸이 DB에 없으면 그 칸만 빼고 올려 쓰기 전체가 실패하지 않는다.
입력은 모두 가짜 값이다.
"""

# ruff: noqa: E501

import datetime as dt
import json
import sys
import urllib.parse
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.io.dictionaries import column_exists  # noqa: E402
from croplib_dp.io.supabase_write import SupabaseWriter  # noqa: E402
from test_collector import ISSUE, ISSUES, KEY, FakeApi, FakeClock, _dicts  # noqa: E402
from test_upload import FakeSupabase  # noqa: E402

TS = pd.Timestamp
ROOT = Path(__file__).resolve().parent.parent
PREV = dt.date(2026, 6, 9)  # ISSUE(6/10 02시)의 어제
WIN_START = dt.date(2026, 5, 27)  # 어제까지 14일
PRIOR_DAY = dt.date(2026, 5, 26)  # 창 시작 전날


# ── 가짜 지점·토성 ────────────────────────────────────────────────────────
SOILS = [("loam", 0.25, 0.12), ("sand", 0.12, 0.045), ("clay", 0.36, 0.22)]


def _obs_dicts(extra=()):
    """101 춘천(대표, 예보 지점) + 216 태백·301~303(관측만) + 999(폐지). 216 에는 사과 계산 조건이 있다(대표 밖인데 있으면 무시돼야 함)."""
    d = _dicts()
    d.soils = {
        c: dict(soil_code=c, fc=fc, wp=wp, is_default=(c == "loam"), sort_order=i + 1)
        for i, (c, fc, wp) in enumerate(SOILS)
    }
    base = dict(d.stations["216"])
    d.stations["101"]["is_active"] = True
    d.stations["216"]["is_active"] = True
    for i, sid in enumerate(["301", "302", "303", *extra]):
        d.stations[sid] = dict(
            base, stn_id=sid, name_ko=f"관측{sid}", has_forecast=False, is_active=True, sort_order=3 + i
        )
    d.stations["999"] = dict(
        base, stn_id="999", name_ko="폐지", has_forecast=False, is_active=False, sort_order=99
    )
    return d


def _run(d=None, http=None, clock=None, **kw):
    clock = clock or FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    d = d or _obs_dicts()
    r = W.run_slot("아침", ISSUE, d, KEY, http=http(api) if http else api, clock=clock, **kw)
    return r, api, clock


def _asos(api, stn):
    return [u for u in api.calls if "startDt" in u and f"stnIds={stn}" in u]


# ── 어느 지점을 도나 ──────────────────────────────────────────────────────
def test_observation_stations_are_the_active_ones_in_order_without_the_retired():
    d = _obs_dicts()
    assert W.observation_stations(d) == ["101", "216", "301", "302", "303"]
    assert "999" not in W.observation_stations(d)
    assert W.observation_only_stations(d) == ["216", "301", "302", "303"]  # 예보 지점 제외


def test_switch_is_off_unless_the_variable_is_true():
    assert W.obs_all_enabled({"OBS_ALL_STATIONS": "true"})
    assert W.obs_all_enabled({"OBS_ALL_STATIONS": " TRUE "})
    for v in ("", "false", "1", "yes", "on", "tru"):
        assert not W.obs_all_enabled({"OBS_ALL_STATIONS": v}), v
    assert not W.obs_all_enabled({})


def test_switch_off_runs_exactly_the_forecast_stations_as_before():
    r, api, _ = _run()
    assert [s["stn_id"] for s in r.stations] == ["101"]
    assert not any("stnIds=301" in u for u in api.calls)
    assert {x["stn_id"] for x in r.soil_water_daily} == {"101"}
    assert r.warnings == []


def test_switch_on_runs_forecast_stations_first_then_the_observation_only_ones():
    r, api, _ = _run(obs_all=True)
    assert [s["stn_id"] for s in r.stations] == ["101", "216", "301", "302", "303"]
    assert [x["stn_id"] for x in r.forecast_runs] == [
        "101"
    ]  # 관측만 하는 지점은 forecast_runs 를 만들지 않는다
    assert all(len(_asos(api, s)) == 1 for s in ("101", "216", "301", "302", "303"))  # 지점마다 ASOS 한 번
    assert not any("stnIds=999" in u for u in api.calls)
    assert len([u for u in api.calls if "base_date" in u]) > 0 and not any(
        "base_date" in u and "nx=95" in u for u in api.calls
    )  # 예보는 늘리지 않는다 (216 의 격자 95,119 로는 묻지 않음)


def test_explicit_stations_run_in_the_observation_mode_only_when_the_switch_is_on():
    r, _, _ = _run(stations=["301"], obs_all=True)
    assert {x["soil_code"] for x in r.soil_water_daily} == {c for c, _, _ in SOILS}
    off, _, _ = _run(stations=["216"])  # 꺼짐: 예전 그대로(계산 조건이 있으면 물수지도 계산)
    assert any(x["scenario_id"].startswith("216-") for x in off.water_balance_daily)


# ── 관측만 하는 지점의 결과 ───────────────────────────────────────────────
def test_observation_only_stations_get_12_soils_style_rows_and_no_crop_rows():
    r, _, _ = _run(obs_all=True)
    obs = {"216", "301", "302", "303"}
    soil = [x for x in r.soil_water_daily if x["stn_id"] in obs]
    assert {x["stn_id"] for x in soil} == obs and {x["soil_code"] for x in soil} == {c for c, _, _ in SOILS}
    assert not [
        x for x in r.water_balance_daily if x["scenario_id"].split("-")[0] in obs
    ]  # 사과·배추 줄 0 (216 의 사과 조건도 무시)
    assert not [x for x in r.water_outlook if x["scenario_id"].split("-")[0] in obs]
    assert not [x for x in r.water_outlook_summary if x["scenario_id"].split("-")[0] in obs]
    assert {x["stn_id"] for x in r.forecast_runs} == {"101"}
    assert {w["stn_id"] for w in r.weather_daily} == {"101", "216", "301", "302", "303"}


def test_representative_station_rows_do_not_change_when_the_switch_is_on():
    off, _, _ = _run()
    on, _, _ = _run(obs_all=True)

    def rep(r, t):
        return [x for x in getattr(r, t) if str(x.get("stn_id", x.get("scenario_id", ""))).startswith("101")]

    for t in ("weather_daily", "soil_water_daily", "water_balance_daily", "forecast_runs"):
        assert rep(on, t) == rep(off, t), t


def test_normal_run_uploads_only_the_last_14_days_for_observation_only_stations():
    r, api, _ = _run(obs_all=True)
    soil = [x for x in r.soil_water_daily if x["stn_id"] == "301"]
    assert (
        min(x["date"] for x in soil) == WIN_START.isoformat()
        and max(x["date"] for x in soil) == PREV.isoformat()
    )
    assert len(soil) == 14 * len(SOILS)
    wx = [w for w in r.weather_daily if w["stn_id"] == "301"]
    assert [w["date"] for w in wx][0] == WIN_START.isoformat() and len(wx) == 14
    # 대표 지점은 지금 방식 그대로(관측은 전체, 토양은 14일)
    assert len([w for w in r.weather_daily if w["stn_id"] == "101"]) > 14
    # 계산은 1/1부터 받아서(월 단위 일사 규칙·ETo 채우기) 한 번에
    assert "startDt=20260101" in _asos(api, "301")[0] or "2026-01-01" in urllib.parse.unquote(
        _asos(api, "301")[0]
    )


def test_backfill_uploads_everything_from_jan_1_for_the_listed_stations_only():
    r, _, _ = _run(stations=["301", "302"], obs_all=True, soil_backfill=True)
    assert {x["stn_id"] for x in r.soil_water_daily} == {"301", "302"}
    soil = [x for x in r.soil_water_daily if x["stn_id"] == "301" and x["soil_code"] == "loam"]
    assert soil[0]["date"] == "2026-01-01" and soil[-1]["date"] == PREV.isoformat() and len(soil) == 160
    wx = [w for w in r.weather_daily if w["stn_id"] == "301"]
    assert wx[0]["date"] == "2026-01-01" and len(wx) == 160
    assert r.forecast_runs == [] and r.water_balance_daily == [] and not r.failed
    assert W.check_contract(r, W.taw_map(_obs_dicts())) == []


def _prior_from(rows, day=PRIOR_DAY):
    return {
        (x["stn_id"], x["soil_code"]): dict(dr_mm=x["dr_mm"], dp_mm=x["dp_mm"], filled=x["filled"])
        for x in rows
        if x["date"] == day.isoformat()
    }


def _key(x):
    return (x["stn_id"], x["soil_code"], x["date"])


def test_window_run_with_a_db_start_equals_the_full_recalculation_inside_the_window():
    full, _, _ = _run(stations=["301", "302"], obs_all=True, soil_backfill=True)
    prior = _prior_from(full.soil_water_daily)
    assert len(prior) == 2 * len(SOILS)
    win, _, _ = _run(stations=["301", "302"], obs_all=True, soil_prior=prior)
    want = {_key(x): x for x in full.soil_water_daily if x["date"] >= WIN_START.isoformat()}
    got = {_key(x): x for x in win.soil_water_daily}
    assert got == want and len(got) == 14 * 2 * len(SOILS)
    assert win.warnings == []  # 시작점이 있으면 경고 없음
    assert [w for w in win.weather_daily if w["stn_id"] == "301"] == [
        w for w in full.weather_daily if w["stn_id"] == "301" and w["date"] >= WIN_START.isoformat()
    ]


def test_missing_db_start_recalculates_from_jan_1_uploads_only_the_window_and_warns_once():
    full, _, _ = _run(stations=["301", "302"], obs_all=True, soil_backfill=True)
    win, _, _ = _run(stations=["301", "302"], obs_all=True, soil_prior={})
    want = {_key(x): x for x in full.soil_water_daily if x["date"] >= WIN_START.isoformat()}
    assert {_key(x): x for x in win.soil_water_daily} == want  # 전체 재계산 결과의 창 안 값과 같다
    msgs = [w for w in win.warnings if "전체 재계산" in w]
    assert len(msgs) == 1 and "301" in msgs[0] and "302" in msgs[0] and "소급" in msgs[0]
    assert not win.failed


def test_one_soil_without_a_start_makes_that_station_recalculate_fully():
    full, _, _ = _run(stations=["301", "302"], obs_all=True, soil_backfill=True)
    prior = _prior_from(full.soil_water_daily)
    del prior[("302", "sand")]
    win, _, _ = _run(stations=["301", "302"], obs_all=True, soil_prior=prior)
    want = {_key(x): x for x in full.soil_water_daily if x["date"] >= WIN_START.isoformat()}
    assert {_key(x): x for x in win.soil_water_daily} == want
    msgs = [w for w in win.warnings if "전체 재계산" in w]
    assert len(msgs) == 1 and "302" in msgs[0] and "301" not in msgs[0].split("—")[1]


def test_a_start_row_without_dr_counts_as_missing():
    full, _, _ = _run(stations=["301"], obs_all=True, soil_backfill=True)
    prior = _prior_from(full.soil_water_daily)
    prior[("301", "loam")]["dr_mm"] = None
    win, _, _ = _run(stations=["301"], obs_all=True, soil_prior=prior)
    assert any("전체 재계산" in w for w in win.warnings)


def test_no_start_is_needed_in_winter_and_no_warning_is_made():
    issue = TS("2026-01-20 02:00")
    clock = FakeClock("2026-01-20 02:20")
    api = FakeApi(
        clock, [issue + pd.Timedelta(hours=3 * k - 24) for k in range(9)], asos_last=dt.date(2026, 1, 19)
    )
    r = W.run_slot("아침", issue, _obs_dicts(), KEY, http=api, clock=clock, stations=["301"], obs_all=True)
    soil = [x for x in r.soil_water_daily]
    assert {x["status"] for x in soil} == {"winter"} and len(soil) == 14 * len(SOILS)
    assert not any("전체 재계산" in w for w in r.warnings)


# ── 실패·건너뜀 ───────────────────────────────────────────────────────────
def _down(stations):
    def wrap(api):
        def http(url, timeout):
            if "AsosDalyInfoService" in url and any(f"stnIds={s}" in url for s in stations):
                return 503, b""
            return api(url, timeout)

        return http

    return wrap


def test_observation_only_asos_failure_is_a_warning_not_a_failed_slot():
    r, api, clock = _run(obs_all=True, http=_down(["301"]))
    s = next(x for x in r.stations if x["stn_id"] == "301")
    assert not s["asos_ok"] and s["asos_status"] == "retry"
    assert not r.failed and r.problems == []
    msgs = [w for w in r.warnings if "ASOS 실패" in w]
    assert len(msgs) == 1 and "301" in msgs[0] and "회차 실패로 세지 않" in msgs[0]
    assert not [x for x in r.soil_water_daily if x["stn_id"] == "301"]  # 받은 값이 없으니 줄도 없다
    assert {x["stn_id"] for x in r.soil_water_daily} >= {"101", "216", "302", "303"}


def test_observation_only_asos_is_tried_three_times_with_30_and_90_second_waits():
    r, api, clock = _run(obs_all=True, stations=["301"], http=_down(["301"]))
    tries = [e for e in r.log.entries if e["kind"] == "asos" and e["target"].startswith("301")]
    assert [e["attempt"] for e in tries] == [1, 2, 3]
    assert clock.now() == TS("2026-06-10 02:22:00")  # 30초 + 90초 (대표 지점의 3분은 쓰지 않는다)


def test_representative_station_asos_failure_still_fails_the_slot_with_four_tries():
    r, api, clock = _run(obs_all=True, http=_down(["101"]))
    assert r.failed
    tries = [e for e in r.log.entries if e["kind"] == "asos" and e["target"].startswith("101")]
    assert [e["attempt"] for e in tries] == [1, 2, 3, 4]


def test_stage_has_a_time_cap_and_the_rest_wait_for_the_next_slot(monkeypatch):
    monkeypatch.setattr(W, "OBS_STAGE_MAX_S", 250)
    extra = ["401", "402", "403", "404", "405"]
    d = _obs_dicts(extra)
    r, api, clock = _run(d=d, obs_all=True, stations=extra, http=_down(extra))
    assert [s["stn_id"] for s in r.stations] == [
        "401",
        "402",
        "403",
    ]  # 120초씩: 0→120→240→(250 넘음)→3번째까지
    msgs = [w for w in r.warnings if "상한" in w]
    assert len(msgs) == 1 and "404" in msgs[0] and "405" in msgs[0] and "다음 회차" in msgs[0]
    assert not any("stnIds=404" in u for u in api.calls)
    assert not r.failed


def test_default_cap_is_20_minutes_and_retries_are_two():
    assert W.OBS_STAGE_MAX_S == 20 * 60 and W.OBS_ASOS_RETRY_S == (30, 90)


def test_station_without_calculation_inputs_is_skipped_with_a_warning_and_no_asos_call():
    d = _obs_dicts()
    d.stations["302"] = dict(d.stations["302"], elev_m=None)
    r, api, _ = _run(d=d, obs_all=True)
    assert not _asos(api, "302") and not [x for x in r.soil_water_daily if x["stn_id"] == "302"]
    msgs = [w for w in r.warnings if "계산 입력" in w]
    assert len(msgs) == 1 and "302" in msgs[0]
    assert not r.failed


def test_a_healthy_observation_stage_is_not_a_failure_and_has_no_warning():
    full, _, _ = _run(obs_all=True, soil_backfill=True, stations=["216", "301", "302", "303"])
    r, _, _ = _run(obs_all=True, soil_prior=_prior_from(full.soil_water_daily))
    assert not r.failed and r.problems == [] and r.warnings == []
    # 시작점을 못 읽었으면 실패가 아니라 경고 한 줄(소급 필요)
    r2, _, _ = _run(obs_all=True)
    assert not r2.failed and len(r2.warnings) == 1 and "전체 재계산" in r2.warnings[0]


# ── rs_source (C7) ───────────────────────────────────────────────────────
def test_weather_rows_carry_rs_source_with_allowed_values():
    r, _, _ = _run(obs_all=True)
    assert all("rs_source" in w for w in r.weather_daily)
    assert {w["rs_source"] for w in r.weather_daily} <= {"observed", "sunshine", "temp_range", None}
    assert W.check_contract(r, W.taw_map(_obs_dicts())) == []


def test_contract_rejects_an_unknown_rs_source_and_a_row_without_it():
    r, _, _ = _run()
    bad = dict(r.weather_daily[0], rs_source="guess")
    probs = W.check_rows("weather_daily", [bad])
    assert probs and "rs_source='guess'" in probs[0]
    lacking = {k: v for k, v in r.weather_daily[0].items() if k != "rs_source"}
    assert "빠진 열 ['rs_source']" in W.check_rows("weather_daily", [lacking])[0]


def _writer():
    fake = FakeSupabase()
    return SupabaseWriter("https://x.supabase.co", "srv", http=fake), fake


def test_publish_sends_rs_source_when_the_column_exists():
    r, _, _ = _run(obs_all=True)
    w, fake = _writer()
    W.publish(r, w, W.taw_map(_obs_dicts()), rs_source_column=True)
    assert r.uploaded and all("rs_source" in x for x in fake.tables["weather_daily"])
    assert not any("rs_source" in x for x in r.warnings)


def test_publish_drops_only_that_column_when_the_db_has_none_and_does_not_fail():
    r, _, _ = _run(obs_all=True)
    n = len(r.weather_daily)
    w, fake = _writer()
    W.publish(r, w, W.taw_map(_obs_dicts()), rs_source_column=False)
    assert r.uploaded and not r.problems
    rows = fake.tables["weather_daily"]
    assert len(rows) == n and not any("rs_source" in x for x in rows)  # 그 칸만 빠짐
    assert set(rows[0]) == set(r.weather_daily[0]) - {"rs_source"}
    assert all("rs_source" in x for x in r.weather_daily)  # 계산 결과·파일에는 남는다
    assert any("rs_source" in x and "빼고" in x for x in r.warnings)  # 조용히 넘어가지 않는다
    assert "rs_source" in W.summary_text(r)


def test_contract_failure_is_still_a_failure_when_the_column_is_dropped():
    r, _, _ = _run(obs_all=True)
    r.weather_daily[3]["eto_mm"] = 99.0
    w, fake = _writer()
    W.publish(r, w, W.taw_map(_obs_dicts()), rs_source_column=False)
    assert not r.uploaded and r.problems and "weather_daily" not in fake.tables


def _http(status, body=b""):
    return lambda url, headers: (status, body)


def test_column_exists_reads_the_answer():
    seen = []

    def ok(url, headers):
        seen.append(url)
        return 200, b"[]"

    assert column_exists("https://x.supabase.co", "srv", "weather_daily", "rs_source", ok)
    assert "select=rs_source" in seen[0] and "limit=1" in seen[0]
    missing = b'{"code":"42703","message":"column weather_daily.rs_source does not exist"}'
    assert not column_exists(
        "https://x.supabase.co", "srv", "weather_daily", "rs_source", _http(400, missing)
    )
    with pytest.raises(RuntimeError):
        column_exists("https://x.supabase.co", "srv", "weather_daily", "rs_source", _http(500, b"boom"))
    with pytest.raises(RuntimeError):
        column_exists("https://x.supabase.co", "srv", "weather_daily", "rs_source", _http(401, b"{}"))


def test_rs_source_state_falls_back_to_dropping_the_column_with_a_note_when_it_cannot_tell():
    ok, note = W.rs_source_state("https://x.supabase.co", "srv", _http(200, b"[]"))
    assert ok and note == ""
    missing = b'{"code":"42703","message":"column weather_daily.rs_source does not exist"}'
    ok, note = W.rs_source_state("https://x.supabase.co", "srv", _http(400, missing))
    assert not ok and "칸이 DB에 없어" in note
    ok, note = W.rs_source_state("https://x.supabase.co", "srv", _http(503, b"down"))
    assert not ok and "확인하지 못해" in note and "503" in note  # 쓰기 전체를 막지 않고 그 칸만 뺀다


# ── DB 시작점 읽기 ───────────────────────────────────────────────────────
def test_load_soil_prior_reads_the_day_before_the_window_in_pages():
    rows = [
        dict(stn_id=str(100 + i // 12), soil_code=f"s{i % 12}", dr_mm=1.5 + i, dp_mm=0.0, filled=False)
        for i in range(1044)
    ]
    urls = []

    def http(url, headers):
        urls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        lim, off = int(q["limit"]), int(q.get("offset", 0))
        return 200, json.dumps(rows[off : off + lim]).encode()

    got = W.load_soil_prior(
        "https://x.supabase.co", "srv", [str(100 + i) for i in range(87)], PRIOR_DAY, http
    )
    assert len(got) == 1044 and got[("100", "s0")] == dict(dr_mm=1.5, dp_mm=0.0, filled=False)
    assert len(urls) == 2  # 1,000줄 한도를 쪽으로 넘는다
    q = urllib.parse.unquote(urls[0])
    assert "date=eq.2026-05-26" in q and "stn_id=in.(100," in q and "order=" in q


# ── slot_done 은 예보 지점만 본다 (사용자 확정 2026-11-08: 관측만 하는 지점은 회차 재실행 없이 감시 경고로만) ──
def _slot_db(runs=None):
    runs = [{"stn_id": "101", "result": "정시 성공"}] if runs is None else runs

    def db(url, headers):
        table = urllib.parse.urlparse(url).path.split("/rest/v1/")[1]
        if table == "forecast_runs":
            return 200, json.dumps(runs).encode()
        raise AssertionError(f"slot_done 이 {table} 를 읽었다 — 예보 지점의 회차 기록만 봐야 한다")

    return db


def test_slot_done_ignores_the_observation_only_stations_completely():
    d = _obs_dicts()  # 216·301~303 은 관측만 하는 지점, 어제 줄이 하나도 없어도
    assert W.slot_done("https://x.supabase.co", "srv", d, ISSUE, _slot_db())


def test_slot_done_still_needs_the_forecast_stations_on_time():
    d = _obs_dicts()
    runs = [{"stn_id": "101", "result": "지연 수집"}]
    assert not W.slot_done("https://x.supabase.co", "srv", d, ISSUE, _slot_db(runs))


def test_slot_done_has_no_observation_option_any_more():
    import inspect

    assert "obs_all" not in inspect.signature(W.slot_done).parameters


def test_one_observation_only_station_that_keeps_failing_ends_the_slot_in_one_run_with_a_warning_only():
    """관측만 하는 지점 1곳이 계속 실패해도: 회차는 실패가 아니고, slot_done 은 끝났다고 보고(재실행 없음), 경고만 남는다."""
    r, _, _ = _run(obs_all=True, http=_down(["301"]))
    assert not r.failed and r.problems == []
    assert [x["stn_id"] for x in r.forecast_runs] == ["101"] and r.forecast_runs[0]["result"] == "정시 성공"
    assert any("ASOS 실패" in w and "301" in w for w in r.warnings)
    assert not [x for x in r.soil_water_daily if x["stn_id"] == "301"]  # 301 의 어제 줄은 없다
    done_rows = [{"stn_id": x["stn_id"], "result": x["result"]} for x in r.forecast_runs]
    assert W.slot_done(
        "https://x.supabase.co", "srv", _obs_dicts(), ISSUE, _slot_db(done_rows)
    )  # 재실행 없음
    # 그 지점은 감시의 "N일째" 경고로만 알린다
    from croplib_dp.core.monitor import check_health

    yesterday = PREV
    a = check_health(
        TS("2026-06-10 18:30"),
        [
            dict(
                run_at="2026-06-10 02:00",
                finished_at="2026-06-10 02:10",
                stn_id="101",
                result="정시 성공",
                on_time=True,
            )
        ],
        {"101": yesterday},
        {"301": "관측301"},
        obs_last={"301": dt.date(2026, 6, 7)},
    )
    assert [x.code for x in a] == ["obs"] and "301 관측301(2일째)" in a[0].text


# ── 워크플로 입력: workflow_dispatch 에서만 ────────────────────────────────
def test_schedule_run_is_the_same_as_before_even_if_inputs_were_somehow_set():
    for kw in (
        dict(stations="", backfill="", dry_run=""),
        dict(stations="301,302", backfill="true", dry_run="true"),
    ):
        o = W.dispatch_options("schedule", **kw)
        assert (o.stations, o.soil_backfill, o.write, o.skip_if_done) == (None, False, True, True)


def test_manual_run_without_new_inputs_is_the_same_as_before():
    o = W.dispatch_options("workflow_dispatch", stations="", backfill="false", dry_run="false")
    assert (o.stations, o.soil_backfill, o.write, o.skip_if_done) == (None, False, True, True)
    o = W.dispatch_options("workflow_dispatch", stations="", backfill="", dry_run="")
    assert (o.stations, o.soil_backfill, o.write, o.skip_if_done) == (None, False, True, True)


def test_manual_backfill_names_stations_does_not_skip_a_finished_slot_and_can_be_a_dry_run():
    o = W.dispatch_options("workflow_dispatch", stations=" 301, 302 ", backfill="true", dry_run="true")
    assert o.stations == ["301", "302"] and o.soil_backfill and not o.write and not o.skip_if_done
    o = W.dispatch_options("workflow_dispatch", stations="301", backfill="true", dry_run="false")
    assert o.write and o.soil_backfill and not o.skip_if_done
    o = W.dispatch_options("workflow_dispatch", stations="", backfill="false", dry_run="true")
    assert not o.write and not o.skip_if_done


def test_bad_station_input_stops_with_a_clear_message():
    for bad in ("301;rm", "30a", "301,,302x", "../"):
        with pytest.raises(SystemExit, match="지점 번호"):
            W.dispatch_options("workflow_dispatch", stations=bad, backfill="", dry_run="")


def test_backfill_must_name_at_most_20_observation_only_stations_when_the_switch_is_on():
    extra = [f"5{i:02d}" for i in range(30)]
    d = _obs_dicts(extra)
    with pytest.raises(SystemExit, match="20곳"):
        W.check_backfill_scope(d, None, True, True)  # 지점을 안 적으면 97곳 전부가 된다
    with pytest.raises(SystemExit, match="20곳"):
        W.check_backfill_scope(d, extra[:21], True, True)
    W.check_backfill_scope(d, extra[:20], True, True)  # 20곳은 된다
    W.check_backfill_scope(d, None, False, True)  # 소급이 아니면 검사 없음
    W.check_backfill_scope(d, None, True, False)  # 스위치가 꺼져 있으면(대표 10곳 소급) 검사 없음
    W.check_backfill_scope(d, ["101"], True, True)  # 대표 지점 소급은 그대로


def test_collect_workflow_passes_the_switch_and_the_new_inputs_through_the_python_decision():
    yml = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
    assert "OBS_ALL_STATIONS: ${{ vars.OBS_ALL_STATIONS }}" in yml
    for name in ("stations:", "soil_backfill:", "dry_run:"):
        assert name in yml.split("workflow_dispatch:")[1].split("concurrency:")[0], name
    # 두 번(첫 실행·다시 실행) 모두 같은 판단 함수를 거치고, 셸에 --write·--skip-if-done 을 박아 두지 않는다
    runs = [ln for ln in yml.splitlines() if "croplib_dp.collectors.water --slot" in ln]
    assert len(runs) == 2
    for ln in runs:
        assert (
            '--event "$EVENT"' in ln
            and "--input-stations" in ln
            and "--input-backfill" in ln
            and "--input-dry-run" in ln
        )
        assert "--write" not in ln and "--skip-if-done" not in ln
    # 예약(schedule) 줄은 그대로
    assert 'cron: "15,35,55 17 * * *"' in yml and 'cron: "15,35,55 8 * * *"' in yml
    assert yml.count("schedule:") == 1


def test_monitor_workflow_also_knows_the_switch():
    yml = (ROOT / ".github" / "workflows" / "monitor.yml").read_text(encoding="utf-8")
    assert "OBS_ALL_STATIONS: ${{ vars.OBS_ALL_STATIONS }}" in yml


def test_collect_workflow_monitor_step_gets_the_switch_too():
    """감시 단계(collect.yml 안)가 수집과 같은 스위치를 본다 = 스위치는 job 전체 환경변수(steps: 앞)에 있다."""
    yml = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
    job_env = yml.split("jobs:")[1].split("steps:")[0]
    assert "OBS_ALL_STATIONS: ${{ vars.OBS_ALL_STATIONS }}" in job_env
    step = yml.split("수집 감시 (문제가 있으면")[1].split("- name: 회차 결과")[0]
    assert (
        "croplib_dp.collectors.monitor" in step and "OBS_ALL_STATIONS" not in step
    )  # 단계마다 따로 적지 않는다


# ── 보고 ─────────────────────────────────────────────────────────────────
def test_summary_has_station_lines_row_counts_and_a_db_growth_estimate():
    r, _, _ = _run(stations=["301", "302"], obs_all=True, soil_backfill=True)
    text = W.summary_text(r)
    assert "- 301 관측301: ASOS" in text and "- 302 관측302: ASOS" in text
    n_soil, n_wx = len(r.soil_water_daily), len(r.weather_daily)
    mb = (n_soil * 253 + n_wx * 301) / 1048576
    assert f"예상 DB 증가(전부 새 줄일 때의 상한): {mb:.1f}MB" in text
    assert f"soil_water_daily {n_soil}줄" in text and f"weather_daily {n_wx}줄" in text


def test_brief_collapses_observation_only_stations_into_one_line():
    r, _, _ = _run(obs_all=True, http=_down(["302"]))
    lines = W.brief_text(r).splitlines()
    assert any(x.startswith("- 101 춘천") for x in lines)
    assert not any(
        x.startswith("- 301") or x.startswith("- 216") for x in lines
    )  # 지점마다 한 줄이면 97곳에서 잘린다
    one = [x for x in lines if x.startswith("- 관측 전용")]
    assert len(one) == 1 and "4곳" in one[0] and "실패 1" in one[0]


def test_brief_stays_short_with_97_stations():
    extra = [f"6{i:02d}" for i in range(90)]
    d = _obs_dicts(extra)
    r, _, _ = _run(d=d, obs_all=True)
    assert len(W.brief_text(r).encode()) < 2500


# ── main 연결 ────────────────────────────────────────────────────────────
def _main_env(monkeypatch, d, clock, api, switch="true"):
    monkeypatch.setenv("DATA_GO_KR", KEY)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "srv")
    if switch is None:
        monkeypatch.delenv("OBS_ALL_STATIONS", raising=False)
    else:
        monkeypatch.setenv("OBS_ALL_STATIONS", switch)
    monkeypatch.setattr(W, "load_dictionaries", lambda url, key: d)
    monkeypatch.setattr(W, "Clock", lambda: clock)
    real = W.run_slot
    monkeypatch.setattr(W, "run_slot", lambda *a, **k: real(*a, **{**k, "http": api}))

    calls = []
    monkeypatch.setattr(W, "publish", lambda r, w, taw, **k: calls.append(k))
    monkeypatch.setattr(W, "rs_source_state", lambda base, key, http=None: (True, ""))
    return calls


def test_main_dry_run_backfill_reports_rows_and_growth_and_never_writes(monkeypatch, tmp_path, capsys):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    calls = _main_env(monkeypatch, _obs_dicts(), clock, api)
    monkeypatch.setattr(
        W,
        "load_soil_prior",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("소급은 시작점을 읽지 않는다")),
    )
    code = W.main(
        ["--slot", "2026-06-10 02", "--out", str(tmp_path), "--event", "workflow_dispatch",
         "--input-stations", "301,302", "--input-backfill", "true", "--input-dry-run", "true"]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0 and "예상 DB 증가" in out and "301" in out and "DB 올리기: 안 함" in out
    assert calls == []  # 시험 실행: DB 쓰기(publish)를 부르지 않는다


def test_main_normal_run_reads_the_db_start_once_for_the_observation_only_stations(monkeypatch, tmp_path):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    calls = _main_env(monkeypatch, _obs_dicts(), clock, api)
    seen = []

    def prior(base, key, ids, day, *a, **k):
        seen.append((list(ids), day))
        return {}

    monkeypatch.setattr(W, "load_soil_prior", prior)
    monkeypatch.setattr(W, "slot_done", lambda *a, **k: False)
    monkeypatch.setattr(
        W, "pending_forecast_stations", lambda base, key, d, issue, http=None: W.forecast_stations(d)
    )
    W.main(["--slot", "2026-06-10 02", "--out", str(tmp_path), "--event", "schedule",
            "--input-stations", "", "--input-backfill", "", "--input-dry-run", ""])  # fmt: skip
    assert len(seen) == 1 and seen[0][0] == ["216", "301", "302", "303"] and seen[0][1] == PRIOR_DAY
    assert len(calls) == 1 and calls[0]["rs_source_column"] is True  # 예약 실행은 쓴다
    assert "전체 재계산" in (tmp_path / "20260610_0200" / "summary.md").read_text(encoding="utf-8")


def test_main_with_the_switch_off_does_not_read_the_start_and_runs_forecast_stations_only(
    monkeypatch, tmp_path
):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    _main_env(monkeypatch, _obs_dicts(), clock, api, switch=None)
    monkeypatch.setattr(
        W, "load_soil_prior", lambda *a, **k: (_ for _ in ()).throw(AssertionError("꺼짐인데 읽었다"))
    )
    monkeypatch.setattr(W, "slot_done", lambda *a, **k: False)
    monkeypatch.setattr(
        W, "pending_forecast_stations", lambda base, key, d, issue, http=None: W.forecast_stations(d)
    )
    W.main(["--slot", "2026-06-10 02", "--out", str(tmp_path), "--event", "schedule",
            "--input-stations", "", "--input-backfill", "", "--input-dry-run", ""])  # fmt: skip
    assert not any("stnIds=301" in u for u in api.calls)


def test_main_backfill_without_naming_stations_stops_when_the_switch_is_on(monkeypatch, tmp_path):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    _main_env(monkeypatch, _obs_dicts([f"7{i:02d}" for i in range(30)]), clock, api)
    with pytest.raises(SystemExit, match="20곳"):
        W.main(["--slot", "2026-06-10 02", "--out", str(tmp_path), "--event", "workflow_dispatch",
                "--input-stations", "", "--input-backfill", "true", "--input-dry-run", "true"])  # fmt: skip
