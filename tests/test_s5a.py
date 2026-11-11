"""S5-a: 대표 밖 지점에 3일 전망·물수지를 켜기 전 준비 (#87, 결정 0030). 입력은 모두 가짜 값이다.

- 스위치 `NONREP_SCENARIOS`(기본 꺼짐): 꺼져 있으면 시나리오가 시드돼 있어도 대표 밖 지점은 무시(전과 같음).
- 대표 밖은 "필요한 앞선 발표만": 02시 회차 3시간 전 + 24시간 전(어제 아침 발표), 17시 회차 3시간 전 + 15시간 전
  (오늘 아침 발표). 최근 2개(3·6시간 전)로는 어제를 채울 수 없어 전망이 실패한다.
- 14일 창 규칙은 대표 밖 전부(`weather_daily`·`soil_water_daily`·`water_balance_daily`). 대표 10곳은 그대로.
- `--wb-backfill`: 예보 없이 ASOS만으로 `water_balance_daily` 소급(20곳씩, dry-run 먼저).
"""

# ruff: noqa: E501

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import retention as RT  # noqa: E402
from croplib_dp.collectors import water as W  # noqa: E402
from test_collector import ISSUE, ISSUES, KEY, FakeApi, FakeClock  # noqa: E402
from test_obs_all import _main_env, _obs_dicts  # noqa: E402
from test_s4a import _issues_asked  # noqa: E402

TS = pd.Timestamp
WINDOW_START = "2026-05-27"  # ISSUE(6/10 02시)의 어제 6/9 포함 14일


def _d(seed=True, nonrep_ids=("301",)):
    """101 춘천(대표) + 301(대표 밖 예보 지점) + 관측만 하는 216·302·303. seed 면 301에 사과 k3·배추 k1을 시드한다."""
    d = _obs_dicts()
    d.stations["101"]["is_representative"] = True
    for sid in nonrep_ids:
        if sid not in d.stations:
            d.stations[sid] = dict(d.stations["301"], stn_id=sid, name_ko=f"예보{sid}")
        d.stations[sid].update(
            has_forecast=True, is_representative=False, grid_nx=73, grid_ny=135 + int(sid) % 50
        )
    if seed:
        for sid in nonrep_ids:
            for base in [
                s
                for s in d.scenarios
                if s["stn_id"] == "101" and s["kc_set_id"] in ("apple-k3", "kimchi_cabbage-k1")
            ]:
                d.scenarios.append(
                    dict(base, scenario_id=base["scenario_id"].replace("101", sid), stn_id=sid)
                )
    return d


def _run(
    d=None,
    nonrep=False,
    stations=("101", "301"),
    asos_last=dt.date(2026, 6, 9),
    soil_backfill=False,
    patch=None,
):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES, asos_last=asos_last)
    r = W.run_slot(
        "아침", ISSUE, d or _d(), KEY, http=api, clock=clock, stations=list(stations),
        nonrep_scen=nonrep, soil_backfill=soil_backfill,
    )  # fmt: skip
    return r, api


# ── 스위치 ───────────────────────────────────────────────────────────────
def test_switch_is_on_only_for_true_and_off_by_default():
    assert W.nonrep_scenarios_enabled({}) is False
    assert W.nonrep_scenarios_enabled({"NONREP_SCENARIOS": ""}) is False
    assert W.nonrep_scenarios_enabled({"NONREP_SCENARIOS": "false"}) is False
    assert W.nonrep_scenarios_enabled({"NONREP_SCENARIOS": " True "}) is True


def test_with_the_switch_off_seeded_scenarios_of_a_non_representative_station_are_ignored():
    r, _ = _run(nonrep=False)
    assert not [x for x in r.water_outlook if x["stn_id"] == "301"]
    assert not [x for x in r.water_balance_daily if x["scenario_id"].startswith("301-")]
    run = next(x for x in r.forecast_runs if x["stn_id"] == "301")
    assert run["result"] == "정시 성공" and run["rs_method"] is None  # c1·c2 와 같다(계산 조건 없는 지점)
    days = sorted(x["date"] for x in r.weather_daily if x["stn_id"] == "301")
    assert days[0] == WINDOW_START  # 14일 창 (S4-b 후속)
    assert [x for x in r.water_outlook if x["stn_id"] == "101"]  # 대표는 그대로


# ── 켜면: 전망·물수지, 14일 창 ────────────────────────────────────────────
def test_with_the_switch_on_a_non_representative_station_gets_outlook_and_summary_and_s3():
    r, _ = _run(nonrep=True, asos_last=dt.date(2026, 6, 8))  # 02시 회차: ASOS 어제(6/9)가 아직 없다
    assert not r.problems and not r.failed
    run = next(x for x in r.forecast_runs if x["stn_id"] == "301")
    assert run["result"] == "정시 성공" and run["rs_method"] == "S3" and run["error"] is None
    ol = [x for x in r.water_outlook if x["stn_id"] == "301"]
    sm = [x for x in r.water_outlook_summary if x["stn_id"] == "301"]
    assert len(ol) == 4 * 2 and len(sm) == 2  # 사과 k3·배추 k1 두 조건 × (리드 0~3일 4줄, 요약 1줄)


def test_non_representative_balance_rows_are_only_the_14_day_window_but_equal_the_full_season_values():
    win, _ = _run(nonrep=True)
    full, _ = _run(nonrep=True, soil_backfill=True)  # 소급은 창 없이 전부
    for sid_scen in ("301-apple-main-k3-loam-2026", "301-kimchi_cabbage-summer-k1-loam-2026"):
        w = [x for x in win.water_balance_daily if x["scenario_id"] == sid_scen]
        f = {x["date"]: x for x in full.water_balance_daily if x["scenario_id"] == sid_scen}
        assert w and min(x["date"] for x in w) >= WINDOW_START and len(w) <= W.SOIL_UPLOAD_DAYS
        assert len(f) > len(w)  # 전체는 작기 시작부터
        assert all(f[x["date"]] == x for x in w)  # 창만 올려도 전체와 같은 값(작기 시작부터 다시 계산)
    wd = [x for x in win.weather_daily if x["stn_id"] == "301"]
    assert (
        min(x["date"] for x in wd) == WINDOW_START and len(wd) == W.SOIL_UPLOAD_DAYS
    )  # 시나리오가 생겨도 weather 는 창만


def test_the_representative_is_unchanged_by_the_switch_and_by_a_non_representative_station():
    alone, _ = _run(nonrep=False, stations=("101",))
    both_on, _ = _run(nonrep=True, stations=("101", "301"))
    for table in (
        "weather_daily",
        "soil_water_daily",
        "water_balance_daily",
        "water_outlook",
        "water_outlook_summary",
        "forecast_runs",
    ):
        a = getattr(alone, table)
        b = [x for x in getattr(both_on, table) if x.get("stn_id", "101") == "101"]
        if table == "water_balance_daily":
            b = [x for x in getattr(both_on, table) if x["scenario_id"].startswith("101-")]
        assert a and a == b, table
    days = sorted(x["date"] for x in both_on.weather_daily if x["stn_id"] == "101")
    assert days[0] == "2026-03-01" and len(days) == 101  # 대표는 3/1~어제 전체 시즌


# ── 필요한 앞선 발표만 ───────────────────────────────────────────────────
def test_02_slot_asks_3h_and_24h_earlier_issues_for_a_non_representative():
    _, api = _run(nonrep=True)
    assert _issues_asked(api, 73, 136) == {
        "202606100200",
        "202606092300",
        "202606090200",
    }  # 서비스·3시간 전·24시간 전
    assert len(_issues_asked(api, 73, 134)) == 1 + 8  # 대표 101 은 앞선 발표 8개 그대로


def test_17_slot_asks_3h_and_15h_earlier_issues():
    clock = FakeClock("2026-06-10 17:20")
    issues = [TS("2026-06-09 02:00") + pd.Timedelta(hours=3 * k) for k in range(16)]
    api = FakeApi(clock, issues)
    r = W.run_slot(
        "저녁", TS("2026-06-10 17:00"), _d(), KEY, http=api, clock=clock, stations=["301"], nonrep_scen=True
    )
    assert _issues_asked(api, 73, 136) == {
        "202606101700",
        "202606101400",
        "202606100200",
    }  # 서비스·3시간 전·오늘 아침 발표
    run = next(x for x in r.forecast_runs if x["stn_id"] == "301")
    assert run["result"] == "정시 성공" and [x for x in r.water_outlook if x["stn_id"] == "301"]


def test_the_two_most_recent_earlier_issues_cannot_fill_yesterday_so_the_outlook_falls_back_to_backup(
    monkeypatch,
):
    """S4 의 옛 규칙(3시간·6시간 전)으로는 02시 회차의 어제를 예보로 채울 수 없다 — 그래서 '필요한 것만'으로 바꿨다.
    이슈 #100 이후에는 실패하지 않고 채운 줄로 출발하는 '백업'으로 끝난다(필요한 발표를 받으면 정시 성공)."""
    monkeypatch.setattr(W, "NONREP_CATCHUP_BACK", {2: (3, 6), 17: (3, 6)})
    r, _ = _run(nonrep=True, asos_last=dt.date(2026, 6, 8))
    run = next(x for x in r.forecast_runs if x["stn_id"] == "301")
    assert run["result"] == "백업: 직전 발표" and run["error"] is None
    assert any("301" in w and "지연일" in w for w in r.warnings)
    monkeypatch.undo()
    ok, _ = _run(nonrep=True, asos_last=dt.date(2026, 6, 8))
    assert next(x for x in ok.forecast_runs if x["stn_id"] == "301")["result"] == "정시 성공"


# ── 소급: --wb-backfill ──────────────────────────────────────────────────
def _backfill(stations=("301",), d=None, asos_last=dt.date(2026, 6, 9)):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES, asos_last=asos_last)
    return W.run_wb_backfill("아침", ISSUE, d or _d(), KEY, list(stations), http=api, clock=clock), api


def test_wb_backfill_writes_only_water_balance_rows_from_the_season_start_without_any_forecast_call():
    r, api = _backfill()
    assert r.water_balance_daily and not r.weather_daily and not r.soil_water_daily
    assert not r.forecast_runs and not r.water_outlook and not r.water_outlook_summary and not r.raw_files
    assert not [u for u in api.calls if "base_date" in u]  # 예보는 한 번도 받지 않는다(ASOS 한 번)
    assert (
        min(x["date"] for x in r.water_balance_daily if x["scenario_id"].startswith("301-apple"))
        == "2026-04-01"
    )
    assert r.wb_backfill and not r.failed


def test_wb_backfill_rows_equal_what_a_regular_slot_computes_for_the_whole_season():
    bf, _ = _backfill()
    full, _ = _run(nonrep=True, soil_backfill=True, stations=("301",))
    a = {(x["scenario_id"], x["date"]): x for x in bf.water_balance_daily}
    b = {(x["scenario_id"], x["date"]): x for x in full.water_balance_daily}
    assert a and a == b


def test_wb_backfill_scope_stops_unnamed_too_many_and_stations_without_conditions():
    d = _d()
    with pytest.raises(SystemExit, match="지점 번호"):
        W.check_wb_backfill_scope(d, None)
    many = [str(500 + i) for i in range(21)]
    d2 = _d(nonrep_ids=("301", *many))
    with pytest.raises(SystemExit, match="20곳"):
        W.check_wb_backfill_scope(d2, ["301", *many])
    with pytest.raises(SystemExit, match="302"):
        W.check_wb_backfill_scope(d, ["301", "302"])  # 302 는 관측만 하는 지점(계산 조건·예보 없음)
    with pytest.raises(SystemExit, match="101"):
        W.check_wb_backfill_scope(d, ["101"])  # 대표는 대상이 아니다
    W.check_wb_backfill_scope(d, ["301"])  # 통과


# ── 수동 실행 입력 ───────────────────────────────────────────────────────
def test_dispatch_options_for_wb_backfill_and_the_dry_run_only_scenario_switch():
    o = W.dispatch_options("workflow_dispatch", "301", "", "true", wb_backfill="true")
    assert o.wb_backfill and o.write is False and o.skip_if_done is False and o.stations == ["301"]
    o = W.dispatch_options("workflow_dispatch", "301", "", "true", nonrep_scenarios="true")
    assert o.nonrep_dry is True and o.write is False  # 시험 실행에서만 켜 볼 수 있다
    with pytest.raises(SystemExit, match="dry_run"):
        W.dispatch_options(
            "workflow_dispatch", "301", "", "", nonrep_scenarios="true"
        )  # 쓰는 실행에는 못 쓴다
    with pytest.raises(SystemExit, match="동시에"):
        W.dispatch_options("workflow_dispatch", "301", "true", "true", wb_backfill="true")
    s = W.dispatch_options("schedule", "301", "true", "true", wb_backfill="true", nonrep_scenarios="true")
    assert (s.wb_backfill, s.nonrep_dry, s.write, s.skip_if_done, s.stations) == (
        False,
        False,
        True,
        True,
        None,
    )


def _main(monkeypatch, tmp_path, args, switch="true"):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    calls = _main_env(monkeypatch, _d(), clock, api, switch=None)
    real_wb = W.run_wb_backfill
    monkeypatch.setattr(W, "run_wb_backfill", lambda *a, **k: real_wb(*a, **{**k, "http": api}))
    if switch is None:
        monkeypatch.delenv("NONREP_SCENARIOS", raising=False)
    else:
        monkeypatch.setenv("NONREP_SCENARIOS", switch)
    base = [
        "--slot",
        "2026-06-10 02",
        "--out",
        str(tmp_path),
        "--event",
        "workflow_dispatch",
        "--input-stations",
        "301",
    ]
    return W.main([*base, *args]), calls


def test_main_wb_backfill_dry_run_reports_rows_and_growth_and_never_writes(monkeypatch, tmp_path, capsys):
    code, calls = _main(
        monkeypatch,
        tmp_path,
        ["--input-backfill", "", "--input-dry-run", "true", "--input-wb-backfill", "true"],
        switch=None,
    )
    out = capsys.readouterr().out
    assert code == 0 and calls == []
    assert (
        "water_balance_daily" in out
        and "예상 DB 증가" in out
        and "DB 올리기: 안 함" in out
        and "계약 검사: 통과" in out
    )


def test_main_wb_backfill_write_needs_the_switch_on(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="NONREP_SCENARIOS"):
        _main(
            monkeypatch,
            tmp_path,
            ["--input-backfill", "", "--input-dry-run", "", "--input-wb-backfill", "true"],
            switch="false",
        )
    code, calls = _main(
        monkeypatch,
        tmp_path,
        ["--input-backfill", "", "--input-dry-run", "", "--input-wb-backfill", "true"],
        switch="true",
    )
    assert code == 0 and len(calls) == 1


def test_main_dry_run_with_the_scenario_input_computes_outlooks_without_the_variable(
    monkeypatch, tmp_path, capsys
):
    code, calls = _main(
        monkeypatch,
        tmp_path,
        ["--input-backfill", "", "--input-dry-run", "true", "--input-nonrep-scenarios", "true"],
        switch=None,
    )
    out = capsys.readouterr().out
    rows = json.loads((tmp_path / "20260610_0200" / "water_outlook.json").read_text(encoding="utf-8"))
    assert code == 0 and calls == [] and any(x["stn_id"] == "301" for x in rows)
    assert "DB 올리기: 안 함" in out


# ── 요약 ─────────────────────────────────────────────────────────────────
def test_summary_reports_the_scenario_calculation_time_and_count():
    r, _ = _run(nonrep=True)
    line = next(x for x in W.summary_text(r).splitlines() if x.startswith("단계:"))
    n_scen = sum(len(s["scenarios"]) for s in r.stations)
    assert n_scen == 5 and f"시나리오 {n_scen}개 계산" in line  # 대표 101(3개) + 대표 밖 301(2개)


def test_brief_stays_under_4kb_with_97_forecast_stations_and_still_names_the_failures():
    """예보 지점이 97곳이 되면 지점마다 한 줄은 알림 칸(약 4KB)에 안 들어간다 → 대표 밖은 한 줄로 모으고 정시 성공이
    아닌 지점만 따로 적는다."""
    r, _ = _run()
    base = r.stations[1]
    fake = []
    for i in range(87):
        s = dict(
            base,
            stn_id=f"{300 + i}",
            name="가나다라마바사",
            result="정시 성공",
            on_time=True,
            attempts=1,
            scenarios=[],
        )
        fake.append(s)
    fake[5].update(result="지연 수집")
    fake[9].update(result="실패", error="ReadTimeout: x" * 5)
    r.stations = [r.stations[0], *fake]
    text = W.brief_text(r)
    assert len(text.encode()) < 3800
    assert "대표 밖 예보 지점 87곳" in text and "정시 성공 85" in text
    assert "305" in text and "309" in text and "지연 수집" in text  # 정시 성공이 아닌 지점은 이름을 적는다
    assert "DB 올리기" in text  # 잘리지 않고 끝까지 들어간다


# ── retention 상한 ───────────────────────────────────────────────────────
def test_retention_cap_is_40000_so_that_one_skipped_week_still_fits():
    assert RT.DEFAULT_MAX_ROWS == 40000
    weekly = 7 * (500 + 1740)  # 대표 + 대표 밖 87곳 전망(S5)
    assert weekly == 15680 and weekly < RT.DEFAULT_MAX_ROWS
    assert 2 * weekly <= RT.DEFAULT_MAX_ROWS  # 한 주를 건너뛰어도(31,360줄) 상한 안
    assert 3 * weekly > RT.DEFAULT_MAX_ROWS  # 세 주 밀리면(47,040줄) 멈추고 사람이 본다(안전장치는 그대로)
