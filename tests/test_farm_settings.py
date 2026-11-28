"""농장 계산의 회원 설정: 관수 방식(관수효율) · 뿌리 깊이 · 생육 단계 일수 (CropMon v2.6, 이슈 #140).

입력은 모두 가짜 값이다. 가짜 `irrigation_methods` 의 drip 효율 0.95 는 공개 계산의 관수효율과 같은 값이다
(설정이 모두 빈 농장이 공개 결과와 같다는 시험 `test_farm_calc.py` 는 이 전제에서 성립한다).
"""

# ruff: noqa: E501, F811  (한글은 전각이라 긴 줄로 셈 · world 는 다른 시험 파일에서 가져온 fixture)

import datetime as dt
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from croplib_dp.collectors import farm_calc as F  # noqa: E402
from croplib_dp.core.farm_water import (  # noqa: E402
    irrigation_efficiency,
    season_last_day,
    with_stage_days,
)
from croplib_dp.core.kc import adjust_crop, stage_climate  # noqa: E402
from croplib_dp.core.water_balance import CropParams, Soil  # noqa: E402
from croplib_dp.io.dictionaries import TABLES, crop_height, crop_params  # noqa: E402
from croplib_dp.io.farm_db import FarmDb  # noqa: E402
from test_farm_calc import (  # noqa: E402
    FARM,
    FARM2,
    UrlSpy,
    _run_main,
    farm,
    fseason,
    go,
    make_db,
    prot_db,
    world,  # noqa: F401  (fixture)
)

APRIL = dt.date(2026, 4, 1)


def lens(a, b, c, d):
    return dict(l_ini_d=a, l_dev_d=b, l_mid_d=c, l_late_d=d)


def end_of(start: str, total: int) -> str:
    return (dt.date.fromisoformat(start) + dt.timedelta(days=total - 1)).isoformat()


def log(day, liters=30000.0, fid=FARM, i=1):
    return dict(log_id=i, farm_id=fid, date=day, volume_l=liters)


# ── 순수 함수 ───────────────────────────────────────────────────────────────
BASE = CropParams(
    season_start=APRIL, l_ini=20, l_dev=70, l_mid=90, l_late=30, kc_ini=0.5, kc_mid=1.2, kc_end=0.9
)


def test_with_stage_days_overrides_only_when_all_four_are_given():
    got = with_stage_days(BASE, lens(30, 60, 90, 45))
    assert got == replace(BASE, l_ini=30, l_dev=60, l_mid=90, l_late=45)
    assert with_stage_days(BASE, lens(None, None, None, None)) == BASE
    assert with_stage_days(BASE, {}) == BASE
    assert with_stage_days(BASE, lens("30", "60", "90", "45")) == got


@pytest.mark.parametrize(
    "bad",
    [
        lens(30, 60, 90, None),
        lens(None, None, None, 45),
        lens(30, 0, 90, 45),
        lens(30, -5, 90, 45),
        lens(30, 60.5, 90, 45),
        lens(30, "x", 90, 45),
        lens(30, True, 90, 45),
    ],
)
def test_with_stage_days_rejects_partial_or_invalid_values(bad):
    with pytest.raises(ValueError):
        with_stage_days(BASE, bad)


def test_season_last_day_is_start_plus_the_four_stages_minus_one():
    assert season_last_day(BASE) == dt.date(2026, 10, 27)
    assert season_last_day(with_stage_days(BASE, lens(30, 60, 90, 45))) == dt.date(2026, 11, 11)


def test_irrigation_efficiency_reads_the_dictionary_value():
    table = {"drip": "0.95", "sprinkler": 0.75, "furrow": "0.40", "full": 1}
    assert [irrigation_efficiency(c, table) for c in table] == [0.95, 0.75, 0.40, 1.0]


@pytest.mark.parametrize(
    "code,table",
    [
        ("flood", {"drip": 0.95}),
        (None, {"drip": 0.95}),
        ("", {"drip": 0.95}),
        ("drip", {}),
        ("drip", {"drip": None}),
        ("drip", {"drip": 0}),
        ("drip", {"drip": -0.5}),
        ("drip", {"drip": 1.2}),
        ("drip", {"drip": "abc"}),
        ("drip", {"drip": "NaN"}),
    ],
)
def test_irrigation_efficiency_never_falls_back_to_a_default(code, table):
    with pytest.raises(ValueError):
        irrigation_efficiency(code, table)


# ── ① 관수 방식별 효율: 근권 순관수량 · 전망 · 권장 공급량이 같은 값을 쓴다 ───────────────────
EFF = {"drip": 0.95, "sprinkler": 0.75, "furrow": 0.40}


@pytest.mark.parametrize("method", EFF)
def test_open_field_uses_the_method_efficiency_everywhere(world, method):
    ea = EFF[method]
    # 뿌리 깊이를 얕게 해 관수 필요(권장량)가 나오게 한다
    db = make_db(world, [farm(irrigation_method=method, zr_m=0.3)], [fseason()], [log("2026-05-10")])
    res = go(db, world)
    assert res.n_ok == 1 and not res.failed
    row = db.farm_daily(FARM)["2026-05-10"]
    assert row["irr_l"] == 30000.0 and row["irr_net_mm"] == pytest.approx(30.0 * ea)
    s = db.summary[FARM]
    assert s["advice_net_mm"] is not None and s["advice_net_mm"] > 0
    assert s["advice_gross_mm"] == pytest.approx(s["advice_net_mm"] / ea)


def test_todays_record_enters_the_outlook_with_the_method_efficiency(world):
    got = {}
    for method in EFF:
        db = make_db(
            world, [farm(irrigation_method=method, zr_m=0.3)], [fseason()], [log("2026-06-10", 5000.0)]
        )
        go(db, world)
        got[method] = db.outlook[(FARM, "2026-06-10")]["dr_center_mm"]
    # 같은 공급량이면 효율이 낮을수록 뿌리층에 덜 들어가 고갈량이 크다 (5 mm × 효율 차이만큼)
    assert got["sprinkler"] - got["drip"] == pytest.approx(5.0 * (0.95 - 0.75))
    assert got["furrow"] - got["drip"] == pytest.approx(5.0 * (0.95 - 0.40))


@pytest.mark.parametrize("method", EFF)
def test_protected_farm_uses_the_method_efficiency(world, method):
    db = prot_db(world, [log("2026-06-05", 9000.0, i=1), log("2026-06-08", 30000.0, i=2)])
    db.f[0]["irrigation_method"] = method
    go(db, world)
    rows = db.farm_daily(FARM)
    assert rows["2026-06-05"]["irr_net_mm"] == pytest.approx(9.0 * EFF[method])
    assert rows["2026-06-08"]["irr_net_mm"] == pytest.approx(30.0 * EFF[method])


# ── ② 값을 못 읽으면 그 농장만 실패 ───────────────────────────────────────────────
def two_farms(world, bad_farm_kw, bad_season=None, **kw):
    return make_db(
        world,
        [farm(), farm(FARM2, **bad_farm_kw)],
        [fseason(), bad_season or fseason(FARM2, sid="s2")],
        **kw,
    )


def only_the_second_fails(db, world, message):
    res = go(db, world)
    assert res.n_ok == 1 and res.n_failed == 1 and res.failed
    assert db.st[FARM]["result"] == "ok" and db.farm_daily(FARM)
    assert db.st[FARM2]["result"] == "failed" and db.st[FARM2]["error"] == message
    assert not db.farm_daily(FARM2) and FARM2 not in db.summary


@pytest.mark.parametrize("method", ["flood", None, ""])
def test_unknown_or_empty_irrigation_method_fails_only_that_farm(world, method):
    only_the_second_fails(two_farms(world, dict(irrigation_method=method)), world, "관수 방식 값을 읽지 못함")


@pytest.mark.parametrize("value", [None, "0", "1.2", "abc"])
def test_bad_efficiency_in_the_dictionary_fails_only_the_farms_using_it(world, value):
    db = two_farms(world, dict(irrigation_method="furrow"))
    db.methods[2]["efficiency"] = value
    only_the_second_fails(db, world, "관수 방식 값을 읽지 못함")


def test_the_dictionary_is_read_once_per_run(world):
    db = two_farms(world, {})
    go(db, world)
    assert db.calls["irrigation_methods"] == 1


# ── 읽기 실패는 실행 전체가 이유를 남기고 오류로 끝난다 (검토 덧붙임 3) ──────────────────────
def test_unreadable_method_table_stops_the_whole_run_loudly(monkeypatch, tmp_path, world):
    db = two_farms(world, {})
    real = FarmDb("https://x.example", "k", http=lambda url, headers: (404, b'{"message":"no table"}'))
    db.irrigation_methods = real.irrigation_methods
    # 명령줄 실행에서 예외가 그대로 올라온다 → 실행 로그(err.txt · 실행 화면 알림)에 표 이름과 HTTP 번호가 남는다
    with pytest.raises(RuntimeError, match=r"irrigation_methods 읽기 실패: HTTP 404"):
        _run_main(monkeypatch, tmp_path, world, db)
    assert (
        not db.daily and not db.summary and all("done_at" not in s for s in db.st.values())
    )  # 대기 줄은 남는다


def test_empty_method_table_fails_each_farm_with_a_reason(world):
    db = two_farms(world, {})
    db.methods = []
    res = go(db, world)
    assert res.n_failed == 2 and res.failed
    assert {db.st[f]["error"] for f in (FARM, FARM2)} == {"관수 방식 값을 읽지 못함"}


# ── ③ 뿌리 깊이 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("zr,taw,raw", [(None, 130.0, 65.0), (0.5, 65.0, 32.5), ("1.5", 195.0, 97.5)])
def test_root_depth_sets_taw_and_raw(world, zr, taw, raw):
    db = make_db(world, [farm(zr_m=zr)], [fseason()])
    go(db, world)
    rows = db.farm_daily(FARM)
    assert rows and all(
        r["taw_mm"] == pytest.approx(taw) and r["raw_mm"] == pytest.approx(raw) for r in rows.values()
    )


@pytest.mark.parametrize("zr", [0, -1, "abc"])
def test_invalid_root_depth_fails_only_that_farm(world, zr):
    only_the_second_fails(two_farms(world, dict(zr_m=zr)), world, "뿌리 깊이 값이 올바르지 않음")


# ── ④ ⑤ 단계 일수와 작기 끝 ───────────────────────────────────────────────────
def test_stage_days_move_the_kc_curve_and_the_climate_windows(world):
    mine = lens(5, 5, 40, 20)  # 중기 4/11~5/20, 후기 5/21~6/9: 관측이 있는 구간
    base = make_db(world, [farm()], [fseason()])
    go(base, world)
    db = make_db(world, [farm(**mine)], [fseason(end=end_of("2026-04-01", 70))])
    res = go(db, world)
    assert res.n_ok == 1 and not res.failed
    d = world["d"]
    scen = dict(crop_id="apple", season_type_id="apple-main", kc_set_id="apple-k3", season_start="2026-04-01")
    crop0 = with_stage_days(crop_params(d, scen), mine)
    weather = [dict(w, date=dt.date.fromisoformat(w["date"])) for w in world["weather"]]
    crop = adjust_crop(crop0, stage_climate(weather, crop0), crop_height(d, scen))
    rows, old = db.farm_daily(FARM), base.farm_daily(FARM)
    assert list(rows) == list(old)  # 6/9 까지 모두 새 작기(4/1~6/9) 안
    for day, r in rows.items():
        assert r["kc"] == pytest.approx(crop.kc(dt.date.fromisoformat(day)), rel=1e-12), day
    assert rows["2026-04-08"]["kc"] != pytest.approx(old["2026-04-08"]["kc"])  # 발육기가 앞당겨졌다


@pytest.mark.parametrize("bad", [lens(30, 60, 90, None), lens(30, 0, 90, 90), lens(30.5, 60, 90, 30)])
def test_partial_or_invalid_stage_days_fail_only_that_farm(world, bad):
    only_the_second_fails(two_farms(world, bad), world, "단계 일수 값이 올바르지 않음")


@pytest.mark.parametrize(
    "farm_kw,end",
    [
        ({}, "2026-10-28"),  # 사전 일수의 끝은 10/27
        ({}, "2026-10-26"),
        (lens(30, 60, 90, 45), "2026-10-27"),  # 새 일수(225일)인데 끝이 옛 값
    ],
)
def test_season_end_that_does_not_match_the_stage_days_fails_that_farm_and_keeps_old_results(
    world, farm_kw, end
):
    db = two_farms(world, farm_kw, fseason(FARM2, sid="s2", end=end))
    db.daily[(FARM2, "2026-05-01")] = dict(farm_id=FARM2, date="2026-05-01", marker="old")
    db.summary[FARM2] = dict(farm_id=FARM2, run_id=1)
    res = go(db, world)
    assert res.n_ok == 1 and res.n_failed == 1
    assert db.st[FARM2]["error"] == "작기 끝 날짜가 단계 일수와 다름"
    assert db.farm_daily(FARM2) == {"2026-05-01": dict(farm_id=FARM2, date="2026-05-01", marker="old")}
    assert db.summary[FARM2] == dict(farm_id=FARM2, run_id=1)


def test_matching_end_for_new_stage_days_succeeds(world):
    db = make_db(world, [farm(**lens(30, 60, 90, 45))], [fseason(end="2026-11-11")])
    res = go(db, world)
    assert res.n_ok == 1 and db.st[FARM]["result"] == "ok"


def test_season_not_started_yet_is_also_checked(world):
    db = make_db(world, [farm()], [fseason(start="2026-07-01", end="2026-11-30")])
    res = go(db, world)
    assert res.n_failed == 1 and db.st[FARM]["error"] == "작기 끝 날짜가 단계 일수와 다름"


# ── 지난 줄(끝난 지 3일 안): 끝 날짜는 확인하지 않고, 설정은 같은 작형에만 적용 ─────────────────
def past_and_next(world, farm_row, next_season):
    cut = fseason(sid="old", start="2026-04-01", end="2026-06-08")  # 잘린 지난 줄: 끝이 단계 합과 다르다
    db = make_db(world, [farm_row], [cut, next_season])
    res = go(db, world)
    assert res.n_ok == 1 and not res.failed, db.st[FARM].get("error")
    return db.farm_daily(FARM)["2026-06-08"]


def test_past_row_of_the_same_season_type_uses_the_farm_settings(world):
    mine = lens(5, 5, 40, 20)
    nxt = fseason(sid="nxt", start="2027-04-01", end=end_of("2027-04-01", 70))
    row = past_and_next(world, farm(zr_m=0.5, irrigation_method="furrow", **mine), nxt)
    assert row["taw_mm"] == pytest.approx(65.0)
    d = world["d"]
    scen = dict(crop_id="apple", season_type_id="apple-main", kc_set_id="apple-k3", season_start="2026-04-01")
    crop0 = with_stage_days(crop_params(d, scen), mine)
    weather = [dict(w, date=dt.date.fromisoformat(w["date"])) for w in world["weather"]]
    crop = adjust_crop(crop0, stage_climate(weather, crop0), crop_height(d, scen))
    assert row["kc"] == pytest.approx(crop.kc(dt.date(2026, 6, 8)), rel=1e-12)  # 새 일수의 후기 구간


def test_past_row_of_another_season_type_uses_dictionary_values(world):
    """작물을 바꾼 농장: 새 작물의 뿌리 깊이 · 단계 일수는 옛 작물 줄에 섞지 않는다."""
    nxt = fseason(
        sid="nxt", crop="kimchi_cabbage", season="kimchi_cabbage-summer", start="2027-07-01",
        end=end_of("2027-07-01", 70),
    )  # fmt: skip
    changed = farm(crop="kimchi_cabbage", season="kimchi_cabbage-summer", zr_m=0.3, **lens(5, 5, 40, 20))
    got = past_and_next(world, changed, nxt)
    ref = past_and_next(world, farm(), fseason(sid="nxt", start="2027-04-01", end="2027-10-27"))
    assert (got["kc"], got["taw_mm"], got["raw_mm"]) == (ref["kc"], ref["taw_mm"], ref["raw_mm"])


# ── ⑦ 공개 계산은 그대로 ─────────────────────────────────────────────────────
def test_public_calculation_does_not_read_the_member_dictionary_and_keeps_its_efficiency():
    assert "irrigation_methods" not in TABLES
    assert Soil(taw_mm=1.0, raw_mm=0.5).ea == 0.95
    src = Path(F.__file__).parent / "water.py"
    assert "irrigation_method" not in src.read_text(encoding="utf-8")


# ── ⑧ FarmDb 읽기 주소 ────────────────────────────────────────────────────
def test_farm_db_reads_the_new_farm_columns_and_the_method_table():
    spy = UrlSpy()
    db = FarmDb("https://x.example", "k", http=spy)
    db.farms()
    for col in ("irrigation_method", "zr_m", "l_ini_d", "l_dev_d", "l_mid_d", "l_late_d"):
        assert col in spy.urls[0].split("select=")[1].split("&")[0].split(",")
    db.irrigation_methods()
    assert "/rest/v1/irrigation_methods?select=method_code,efficiency&order=method_code" in spy.urls[1]
    assert json.dumps(spy.urls).count("irrigation_methods") == 1
