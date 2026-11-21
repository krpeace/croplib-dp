"""시설 재배 공개 조건의 계산: 비 0 · 쓰는 물(etc_only) · 알림 작형 이름 (이슈 #128, 결정 0039).

시설 재배(`crop_seasons.is_protected = true`, 포도 비가림 · 시설가온)는 지붕 아래라 비를 계산에 넣지 않고,
판정하지 않고 "쓰는 물"(ETc)만 낸다: 작기 안 `etc_only`, 작기 밖 `dormant`, Dr 관련 열은 모두 비운다.
노지(사과 · 배추)는 한 자리도 바뀌면 안 된다: `tests/data/open_field_golden.txt`(시설 갈래를 넣기 전
`main` 2db9fb0 의 코드로 뽑은 값)와 비교한다. 다시 뽑는 법은 `tests/data/make_open_field_golden.py` 머리말.
입력은 모두 가짜 값이다.
"""

import datetime as dt
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

import make_open_field_golden as G  # noqa: E402

from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.core.water_balance import CropParams, Soil, observed_water_balance  # noqa: E402
from croplib_dp.io.contract import SPECS, check_rows  # noqa: E402
from test_collector import ISSUE, ISSUES, KEY, FakeApi, FakeClock, _dicts  # noqa: E402

TS = pd.Timestamp
ETC_ONLY = "etc_only"
DR_COLS = ("dr_prev_mm", "ks", "etc_adj_mm", "dp_mm", "dr_mm")
OL_DR_COLS = ("dr_center_mm", "dr_early_mm", "dr_late_mm")
SUM_NONE_COLS = (
    "dr_start_mm",
    "need_center_in",
    "need_early_in",
    "need_late_in",
    "advice_net_mm",
    "advice_gross_mm",
)
RAIN = "rainshelter"
HEAT = "heated"
SHORT = "short"


# ── 사전 자료: 지금 가짜 조건에 포도 시설 작형을 더한다 ─────────────────────────
def _season(sid, start, lens, name, protected=True, default=False):
    d = dict(
        season_type_id=sid,
        crop_id="grape",
        name_ko=name,
        start_mmdd=start,
        l_ini_d=lens[0],
        l_dev_d=lens[1],
        l_mid_d=lens[2],
        l_late_d=lens[3],
        is_default=default,
    )
    if protected is not None:
        d["is_protected"] = protected
    return d


def _scenario(stn, sid, start, end, default=True):
    return dict(
        scenario_id=f"{stn}-{sid}-k1-loam-2026",
        stn_id=stn,
        crop_id="grape",
        season_type_id=sid,
        kc_set_id="grape-k1",
        soil_code="loam",
        season_year=2026,
        season_start=start,
        season_end=end,
        dr0_mm=0,
        taw_mm=130,
        raw_mm=65,
        is_default=default,
    )


def protected_dicts(stations=("101",), protected=True, with_open_field=True):
    """test_collector._dicts() + 포도 비가림(4/20 시작) · 시설가온(1/1 시작) · 짧은 시험 작형(5/1~5/20).
    기존 노지 작형 줄에는 is_protected = False 를 단다(DB 기본값과 같다)."""
    d = _dicts()
    for s in d.seasons.values():
        s["is_protected"] = False
    d.crops["grape"] = dict(
        crop_id="grape", name_ko="포도", kc_ini=0.3, kc_mid=0.85, kc_end=0.45, zr_m=1.0, p=0.5, h_m=2.0
    )
    d.seasons["grape-rainshelter"] = _season(
        "grape-rainshelter", "04-20", (25, 45, 90, 45), "포도 비가림", protected, True
    )
    d.seasons["grape-heated"] = _season(
        "grape-heated", None, (35, 40, 85, 30), "포도 시설가온", protected, True
    )
    d.seasons["grape-short"] = _season("grape-short", "05-01", (5, 5, 5, 5), "포도 짧은 시험", protected)
    d.kc_sets["grape-k1"] = dict(
        kc_set_id="grape-k1", crop_id="grape", set_no=1, kc_ini=0.3, kc_mid=0.85, kc_end=0.45, is_default=True
    )
    if not with_open_field:
        d.scenarios[:] = []
    for stn in stations:
        d.scenarios += [
            _scenario(stn, "grape-rainshelter", "2026-04-20", "2026-10-31"),
            _scenario(stn, "grape-heated", "2026-01-01", "2026-07-09"),
            _scenario(stn, "grape-short", "2026-05-01", "2026-05-20", default=False),
        ]
    d.scenarios.sort(key=lambda r: r["scenario_id"])
    return d


class RainApi(FakeApi):
    """모든 날 같은 강수(sumRn)를 주는 가짜 ASOS (비 값이 시설 결과에 들어가지 않는지 보려고)"""

    def __init__(self, *a, rain="0.0", **k):
        super().__init__(*a, **k)
        self.rain = rain

    def __call__(self, url, timeout):
        import json
        import urllib.parse

        code, body = super().__call__(url, timeout)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if "startDt" in q:
            doc = json.loads(body)
            items = doc["response"]["body"]["items"]["item"]
            for it in items:
                it["sumRn"] = self.rain
            body = json.dumps(doc).encode()
        return code, body


def run(d=None, stations=("101",), rain=None, start="2026-06-10 02:20", issue=ISSUE, name="아침", **kw):
    clock = FakeClock(start)
    api_cls = FakeApi if rain is None else RainApi
    extra = {} if rain is None else dict(rain=rain)
    api = api_cls(clock, kw.pop("issues", ISSUES), **extra, **kw)
    d = d or protected_dicts(stations)
    return W.run_slot(name, issue, d, KEY, http=api, clock=clock, stations=list(stations)), d


def rows_of(table, sid_part, stn="101"):
    return [x for x in table if x["scenario_id"] == f"{stn}-{sid_part}-k1-loam-2026"]


def wb_rows(r, season, stn="101"):
    return rows_of(r.water_balance_daily, season, stn)


def apple_k3(r, stn="101"):
    return [x for x in r.water_balance_daily if x["scenario_id"] == f"{stn}-apple-main-k3-loam-2026"]


# ── ① 시설 조건: 비가 와도 ETc 만, Dr 비움, etc_only ────────────────────────────
@pytest.mark.parametrize("season", [RAIN, HEAT])
def test_protected_rows_are_etc_only_with_empty_dr_and_zero_irrigation(season):
    r, _ = run(rain="60.0")  # 날마다 60 mm 비가 와도
    rows = wb_rows(r, f"grape-{season}")
    assert rows and not r.failed, r.problems
    for x in rows:
        assert x["status"] == ETC_ONLY, x
        assert all(x[c] is None for c in DR_COLS), x
        assert x["irr_net_mm"] == 0.0
        assert x["kc"] > 0 and x["eto_mm"] is not None
        assert x["etc_mm"] == pytest.approx(x["kc"] * x["eto_mm"])


def test_protected_result_does_not_depend_on_the_rain():
    dry, _ = run(rain="0.0")
    wet, _ = run(rain="80.0")
    for season in (RAIN, HEAT):
        a, b = wb_rows(dry, f"grape-{season}"), wb_rows(wet, f"grape-{season}")
        assert len(a) == len(b) > 0
        for x, y in zip(a, b, strict=True):
            keep = {k: v for k, v in x.items() if k != "rain_mm"}
            assert keep == {k: v for k, v in y.items() if k != "rain_mm"}
    # 기록 열 rain_mm 에는 바깥 비 값을 그대로 남겨도 된다(화면이 시설 포도에서 지운다)
    observed = [x for x in wb_rows(wet, "grape-rainshelter") if x["eto_src"] == "관측"]
    assert observed and all(x["rain_mm"] == 80.0 for x in observed)


def test_open_field_does_use_the_rain_so_the_test_above_is_meaningful():
    dry, _ = run(rain="0.0")
    wet, _ = run(rain="80.0")
    a, b = apple_k3(dry), apple_k3(wet)
    assert [x["dr_mm"] for x in a] != [x["dr_mm"] for x in b]  # 노지는 비로 Dr 이 달라진다


def test_protected_eto_src_and_filled_follow_the_same_rule_as_open_field():
    r, _ = run(asos_last=dt.date(2026, 6, 8))  # 어제(6/9) 관측이 아직 없는 지연일: 예보로 채움
    apple = {x["date"]: x for x in apple_k3(r)}
    for season in (RAIN, HEAT):
        rows = wb_rows(r, f"grape-{season}")
        assert (
            rows[-1]["date"] == "2026-06-09" and rows[-1]["eto_src"] == "예보" and rows[-1]["filled"] is True
        )
        for x in rows:
            assert x["eto_src"] in ("관측", "예보", "없음")
            assert x["filled"] is (x["eto_src"] != "관측")
            if x["date"] in apple:  # 같은 날 · 같은 기상이면 노지와 같은 규칙으로 채운다
                assert x["eto_src"] == apple[x["date"]]["eto_src"]
                assert x["eto_mm"] == pytest.approx(apple[x["date"]]["eto_mm"])


def test_core_observed_water_balance_protected_flag_directly():
    crop = CropParams(dt.date(2026, 5, 1), 5, 5, 5, 5, 0.3, 0.85, 0.45)
    soil = Soil(taw_mm=130, raw_mm=65)
    days = [
        dict(
            date=dt.date(2026, 5, 1) + dt.timedelta(days=k),
            eto_mm=4.0,
            rain_mm=100.0 if k == 3 else 0.0,
            filled=False,
        )
        for k in range(25)
    ]
    prot = observed_water_balance(days, crop, soil, protected=True)
    open_ = observed_water_balance(days, crop, soil)
    assert [x["status"] for x in prot[:20]] == [ETC_ONLY] * 20 and [x["status"] for x in prot[20:]] == [
        "dormant"
    ] * 5
    assert all(x[c] is None for x in prot for c in DR_COLS) and all(x["irr_net_mm"] == 0.0 for x in prot)
    assert [x["etc_mm"] for x in prot] == pytest.approx([x["etc_mm"] for x in open_])  # 쓰는 물은 노지와 같다
    assert prot[3]["rain_mm"] == 100.0  # 기록 열
    assert open_[3]["dr_mm"] is not None and open_[3]["status"] != ETC_ONLY


# ── ② 작기 밖은 dormant ─────────────────────────────────────────────────────
def test_outside_the_season_is_dormant_and_dr_stays_empty():
    r, _ = run()
    rows = wb_rows(r, "grape-short")  # 5/1 ~ 5/20 만 작기, 오늘(6/10)은 끝난 뒤
    by = {x["date"]: x for x in rows}
    assert by["2026-05-20"]["status"] == ETC_ONLY and by["2026-05-20"]["kc"] > 0
    assert by["2026-05-21"]["status"] == "dormant" and by["2026-05-21"]["kc"] == 0
    assert by["2026-06-09"]["status"] == "dormant"
    assert all(x[c] is None for x in rows for c in DR_COLS)


def test_before_the_season_starts_is_dormant():
    d = protected_dicts()
    for s in d.scenarios:
        if s["season_type_id"] == "grape-short":
            s["season_start"] = "2026-07-01"
    r, _ = run(d=d)
    rows = wb_rows(r, "grape-short")
    assert rows and all(x["status"] == "dormant" and x["kc"] == 0 for x in rows)
    assert all(x[c] is None for x in rows for c in DR_COLS)


# ── ③ 전망 · 요약 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("season", [RAIN, HEAT])
def test_outlook_and_summary_are_etc_only(season):
    r, _ = run(rain="60.0")
    ol = rows_of(r.water_outlook, f"grape-{season}")
    assert [o["lead_day"] for o in ol] == [0, 1, 2, 3] and not r.failed, r.problems
    for o in ol:
        assert o["status"] == ETC_ONLY
        assert all(o[c] is None for c in OL_DR_COLS)
        assert o["eto_mm"] is not None and o["etc_mm"] is not None and o["etc_err_mm"] is not None
    (s,) = rows_of(r.water_outlook_summary, f"grape-{season}")
    assert s["status_start"] == ETC_ONLY and s["horizon_days"] == 3
    assert s["start_date"] == "2026-06-09"
    assert all(s[c] is None for c in SUM_NONE_COLS)
    assert s["etc_sum_mm"] == pytest.approx(sum(o["etc_mm"] for o in ol[:3]))  # 3일 ETc 합
    assert s["etc_sum_err_mm"] is not None


def test_outlook_outside_the_season_is_dormant():
    r, _ = run()
    ol = rows_of(r.water_outlook, "grape-short")
    assert ol and all(o["status"] == "dormant" for o in ol)
    assert all(o[c] is None for o in ol for c in OL_DR_COLS)
    (s,) = rows_of(r.water_outlook_summary, "grape-short")
    assert s["status_start"] == "dormant" and all(s[c] is None for c in SUM_NONE_COLS)
    assert s["etc_sum_mm"] == 0


def test_evening_slot_outlook_is_etc_only_too():
    issues = ISSUES + [TS("2026-06-10 05:00") + pd.Timedelta(hours=3 * k) for k in range(5)]
    r, _ = run(start="2026-06-10 17:20", issue=TS("2026-06-10 17:00"), name="저녁", issues=issues)
    ol = rows_of(r.water_outlook, "grape-rainshelter")
    assert [o["lead_day"] for o in ol] == [1, 2, 3, 4] and all(o["status"] == ETC_ONLY for o in ol)
    (s,) = rows_of(r.water_outlook_summary, "grape-rainshelter")
    assert s["status_start"] == ETC_ONLY and s["start_date"] == "2026-06-10"
    assert not r.failed, r.problems


def test_protected_outlook_rain_columns_keep_the_forecast_values_as_records():
    r, _ = run()
    prot = rows_of(r.water_outlook, "grape-rainshelter")
    apple = [
        x for x in r.water_outlook if x["scenario_id"] == "101-apple-main-k3-loam-2026"
    ]  # 같은 발표의 노지 줄
    assert [o["rain_fcst_mm"] for o in prot] == [o["rain_fcst_mm"] for o in apple]
    assert [o["eto_mm"] for o in prot] == pytest.approx([o["eto_mm"] for o in apple])


# ── ④ 노지 사과 · 배추 결과가 이 PR 전과 같음 ─────────────────────────────────────
def _same(a, b, path="") -> None:
    """JSON 으로 바꾼 값이 같다(실수는 상대 1e-9, 플랫폼 차이 대비)"""
    if isinstance(a, dict):
        assert isinstance(b, dict) and a.keys() == b.keys(), (path, sorted(a.keys() ^ b.keys()))
        for k in a:
            _same(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), (path, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _same(x, y, f"{path}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert a is not None and b is not None and a == pytest.approx(b, rel=1e-9, abs=1e-12), (path, a, b)
    else:
        assert a == b, (path, a, b)


def test_open_field_results_equal_the_values_taken_before_this_change():
    golden = G.load_golden()
    assert set(golden) == {"morning", "lag", "evening"}
    now = G.snapshot()
    for case in golden:
        for table in G.TABLES:
            assert golden[case][table], (case, table)
            _same(now[case][table], golden[case][table], f"{case}.{table}")


def test_adding_protected_scenarios_leaves_open_field_rows_and_forecast_runs_unchanged():
    golden = G.load_golden()
    mixed = G.snapshot(dicts=lambda: protected_dicts(("101", "216")))
    for case in golden:
        for table in ("water_balance_daily", "water_outlook", "water_outlook_summary"):
            ids = {x["scenario_id"] for x in golden[case][table]}
            got = [x for x in mixed[case][table] if x["scenario_id"] in ids]
            _same(got, golden[case][table], f"{case}.{table}")
            assert {x["scenario_id"] for x in mixed[case][table]} > ids  # 시설 조건 줄은 따로 있다
        _same(mixed[case]["forecast_runs"], golden[case]["forecast_runs"], f"{case}.forecast_runs")


def test_station_with_only_protected_scenarios_still_succeeds():
    d = protected_dicts(with_open_field=False)
    r, _ = run(d=d)
    assert not r.failed, r.problems
    x = r.forecast_runs[0]
    assert x["result"] == "정시 성공" and x["obs_prev_date"] is None
    assert rows_of(r.water_outlook, "grape-rainshelter") and rows_of(r.water_outlook_summary, "grape-heated")


# ── ⑤ 계약 검사 ─────────────────────────────────────────────────────────────
def test_contract_accepts_etc_only_rows_of_the_three_tables():
    r, d = run(rain="60.0")
    assert any(x["status"] == ETC_ONLY for x in r.water_balance_daily)
    assert any(x["status"] == ETC_ONLY for x in r.water_outlook)
    assert any(x["status_start"] == ETC_ONLY for x in r.water_outlook_summary)
    assert W.check_contract(r, W.taw_map(d)) == []


def test_contract_status_lists_after_the_change():
    wb = SPECS["water_balance_daily"].enums["status"]
    ol = SPECS["water_outlook"].enums["status"]
    sm = SPECS["water_outlook_summary"].enums["status_start"]
    assert ETC_ONLY in wb and ETC_ONLY in ol and ETC_ONLY in sm
    assert "wet" in wb and "wet" in sm and "wet" not in ol  # 전망은 지금처럼 과습 없음
    probs = check_rows(
        "water_outlook", [dict(_outlook_row(), status="wet")], ignore=("run_id",), extra=W.RUN_KEY
    )
    assert any("status" in p for p in probs)
    probs = check_rows(
        "water_outlook", [dict(_outlook_row(), status="etc_only_x")], ignore=("run_id",), extra=W.RUN_KEY
    )
    assert any("status" in p for p in probs)


def _outlook_row():
    r, _ = run()
    return dict(rows_of(r.water_outlook, "grape-rainshelter")[0])


def test_contract_still_checks_dr_range_for_open_field_rows():
    r, d = run()
    bad = dict(apple_k3(r)[0], dr_mm=9999.0)
    probs = check_rows("water_balance_daily", [bad], W.taw_map(d))
    assert any("dr_mm" in p for p in probs)


# ── ⑥ 알림: 작형 이름 ────────────────────────────────────────────────────────
def test_brief_names_the_two_grape_seasons_apart_and_shows_water_use():
    r, _ = run()
    text = W.brief_text(r)
    line = next(x for x in text.splitlines() if x.startswith("- 101 "))
    assert "포도 비가림 쓰는 물" in line and "포도 시설가온 쓰는 물" in line and "mm" in line
    assert "etc_only" not in line
    assert "사과" in line and "포도 짧은 시험" not in line  # 기본 조건만
    assert line.count("포도") == 2


def test_brief_water_use_number_is_the_last_day_etc():
    r, _ = run()
    last = wb_rows(r, "grape-rainshelter")[-1]
    line = next(x for x in W.brief_text(r).splitlines() if x.startswith("- 101 "))
    assert f"포도 비가림 쓰는 물 {last['etc_mm']:.1f}mm" in line


def test_brief_dormant_protected_season_is_shown_as_dormant_without_a_number():
    d = protected_dicts()
    d.scenarios = [s for s in d.scenarios if s["season_type_id"] != "grape-short"]
    for s in d.scenarios:
        if s["season_type_id"] == "grape-heated":
            s["season_start"] = "2026-07-01"
    r, _ = run(d=d)
    line = next(x for x in W.brief_text(r).splitlines() if x.startswith("- 101 "))
    assert "포도 시설가온 dormant" in line and "포도 비가림 쓰는 물" in line


def test_crop_name_fallback_without_a_season_name_and_grape_in_crop_ko():
    assert W.CROP_KO["grape"] == "포도" and W.CROP_KO["apple"] == "사과"
    r, _ = run()  # 기본 가짜 사전의 사과·배추 작형에는 name_ko 가 없다 → 작물 이름
    line = next(x for x in W.brief_text(r).splitlines() if x.startswith("- 101 "))
    assert "사과 " in line
    d = protected_dicts()
    d.seasons["kimchi_cabbage-summer"]["name_ko"] = "고랭지배추"
    d.seasons["apple-main"]["name_ko"] = "사과"
    r, _ = run(d=d)
    assert "고랭지배추" in W.brief_text(r)


def test_summary_text_shows_water_use_for_etc_only_rows():
    r, _ = run()
    text = W.summary_text(r)
    assert "Dr None" not in text
    assert "101-grape-rainshelter-k1-loam-2026" in text and "쓰는 물" in text


def test_brief_of_ten_representative_stations_with_four_default_conditions_fits_the_4kb_box():
    scen = [
        dict(
            scenario_id="s1",
            crop_id="apple",
            season_name="사과",
            is_default=True,
            status="normal",
            dr_mm=12.3,
            need=dict(center=2),
        ),
        dict(
            scenario_id="s2",
            crop_id="grape",
            season_name="포도 비가림",
            is_default=True,
            status=ETC_ONLY,
            protected=True,
            etc_mm=3.24,
        ),
        dict(
            scenario_id="s3",
            crop_id="grape",
            season_name="포도 시설가온",
            is_default=True,
            status=ETC_ONLY,
            protected=True,
            etc_mm=2.11,
        ),
        dict(
            scenario_id="s4",
            crop_id="kimchi_cabbage",
            season_name="고랭지배추",
            is_default=True,
            status="dormant",
        ),
    ]
    r = W.SlotResult(run_name="아침", issue=ISSUE)
    names = ["춘천", "서울", "강릉", "대전", "대구", "광주", "부산", "제주", "서귀포", "울릉도"]
    r.stations = [
        dict(
            stn_id=str(100 + i),
            name=n,
            asos_ok=True,
            asos_last="2027-01-04",
            result="정시 성공",
            on_time=True,
            attempts=1,
            error="",
            scenarios=scen,
            representative=True,
        )
        for i, n in enumerate(names)
    ]
    size = len(W.brief_text(r).encode())
    assert size < 3800, size  # 알림 칸(약 4KB, 결정 0030) 안


# ── is_protected 를 못 읽으면 조용히 거짓으로 두지 않는다 ───────────────────────────
@pytest.mark.parametrize("protected", [None, "absent"])
def test_unreadable_is_protected_is_a_problem_and_the_condition_is_not_calculated(protected):
    d = protected_dicts()
    if protected == "absent":
        for k in ("grape-rainshelter", "grape-heated", "grape-short"):
            d.seasons[k].pop("is_protected")
    else:
        for k in ("grape-rainshelter", "grape-heated", "grape-short"):
            d.seasons[k]["is_protected"] = None
    r, _ = run(d=d)
    assert r.failed and any("is_protected" in p and "grape-rainshelter" in p for p in r.problems), r.problems
    assert wb_rows(r, "grape-rainshelter") == [] and rows_of(r.water_outlook, "grape-heated") == []
    assert apple_k3(r)  # 다른 조건은 그대로 계산한다


def test_season_row_missing_from_one_condition_does_not_hide_the_others():
    d = protected_dicts()
    d.seasons["grape-heated"].pop("is_protected")
    r, _ = run(d=d)
    assert wb_rows(r, "grape-heated") == [] and wb_rows(r, "grape-rainshelter")
    assert sum("is_protected" in p for p in r.problems) == 1


# ── 소급 경로도 같은 갈래 ───────────────────────────────────────────────────────
def test_wb_backfill_uses_the_same_protected_branch():
    d = protected_dicts(("216",))
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    r = W.run_wb_backfill("아침", ISSUE, d, KEY, ["216"], http=api, clock=clock)
    prot = wb_rows(r, "grape-rainshelter", "216")
    assert prot and all(x["status"] == ETC_ONLY and all(x[c] is None for c in DR_COLS) for x in prot)
    open_ = [x for x in r.water_balance_daily if x["scenario_id"] == "216-apple-main-k3-loam-2026"]
    assert open_ and all(x["dr_mm"] is not None for x in open_)
    assert not r.problems and W.check_contract(r, W.taw_map(d)) == []
    assert math.isfinite(sum(x["etc_mm"] for x in prot))
