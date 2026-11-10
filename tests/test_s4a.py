"""S4-a: 예보 지점을 20곳씩 켜기 전 준비 (#88, #85 결정 ①~⑤, 결정 0029). 입력은 모두 가짜 값이다.

- 대표 판별은 `stations.is_representative`(결정 ②). 대표 밖 예보 지점은 앞선 발표 8 → 2개(④), 원문은
  `fcst-ext/`에 서비스 발표만.
- 계산 조건이 없는 예보 지점도 '정시 성공'이 될 수 있다(안 그러면 `slot_done`이 영영 안 끝난다).
- 재실행은 '정시 성공'이 아닌 예보 지점만(③). 감시는 지점별 최신 회차 기록을 읽는다(P8).
- retention 상한 20,000(⑤). 한 회차 단계별 시간·호출 수, DB 크기 한 줄, S6 번호 연결.
"""

# ruff: noqa: E501

import sys
import urllib.parse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import monitor as M  # noqa: E402
from croplib_dp.collectors import retention as RT  # noqa: E402
from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.core.monitor import check_health, db_size_line  # noqa: E402
from test_collector import ISSUE, ISSUES, KEY, FakeApi, FakeClock  # noqa: E402
from test_monitor import NOW, YESTERDAY, FakeDb  # noqa: E402
from test_obs_all import _main_env, _obs_dicts, _slot_db  # noqa: E402

URL = "https://x.supabase.co"


def _dicts_with_nonrep():
    """101 춘천(대표) + 301(대표 밖 예보 지점: 격자·has_forecast 있음, 계산 조건 없음) + 관측만 하는 216·302·303."""
    d = _obs_dicts()
    d.stations["101"]["is_representative"] = True
    d.stations["301"].update(has_forecast=True, is_representative=False, grid_nx=73, grid_ny=135)
    return d


def _run(stations=("101", "301")):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    r = W.run_slot("아침", ISSUE, _dicts_with_nonrep(), KEY, http=api, clock=clock, stations=list(stations))
    return r, api


def _issues_asked(api, nx, ny):
    out = set()
    for u in api.calls:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(u).query))
        if "base_date" in q and q["nx"] == str(nx) and q["ny"] == str(ny):
            out.add(q["base_date"] + q["base_time"])
    return out


# ── 대표 판별 ────────────────────────────────────────────────────────────
def test_is_representative_reads_the_column_and_defaults_to_representative_when_absent():
    assert W.is_representative({"is_representative": True}) is True
    assert W.is_representative({"is_representative": False}) is False
    assert (
        W.is_representative({}) is True
    )  # 칸이 없으면 대표로 본다(대표의 원문·앞선 발표를 줄이지 않는 안전한 쪽)


# ── 앞선 발표 8 → 2 ──────────────────────────────────────────────────────
def test_representative_still_asks_for_eight_earlier_issues_but_a_non_representative_only_two():
    """S5-a(결정 0030)에서 대표 밖의 앞선 발표 2개를 "가장 최근 2개(3·6시간 전)"에서 "필요한 2개"로 바꿨다:
    02시 회차는 3시간 전(23시) + 24시간 전(어제 아침 발표). 개수는 그대로 2개."""
    r, api = _run()
    assert len(_issues_asked(api, 73, 134)) == 1 + 8  # 대표 101: 서비스 발표 1 + 앞선 8
    asked = _issues_asked(api, 73, 135)
    assert asked == {
        "202606100200",
        "202606092300",
        "202606090200",
    }  # 대표 밖 301: 서비스 발표 + 필요한 앞선 2개(3시간 전·24시간 전)
    assert [x["stn_id"] for x in r.forecast_runs] == ["101", "301"]


# ── 원문은 fcst-ext/ 에 서비스 발표만 ────────────────────────────────────
def test_non_representative_raw_goes_to_fcst_ext_with_the_service_issue_only():
    r, _ = _run()
    ext = [p for p in r.raw_files if "/73_135/" in p]
    assert ext == ["fcst-ext/73_135/2026/06/20260610_0200.json.gz"]
    rep = [p for p in r.raw_files if "/73_134/" in p]
    assert len(rep) == 9 and all(p.startswith("fcst/73_134/") for p in rep)  # 대표는 그대로 fcst/ 9개
    runs = {x["stn_id"]: x for x in r.forecast_runs}
    assert runs["301"]["raw_path"] == "fcst-ext/73_135/2026/06/20260610_0200.json.gz"
    assert runs["101"]["raw_path"].startswith("fcst/73_134/")


def test_a_backup_issue_is_never_pointed_at_for_a_non_representative_raw_path():
    """대표 밖은 서비스 발표 원문만 올리므로, 앞선 발표를 대신 썼을 때 raw_path 는 없는 파일을 가리키면 안 된다."""
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    col = W.ForecastCollector(KEY, 73, 135, api, clock, W.RunLog(), representative=False)
    col.collect_service(ISSUE, wait=False)
    assert W._raw_path(col, ISSUE) == "fcst-ext/73_135/2026/06/20260610_0200.json.gz"
    assert W._raw_path(col, pd.Timestamp("2026-06-09 23:00")) is None


# ── 계산 조건이 없는 예보 지점 ───────────────────────────────────────────
def test_a_forecast_station_without_calc_conditions_can_be_on_time_success():
    r, _ = _run()
    run = next(x for x in r.forecast_runs if x["stn_id"] == "301")
    assert run["result"] == "정시 성공" and run["on_time"] is True and run["rs_method"] is None
    assert not [x for x in r.water_outlook if x["stn_id"] == "301"]  # 전망은 만들지 않는다(S5)
    assert not r.problems


def test_slot_result_keeps_the_old_rule_when_there_are_calc_conditions():
    col = dict(late=False, required_ok=True, complete8=True)
    base = dict(ok=True, backup=None, rs_base="S3")
    assert W.slot_result(col, dict(base, rs_method="S3")) == "정시 성공"
    assert W.slot_result(col, dict(base, rs_method="S4")) == "백업: S3"  # 계산 조건이 있으면 일사 방식도 본다
    assert W.slot_result(col, dict(base, rs_method=None, calc=False)) == "정시 성공"
    assert W.slot_result(col, dict(base, rs_method=None, calc=True)) == "백업: S3"


# ── 실패한 지점만 다시 돌리기 ────────────────────────────────────────────
def test_pending_forecast_stations_are_those_without_on_time_success():
    d = _dicts_with_nonrep()
    runs = [{"stn_id": "101", "result": "정시 성공"}, {"stn_id": "301", "result": "실패"}]
    db = _slot_db(runs)
    assert W.pending_forecast_stations(URL, "srv", d, ISSUE, db) == ["301"]
    assert not W.slot_done(URL, "srv", d, ISSUE, db)
    done = [{"stn_id": s, "result": "정시 성공"} for s in ("101", "301")]
    assert W.pending_forecast_stations(URL, "srv", d, ISSUE, _slot_db(done)) == []
    assert W.slot_done(URL, "srv", d, ISSUE, _slot_db(done))
    assert W.pending_forecast_stations(URL, "srv", d, ISSUE, _slot_db([])) == [
        "101",
        "301",
    ]  # 아무것도 안 했으면 전부


def _main(monkeypatch, tmp_path, pending, switch="true", extra=()):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    d = _dicts_with_nonrep()
    calls = _main_env(monkeypatch, d, clock, api, switch=switch)
    seen = []
    inner = W.run_slot
    monkeypatch.setattr(W, "run_slot", lambda *a, **k: (seen.append(k.get("stations")), inner(*a, **k))[1])
    monkeypatch.setattr(W, "load_soil_prior", lambda *a, **k: {})
    asked = []
    monkeypatch.setattr(W, "slot_done", lambda *a, **k: not pending)  # 아무 지점도 안 남았으면 끝난 회차
    monkeypatch.setattr(W, "pending_forecast_stations", lambda *a, **k: (asked.append(1), list(pending))[1])
    code = W.main(
        ["--slot", "2026-06-10 02", "--out", str(tmp_path), "--event", "schedule",
         "--input-stations", "", "--input-backfill", "", "--input-dry-run", "", *extra]
    )  # fmt: skip
    return code, seen, calls, asked


def test_rerun_goes_only_over_the_failed_forecast_stations_plus_the_observation_only_ones(
    monkeypatch, tmp_path, capsys
):
    code, seen, calls, _ = _main(monkeypatch, tmp_path, pending=["301"])
    out = capsys.readouterr().out
    assert seen == [
        ["301", "216", "302", "303"]
    ]  # 101(이미 정시 성공)은 건너뜀, 관측만 하는 지점은 다시 받음
    assert "다시 도는 지점" in out and "301" in out and "이미 정시 성공한 1곳" in out
    assert len(calls) == 1  # 쓰기는 그대로


def test_rerun_with_the_switch_off_is_only_the_failed_forecast_stations(monkeypatch, tmp_path):
    _, seen, _, _ = _main(monkeypatch, tmp_path, pending=["301"], switch=None)
    assert seen == [["301"]]


def test_nothing_pending_ends_without_running_and_everything_pending_is_a_normal_full_run(
    monkeypatch, tmp_path, capsys
):
    code, seen, calls, _ = _main(monkeypatch, tmp_path, pending=[])
    assert code == 0 and seen == [] and calls == [] and "이미 정시 성공" in capsys.readouterr().out
    _, seen2, _, _ = _main(monkeypatch, tmp_path, pending=["101", "301"])
    assert seen2 == [None]  # 아무것도 안 한 회차는 지금처럼 전부


def test_a_manual_run_with_named_stations_never_asks_what_is_pending(monkeypatch, tmp_path):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    _main_env(monkeypatch, _dicts_with_nonrep(), clock, api)
    asked = []
    monkeypatch.setattr(W, "pending_forecast_stations", lambda *a, **k: asked.append(1) or [])
    W.main(["--slot", "2026-06-10 02", "--out", str(tmp_path), "--event", "workflow_dispatch",
            "--input-stations", "301", "--input-backfill", "", "--input-dry-run", "true"])  # fmt: skip
    assert asked == []


# ── 한 회차 단계별 시간·호출 수 ──────────────────────────────────────────
def test_summary_reports_stage_times_and_request_counts():
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    r = W.run_slot("아침", ISSUE, _dicts_with_nonrep(), KEY, http=api, clock=clock, stations=["101", "301"])
    fc = sum(1 for e in r.log.entries if e["kind"] == "fcst")
    asos = sum(1 for e in r.log.entries if e["kind"] == "asos")
    line = next(x for x in W.summary_text(r).splitlines() if x.startswith("단계:"))
    assert "예보 지점 2곳" in line and f"예보 요청 {fc}번" in line and f"ASOS {asos}번" in line
    assert "관측 지점 0곳" in line
    assert any(x.startswith("단계:") for x in W.brief_text(r).splitlines())


# ── retention 상한 ───────────────────────────────────────────────────────
def test_retention_weekly_cap_is_raised_and_the_report_points_to_s6():
    assert RT.DEFAULT_MAX_ROWS == 40000  # S4 결정 ⑤ 20,000 → S5-a 40,000(결정 0030)
    assert "#86" in RT.S6_NOTE and "S6" in RT.S6_NOTE


# ── 감시: 지점별 회차 기록 (P8) ──────────────────────────────────────────
def test_monitor_reads_every_recent_run_not_the_latest_twenty():
    db = FakeDb({"101": YESTERDAY, "216": YESTERDAY})
    M.read_state(URL, "srv", db, now=NOW)
    (u,) = [x for x in db.urls if "/rest/v1/forecast_runs" in x]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(u).query))
    assert "limit" not in q or q["limit"] != "20"
    assert q["run_at"].startswith("gte.") and q["collector"] == "eq.actions"
    since = pd.Timestamp(q["run_at"].removeprefix("gte."))
    assert since.tzinfo is not None
    assert (
        abs(
            (
                since.tz_convert("Asia/Seoul").tz_localize(None) - (NOW - pd.Timedelta(hours=48))
            ).total_seconds()
        )
        < 1
    )


def _run_row(stn, hour="02", result="정시 성공"):
    return dict(
        run_at=f"2026-10-27T{hour}:00:00+09:00", run_name="아침", stn_id=stn,
        started_at="2026-10-27T02:10:00+09:00", finished_at="2026-10-27T02:30:00+09:00",
        result=result, on_time=True, error=None,
    )  # fmt: skip


def test_thirty_forecast_stations_are_all_judged_and_a_missing_one_is_named():
    now = pd.Timestamp("2026-10-27 03:00")
    ids = [f"{100 + i}" for i in range(30)]
    runs = [_run_row(s) for s in ids if s != "117"]
    weather = {s: now.date() - pd.Timedelta(days=1) for s in ids}
    names = {s: f"지점{s}" for s in ids}
    alerts = check_health(now, runs, weather, names, expected_run_stations=ids)
    (a,) = alerts
    assert a.level == "warn" and a.code == "stale" and "117 지점117" in a.text and "회차 기록 없음" in a.text
    assert "켠 직후" in a.text  # 방금 켠 지점이면 다음 회차(02·17시)까지는 정상
    assert check_health(now, [_run_row(s) for s in ids], weather, names, expected_run_stations=ids) == []
    assert check_health(now, runs, weather, names) == []  # 넘기지 않으면 전과 같이 보지 않는다


def test_read_state_expects_runs_only_from_collected_stations_that_have_a_grid():
    db = FakeDb({"101": YESTERDAY, "216": YESTERDAY}, stations=[
        {"stn_id": "101", "name_ko": "춘천", "has_forecast": True, "is_active": True, "grid_nx": 73, "grid_ny": 134},
        {"stn_id": "216", "name_ko": "태백", "has_forecast": True, "is_active": True, "grid_nx": None, "grid_ny": None},
    ])  # fmt: skip
    # FakeDb 는 grid 칸을 돌려주지 않으므로 이 시험은 칸이 없을 때 "기다리는 지점 없음"임을 본다
    assert M.read_state(URL, "srv", db, now=NOW)["expected_run_stations"] == []


# ── 감시: DB 크기 한 줄 (사용자 승인 2026-11-09) ─────────────────────────
def test_db_size_line_tells_exact_from_estimate():
    assert (
        db_size_line(dict(mb=65.3, exact=True))
        == "DB 크기: 65.3MB (정확: db_size_bytes() 값) · 경고선 400MB·오류선 450MB"
    )
    assert (
        db_size_line(dict(mb=57.1, exact=False))
        == "DB 크기: 약 57.1MB (줄 수로 추정한 값 — db_size_bytes()를 쓰지 못함) · 경고선 360MB·오류선 405MB"
    )
    assert db_size_line(None, "HTTP 500 boom") == "DB 크기: 읽지 못함 — HTTP 500 boom"
    assert db_size_line(None) == ""


def _state_with(db_size=None, db_size_error=None):
    db = FakeDb({"101": YESTERDAY, "216": YESTERDAY}, heat={"101", "216", "108"})
    st = M.read_state(URL, "srv", db, now=NOW)
    if db_size is not None:
        st["db_size"] = db_size
    if db_size_error is not None:
        st["db_size_error"] = db_size_error
    return st


def test_every_monitor_run_prints_one_db_size_line_even_when_all_is_well():
    text, _ = M.run_monitor(_state_with(dict(mb=65.3, exact=True, source="rpc")), None, now=NOW)
    assert "이상 없음" in text
    assert text.splitlines()[-1] == "DB 크기: 65.3MB (정확: db_size_bytes() 값) · 경고선 400MB·오류선 450MB"
    plain, _ = M.run_monitor(_state_with(), None, now=NOW)
    assert "DB 크기" not in plain  # 크기를 못 넘긴 호출(전에 쓰던 모양)은 그대로


def test_falling_back_to_the_estimate_is_a_visible_warning():
    text, _ = M.run_monitor(_state_with(dict(mb=57.1, exact=False, source="estimate")), None, now=NOW)
    assert "정확한 값으로 읽지 못함" in text and "db_size_bytes()" in text
    assert "DB 크기: 약 57.1MB (줄 수로 추정한 값" in text
    exact, _ = M.run_monitor(_state_with(dict(mb=65.3, exact=True)), None, now=NOW)
    assert "정확한 값으로 읽지 못함" not in exact


def test_db_size_alert_points_to_the_s6_issue():
    (a,) = check_health(NOW, [], {}, {}, db_size=dict(mb=420.0, exact=True))[-1:]
    assert a.code == "dbsize" and "S6" in a.text and "#86" in a.text
