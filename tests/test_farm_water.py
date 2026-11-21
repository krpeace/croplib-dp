"""농장 물수지의 순수 계산 (water_balance 의 rain_zero · outlook 의 오늘 관수 · farm_water).

이슈 #131(결정 0040). Kc 1, TAW 120, RAW 60 은 test_outlook 과 같은 값이다. 입력은 모두 가짜 값이다.
"""

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from croplib_dp.core.fao56 import wb_step
from croplib_dp.core.outlook import service_outlook, wb_frame
from croplib_dp.core.water_balance import observed_water_balance
from test_outlook import SOIL, TS, _days, _ft, _owb  # noqa: E402

D = "2026-06-10"


# ── service_outlook: 오늘 관수(irr_future) · 오늘 끝 Dr = 0(dr_zero) ───────────────────
def _need_now_owb():
    return _owb(_days("2026-05-25", [5.0] * 16, [0.0] * 16))  # 6/9 끝 Dr ≥ RAW


def test_morning_today_irrigation_enters_the_lead_0_step_and_cancels_the_need_now():
    owb = _need_now_owb()
    run = TS(D) + pd.Timedelta(hours=2)
    base = service_outlook(_ft(D), owb, 120.0, 60.0, 0.95, None, "아침", run)
    assert base["need"]["center"] == 0  # 어제 끝 Dr ≥ RAW → 지금 관수 필요
    irr = {dt.date(2026, 6, 10): 40.0}  # 오늘 기록 40 mm (순량, 효율 곱한 값)
    ol = service_outlook(_ft(D), owb, 120.0, 60.0, 0.95, None, "아침", run, irr_future=irr)
    dr9 = float(owb.set_index("date").loc[TS("2026-06-09"), "Dr"])
    d = ol["days"]
    # 오늘(lead 0) 단계: 예보 비 0 + 오늘 관수 40 mm, ETc 4
    assert d.Dr_center.iloc[0] == pytest.approx(wb_step(dr9, 0.0 + 40.0, 4.0, 120.0, 60.0)[3])
    assert d.Dr_center.iloc[0] < base["days"].Dr_center.iloc[0]
    assert ol["need"]["center"] != 0  # 오늘 물을 줬으니 "지금 필요"가 아니다
    assert ol["start"] == pytest.approx(dr9)  # 출발은 그대로 어제 끝(아침 회차)


def test_morning_without_today_irrigation_is_the_old_behavior():
    owb = _need_now_owb()
    run = TS(D) + pd.Timedelta(hours=2)
    a = service_outlook(_ft(D), owb, 120.0, 60.0, 0.95, None, "아침", run)
    b = service_outlook(_ft(D), owb, 120.0, 60.0, 0.95, None, "아침", run, irr_future={}, dr_zero=None)
    assert list(a["days"].Dr_center) == list(b["days"].Dr_center) and a["need"] == b["need"]
    assert a["start"] == b["start"] and a["advice"] == b["advice"]


def test_evening_today_irrigation_enters_the_start_day():
    owb = _owb(_days("2026-05-31", [5.0] * 10, [0.0] * 10))  # 6/9 끝 Dr 50
    run = TS(D) + pd.Timedelta(hours=17)
    base = service_outlook(_ft(D), owb, 120.0, 60.0, 0.95, None, "저녁", run)
    ol = service_outlook(
        _ft(D), owb, 120.0, 60.0, 0.95, None, "저녁", run, irr_future={dt.date(2026, 6, 10): 30.0}
    )
    assert [p["kind"] for p in ol["pre"]] == ["today"]
    assert ol["pre"][0]["irr"] == pytest.approx(30.0)
    assert ol["start"] == pytest.approx(
        wb_step(50.0, 0.0, 4.0, 120.0, 60.0, 30.0)[3]
    )  # 출발일(오늘) 계산에 관수
    assert ol["start"] < base["start"]
    assert ol["days"].Dr_center.iloc[0] < base["days"].Dr_center.iloc[0]


def test_dr_zero_resets_the_end_of_that_day_to_zero_morning_and_evening():
    # 아침: 오늘(lead 0) 끝 Dr = 0, 다음 날부터 그 값에서 이어간다
    owb = _need_now_owb()
    run = TS(D) + pd.Timedelta(hours=2)
    z = dt.date(2026, 6, 10)
    ol = service_outlook(_ft(D), owb, 120.0, 60.0, 0.95, None, "아침", run, dr_zero=z)
    d = ol["days"]
    assert d.Dr_center.iloc[0] == 0.0 and d.Dr_early.iloc[0] == 0.0 and d.Dr_late.iloc[0] == 0.0
    assert d.Dr_center.iloc[1] == pytest.approx(wb_step(0.0, 10.0 * 0.6, 4.0, 120.0, 60.0)[3])
    assert ol["need"]["center"] != 0  # 어제 Dr 이 커도 오늘 첫 기록으로 끝 Dr 0
    # 저녁: 출발일(오늘) 끝 Dr = 0
    ol2 = service_outlook(
        _ft(D), owb, 120.0, 60.0, 0.95, None, "저녁", TS(D) + pd.Timedelta(hours=17), dr_zero=z
    )
    assert ol2["start"] == 0.0 and ol2["pre"][0]["Dr_center"] == 0.0
    assert ol2["days"].Dr_center.iloc[0] == pytest.approx(wb_step(0.0, 10.0 * 0.6, 4.0, 120.0, 60.0)[3])


def test_wb_frame_still_reads_normal_rows():
    rows = observed_water_balance(_days("2026-06-01", [5.0] * 3, [0.0] * 3), None, SOIL)
    assert list(wb_frame(rows).Dr) == pytest.approx([r["dr_mm"] for r in rows])
    assert isinstance(pd.Timestamp("2026-06-01"), pd.Timestamp)


# ══ core/farm_water.py ════════════════════════════════════════════════════════
from croplib_dp.core.farm_water import (  # noqa: E402
    current_season,
    daily_logs,
    farm_outlook,
    first_record,
    rebuild_ft,
    seasons_to_calc,
    stale_dates,
    water_rows,
)
from croplib_dp.core.water_balance import CropParams  # noqa: E402

d_ = dt.date
AREA = 1000.0  # m²: 1,000 L = 1 mm


def season(sid="s1", start="2026-06-01", end="2026-12-01", **kw):
    return dict(farm_season_id=sid, start_date=d_.fromisoformat(start), end_date=d_.fromisoformat(end), **kw)


def crop_p(start="2026-06-01", kc=(0.5, 1.0, 0.5), lens=(10, 20, 40, 20)):
    return CropParams(d_.fromisoformat(start), *lens, *kc)


def wdays(start, n, eto=4.0, rain=0.0):
    s = d_.fromisoformat(start)
    return [dict(date=s + dt.timedelta(days=i), eto_mm=eto, rain_mm=rain, filled=False) for i in range(n)]


# ── 작기 고르기 ──────────────────────────────────────────────────────────────
def test_current_season_is_the_earliest_one_ending_today_or_later():
    rows = [
        season("a", "2026-01-01", "2026-05-31"),
        season("b", "2026-06-01", "2026-12-01"),
        season("c", "2027-06-01", "2027-12-01"),
    ]
    assert current_season(rows, d_(2026, 6, 10))["farm_season_id"] == "b"
    assert current_season(rows, d_(2026, 12, 1))["farm_season_id"] == "b"  # 끝나는 날 당일 포함
    assert current_season(rows, d_(2026, 12, 2))["farm_season_id"] == "c"  # 시작 전인 가장 가까운 줄
    assert current_season(rows[:1], d_(2026, 12, 2)) is None
    assert current_season([], d_(2026, 6, 10)) is None


def test_seasons_to_calc_adds_the_season_that_ended_in_the_last_three_days():
    rows = [season("a", "2026-01-01", "2026-05-31"), season("b", "2026-06-01", "2026-12-01")]

    def ids(t):
        return [s["farm_season_id"] for s in seasons_to_calc(rows, t)]

    assert ids(d_(2026, 6, 10)) == ["b"]  # a 는 끝난 지 오래라 다시 계산하지 않는다
    assert ids(d_(2026, 6, 2)) == ["a", "b"]  # 끝난 지 이틀: 마지막 날 값을 관측으로 한 번 더
    assert ids(d_(2026, 6, 3)) == ["a", "b"] and ids(d_(2026, 6, 4)) == ["b"]
    nxt = [season("a", "2026-01-01", "2026-05-31"), season("c", "2027-06-01", "2027-12-01")]
    assert [s["farm_season_id"] for s in seasons_to_calc(nxt, d_(2026, 6, 2))] == ["a", "c"]


# ── 관수 기록 ────────────────────────────────────────────────────────────────
def test_daily_logs_sum_per_day_and_keep_only_the_season_window():
    logs = [
        dict(date="2026-06-02", volume_l=1000),
        dict(date="2026-06-02", volume_l="500"),  # 같은 날 여러 줄은 합, 문자열 숫자도 읽는다
        dict(date="2026-05-31", volume_l=300),  # 작기 앞
        dict(date="2026-06-20", volume_l=700),  # 작기 끝 뒤
    ]
    got = daily_logs(logs, d_(2026, 6, 1), d_(2026, 6, 10))
    assert got == {d_(2026, 6, 2): 1500.0}
    assert first_record(got) == d_(2026, 6, 2) and first_record({}) is None


# ── 노지 물수지 ──────────────────────────────────────────────────────────────
THROUGH = d_(2026, 6, 20)


def rows_for(logs=None, protected=False, wd=None, through=THROUGH, s=None, crop=None, **kw):
    return water_rows(
        season=s or season(),
        crop=crop or crop_p(),
        soil=SOIL,
        area_m2=AREA,
        weather=wd if wd is not None else wdays("2026-06-01", 20),
        fill=kw.get("fill", {}),
        through=through,
        logs_l=logs or {},
        protected=protected,
    )


def test_open_field_without_logs_equals_the_plain_water_balance_and_adds_farm_columns():
    wd = wdays("2026-06-01", 20)
    wd[5]["rain_mm"] = 15.0
    rows = rows_for(wd=wd)
    base = observed_water_balance(wd, crop_p(), SOIL)
    assert len(rows) == 20 and rows[0]["farm_season_id"] == "s1"
    for r, b in zip(rows, base, strict=True):
        for k in (
            "date",
            "kc",
            "eto_mm",
            "etc_mm",
            "rain_mm",
            "dr_prev_mm",
            "ks",
            "dr_mm",
            "status",
            "filled",
        ):
            assert r[k] == b[k], k
        assert r["irr_l"] == 0 and r["irr_net_mm"] == 0 and r["taw_mm"] == 120.0 and r["raw_mm"] == 60.0


def test_open_field_irrigation_lowers_dr_from_that_day():
    with_log = rows_for({d_(2026, 6, 10): 20000.0})  # 20 mm 공급 → 순량 19
    without = rows_for()
    day = next(r for r in with_log if r["date"] == d_(2026, 6, 10))
    assert day["irr_l"] == 20000.0 and day["irr_net_mm"] == pytest.approx(19.0)
    assert [r["dr_mm"] for r in with_log[:9]] == [r["dr_mm"] for r in without[:9]]  # 그 전은 같다
    assert day["dr_mm"] < next(r for r in without if r["date"] == d_(2026, 6, 10))["dr_mm"]
    assert sum(r["irr_l"] for r in with_log) == 20000.0


# ── ① 해를 넘는 작기: Dr 가 1/1 에 끊기지 않는다 ───────────────────────────────────
def test_season_crossing_the_new_year_keeps_dr_continuous():
    s = season("x", "2026-12-01", "2027-06-30")
    crop = crop_p("2026-12-01", lens=(30, 60, 90, 30))
    wd = wdays("2026-12-01", 72)  # ~ 2027-02-10
    rows = rows_for(wd=wd, through=d_(2027, 2, 10), s=s, crop=crop)
    by = {r["date"]: r for r in rows}
    assert rows[0]["date"] == d_(2026, 12, 1) and rows[-1]["date"] == d_(2027, 2, 10) and len(rows) == 72
    assert by[d_(2027, 1, 1)]["dr_prev_mm"] == by[d_(2026, 12, 31)]["dr_mm"] > 0  # 해가 바뀌어도 이어받음
    base = observed_water_balance(wd, crop, SOIL)
    assert [r["dr_mm"] for r in rows] == [b["dr_mm"] for b in base]


# ── ⑦ 윤년 ──────────────────────────────────────────────────────────────────
def test_leap_day_is_a_normal_day_in_a_season_through_2028_02_29():
    s = season("y", "2028-02-20", "2028-06-30")
    rows = rows_for(
        {d_(2028, 2, 29): 5000.0},
        wd=wdays("2028-02-20", 15),
        through=d_(2028, 3, 5),
        s=s,
        crop=crop_p("2028-02-20"),
    )
    dates = [r["date"] for r in rows]
    assert d_(2028, 2, 29) in dates and dates[dates.index(d_(2028, 2, 29)) + 1] == d_(2028, 3, 1)
    assert len(rows) == 15 and next(r for r in rows if r["date"] == d_(2028, 2, 29))["irr_l"] == 5000.0
    # 2월 28일 → 3월 1일(평년)도 끊기지 않는다
    r2 = rows_for(
        wd=wdays("2027-02-25", 8),
        through=d_(2027, 3, 4),
        s=season("z", "2027-02-25", "2027-06-30"),
        crop=crop_p("2027-02-25"),
    )
    assert [r["date"] for r in r2][3:5] == [d_(2027, 2, 28), d_(2027, 3, 1)]


# ── ② 작기 밖 기록은 넣지 않는다 ────────────────────────────────────────────────────
def test_logs_outside_the_season_are_ignored_and_irr_l_is_zero():
    s = season("w", "2026-06-10", "2026-12-01")
    logs = daily_logs(
        [dict(date="2026-06-05", volume_l=9000), dict(date="2026-06-12", volume_l=1000)],
        s["start_date"],
        s["end_date"],
    )
    rows = rows_for(logs, wd=wdays("2026-06-10", 8), through=d_(2026, 6, 17), s=s, crop=crop_p("2026-06-10"))
    assert sum(r["irr_l"] for r in rows) == 1000.0 and all(r["date"] >= s["start_date"] for r in rows)


# ── ③ ④ ⑤ 시설: 기록 0건 → 모두 etc_only, 첫 기록 날부터 판정 ───────────────────────────
DR_COLS = ("dr_prev_mm", "ks", "etc_adj_mm", "dp_mm", "dr_mm")


def test_protected_without_logs_is_all_etc_only_with_empty_dr():
    rows = rows_for(protected=True, wd=wdays("2026-06-01", 20, rain=30.0))
    assert len(rows) == 20 and {r["status"] for r in rows} == {"etc_only"}
    assert all(r[c] is None for r in rows for c in DR_COLS) and all(r["irr_l"] == 0 for r in rows)
    assert all(r["rain_mm"] == 30.0 for r in rows)  # 기록 열은 바깥 비 그대로
    assert all(r["etc_mm"] == pytest.approx(r["kc"] * r["eto_mm"]) for r in rows)


def test_protected_first_record_day_ends_with_dr_zero_then_normal_balance_with_zero_rain():
    wd = wdays("2026-06-01", 20, rain=30.0)  # 날마다 바깥 비 30 mm
    rows = rows_for({d_(2026, 6, 8): 10000.0, d_(2026, 6, 12): 8000.0}, protected=True, wd=wd)
    by = {r["date"]: r for r in rows}
    assert all(by[d_(2026, 6, i)]["status"] == "etc_only" for i in range(1, 8))
    assert all(by[d_(2026, 6, i)][c] is None for i in range(1, 8) for c in DR_COLS)
    f = by[d_(2026, 6, 8)]
    assert f["dr_mm"] == 0.0 and f["status"] == "wet" and f["irr_l"] == 10000.0  # 첫 기록 날 끝 Dr = 0
    assert f["irr_net_mm"] == pytest.approx(10.0 * 0.95)
    nxt = by[d_(2026, 6, 9)]
    exp = wb_step(0.0, 0.0, nxt["etc_mm"], 120.0, 60.0, 0.0)  # 비 0 (바깥 비 30 mm 는 안 들어감)
    assert nxt["dr_prev_mm"] == 0.0 and nxt["dr_mm"] == pytest.approx(exp[3]) and nxt["dr_mm"] > 0
    assert nxt["rain_mm"] == 30.0
    twelfth = by[d_(2026, 6, 12)]  # 둘째 기록: 그날 관수 mm 를 뺀다
    prev = by[d_(2026, 6, 11)]["dr_mm"]
    assert twelfth["dr_mm"] == pytest.approx(
        wb_step(prev, 0.0, twelfth["etc_mm"], 120.0, 60.0, 8.0 * 0.95)[3]
    )
    assert all(r["status"] != "etc_only" for r in rows if r["date"] >= d_(2026, 6, 8))


def test_deleting_the_first_record_moves_the_first_record_to_the_next_one_or_back_to_etc_only():
    both = {d_(2026, 6, 8): 10000.0, d_(2026, 6, 12): 8000.0}
    only_second = {d_(2026, 6, 12): 8000.0}
    assert first_record(both) == d_(2026, 6, 8) and first_record(only_second) == d_(2026, 6, 12)
    by = {r["date"]: r for r in rows_for(only_second, protected=True)}
    assert by[d_(2026, 6, 11)]["status"] == "etc_only" and by[d_(2026, 6, 12)]["dr_mm"] == 0.0
    assert all(r["status"] == "etc_only" for r in rows_for({}, protected=True))


def test_protected_dormant_and_filled_follow_the_open_field_rules():
    s = season("p", "2026-06-01", "2026-06-12")
    rows = rows_for(
        protected=True,
        wd=wdays("2026-06-01", 12),
        through=d_(2026, 6, 15),
        s=s,
        crop=crop_p(lens=(3, 3, 3, 3)),
    )
    st = {r["date"]: r["status"] for r in rows}
    assert st[d_(2026, 6, 12)] == "etc_only" and st[d_(2026, 6, 13)] == "dormant" and len(rows) == 15
    assert [r["eto_src"] for r in rows[:12]] == ["관측"] * 12 and all(r["filled"] for r in rows[12:])


# ── 줄 정리 ──────────────────────────────────────────────────────────────────
def test_stale_dates_are_outside_every_season_or_unrecomputed_inside_the_current_one():
    seasons = [season("old", "2026-01-01", "2026-05-31"), season("cur", "2026-06-10", "2026-12-01")]
    existing = [
        d_(2026, 3, 1),
        d_(2026, 6, 1),
        d_(2026, 6, 5),
        d_(2026, 6, 10),
        d_(2026, 6, 11),
        d_(2026, 6, 30),
    ]
    new = {d_(2026, 6, 10), d_(2026, 6, 11)}
    # 6/1~6/5 는 어느 작기에도 안 든다(시작일을 늦췄다) → 지움
    # 6/30 은 지금 작기 안인데 이번에 안 만들었다 → 지움
    assert stale_dates(existing, seasons, new, d_(2026, 6, 10), d_(2026, 12, 1)) == [
        d_(2026, 6, 1),
        d_(2026, 6, 5),
        d_(2026, 6, 30),
    ]
    assert stale_dates(existing, [], set(), None, None) == sorted(existing)  # 작기 줄이 없으면 전부


# ── 예보표 되살리기 ───────────────────────────────────────────────────────────
def run_rows():
    return [
        dict(
            run_id=1, run_at="2026-06-09T17:00:00+00:00", run_name="아침", src_run_at=None
        ),  # 6/10 02:00 KST
        dict(
            run_id=2,
            run_at="2026-06-10T08:00:00+00:00",
            run_name="저녁",
            src_run_at="2026-06-10T05:00:00+00:00",
        ),
    ]


def outlook_src():
    rows = []
    for rid, leads in ((1, range(0, 4)), (2, range(1, 5))):
        for k in leads:
            rows.append(
                dict(
                    run_id=rid,
                    scenario_id="z-scn" if rid == 1 else "a-scn",
                    target_date=(d_(2026, 6, 10) + dt.timedelta(days=k)).isoformat(),
                    lead_day=k,
                    eto_mm=str(4.0 + k),  # DB 에서 문자열로 와도 읽는다
                    rain_fcst_mm=10.0 if k == 1 else 0.0,
                    pop_pct=60.0 if k == 1 else 10.0,
                    rain_expected_mm=6.0 if k == 1 else 0.0,
                )
            )
        rows.append(dict(rows[-1], scenario_id="zz-other", eto_mm=99.0))  # 다른 시나리오 줄은 쓰지 않는다
    return rows


def test_rebuild_ft_uses_one_scenario_per_run_and_the_farm_kc():
    def kc(t):
        return 0.5 + 0.1 * (pd.Timestamp(t).day - 10)

    ft = rebuild_ft(run_rows(), outlook_src(), kc)
    morning = ft[ft.run_name == "아침"].sort_values("lead_day")
    assert list(morning.lead_day) == [0, 1, 2, 3] and morning.run.iloc[0] == TS("2026-06-10 02:00")  # KST 로
    assert list(morning.ETo_main) == [4.0, 5.0, 6.0, 7.0]  # 문자열 → 수, 가장 앞 시나리오(z-scn < zz-other)
    expect = [kc(t) * e for t, e in zip(morning.target, morning.ETo_main, strict=True)]
    assert list(morning.ETc_main) == pytest.approx(expect)
    assert list(morning.Kc) == pytest.approx([kc(t) for t in morning.target])
    assert morning.pop_max.iloc[1] == pytest.approx(0.6) and morning.rain_exp.iloc[1] == 6.0
    ev = ft[ft.run_name == "저녁"]
    assert list(ev.lead_day) == [1, 2, 3, 4]
    # 저녁 회차는 직전 발표(05시, 아침 종류)로 대신한 백업: 오차표 종류 · 선행일을 대체 발표 기준으로
    assert bool(ev.backup.iloc[0]) and ev.err_name.iloc[0] == "아침"
    assert list(ev.err_lead) == [(t.normalize() - TS("2026-06-10")).days for t in ev.target]
    assert not bool(morning.backup.iloc[0]) and morning.err_name.iloc[0] == "아침"
    assert list(morning.err_lead) == [0, 1, 2, 3]


# ── 농장 3일 전망 ─────────────────────────────────────────────────────────────
from croplib_dp.core.outlook import outlook_rows, outlook_summary  # noqa: E402

RUN_D = d_(2026, 6, 10)
MORNING = TS("2026-06-10 02:00")
EVENING = TS("2026-06-10 17:00")


def prev_rows(logs=None, protected=False):
    """6/1 시작 작기를 6/9(어제)까지"""
    return rows_for(logs, protected=protected, wd=wdays("2026-06-01", 9, eto=5.0), through=d_(2026, 6, 9))


def outlook(rows, logs=None, protected=False, run=MORNING, ft=None):
    logs = logs or {}
    return farm_outlook(
        ft=ft if ft is not None else _ft(RUN_D.isoformat()),
        rows=rows,
        first=first_record(logs) if protected else None,
        logs_l=logs,
        area_m2=AREA,
        soil=SOIL,
        crop=crop_p(),
        err=None,
        run_name="아침" if run == MORNING else "저녁",
        run=run,
        protected=protected,
    )


def test_open_field_outlook_equals_the_public_service_outlook_for_the_same_inputs():
    rows = prev_rows()
    got_rows, got_sum = outlook(rows)
    owb = wb_frame(
        observed_water_balance(wdays("2026-06-01", 9, eto=5.0), crop_p(), SOIL, end=d_(2026, 6, 9))
    )
    ol = service_outlook(_ft("2026-06-10"), owb, 120.0, 60.0, 0.95, None, "아침", MORNING)
    assert got_rows == outlook_rows(ol, 60.0)
    assert got_sum == outlook_summary(ol, 60.0, crop_p().kc(d_(2026, 6, 9)), crop_p().kc(RUN_D))


@pytest.mark.parametrize("run", [MORNING, EVENING])
def test_open_field_today_record_is_in_the_outlook_for_both_runs(run):
    rows = prev_rows()
    plain_rows, plain_sum = outlook(rows, run=run)
    with_rows, with_sum = outlook(rows, logs={RUN_D: 30000.0}, run=run)  # 오늘 30 mm 공급
    assert with_rows[0]["dr_center_mm"] < plain_rows[0]["dr_center_mm"] or run == EVENING
    first_after = 0 if run == MORNING else 1
    assert with_rows[first_after]["dr_center_mm"] < plain_rows[first_after]["dr_center_mm"]
    if run == EVENING:  # 저녁 회차는 출발일(오늘) 계산에 관수가 들어가 요약 출발 Dr 가 줄어든다
        assert with_sum["dr_start_mm"] < plain_sum["dr_start_mm"]


def test_open_field_today_record_cancels_need_now_in_the_morning():
    rows = rows_for(wd=wdays("2026-06-01", 9, eto=20.0), through=d_(2026, 6, 9))  # 어제 끝 Dr ≥ RAW
    _, plain = outlook(rows)
    assert plain["need_center_in"] == 0
    _, done = outlook(rows, logs={RUN_D: 80000.0})
    assert (
        done["need_center_in"] != 0 and done["dr_start_mm"] == plain["dr_start_mm"]
    )  # 출발은 어제 끝 그대로


# ── 시설 ─────────────────────────────────────────────────────────────────────
def test_protected_outlook_with_no_record_is_etc_only_with_empty_dr_and_a_three_day_sum():
    rows = prev_rows(protected=True)
    out, summ = outlook(rows, protected=True)
    assert [o["status"] for o in out] == ["etc_only"] * 4
    assert all(o[c] is None for o in out for c in ("dr_center_mm", "dr_early_mm", "dr_late_mm"))
    assert summ["status_start"] == "etc_only" and summ["etc_sum_mm"] == pytest.approx(12.0)
    assert all(summ[c] is None for c in ("dr_start_mm", "need_center_in", "advice_net_mm"))


def test_protected_outlook_with_a_record_uses_the_dr_path_with_zero_rain():
    logs = {d_(2026, 6, 5): 10000.0}
    rows = prev_rows(logs, protected=True)
    out, summ = outlook(rows, logs, protected=True)
    assert all(o["status"] != "etc_only" for o in out)
    dr9 = [r for r in rows if r["date"] == d_(2026, 6, 9)][0]["dr_mm"]
    # 예보 비(6/11 10 mm · 기대 6 mm)는 기록 열에만 남고 Dr 에는 들어가지 않는다
    assert out[1]["rain_fcst_mm"] == 10.0 and out[1]["rain_expected_mm"] == 6.0
    assert out[0]["dr_center_mm"] == pytest.approx(wb_step(dr9, 0.0, 4.0, 120.0, 60.0)[3])
    assert out[1]["dr_center_mm"] == pytest.approx(wb_step(out[0]["dr_center_mm"], 0.0, 4.0, 120.0, 60.0)[3])
    assert summ["status_start"] != "etc_only" and summ["dr_start_mm"] == pytest.approx(dr9)


@pytest.mark.parametrize("run", [MORNING, EVENING])
def test_protected_first_record_today_ends_the_day_at_dr_zero(run):
    logs = {RUN_D: 10000.0}
    rows = prev_rows(logs, protected=True)  # 어제까지 기록 0건 → 전부 etc_only
    assert {r["status"] for r in rows} == {"etc_only"}
    out, summ = outlook(rows, logs, protected=True, run=run)
    today = [o for o in out if o["target_date"] == RUN_D]
    if run == MORNING:  # 오늘(lead 0) 끝 Dr = 0, 내일부터 Dr 경로
        assert today[0]["dr_center_mm"] == 0.0 and today[0]["dr_early_mm"] == 0.0
        nxt = out[1]
        assert nxt["dr_center_mm"] == pytest.approx(wb_step(0.0, 0.0, 4.0, 120.0, 60.0)[3])
    else:
        assert today == [] and out[0]["dr_center_mm"] == pytest.approx(wb_step(0.0, 0.0, 4.0, 120.0, 60.0)[3])
    assert all(o["status"] != "etc_only" for o in out)
    assert summ["start_date"] == RUN_D and summ["dr_start_mm"] == 0.0 and summ["status_start"] == "normal"
    assert summ["need_center_in"] != 0


def test_protected_first_record_after_the_run_date_keeps_the_etc_only_outlook():
    logs = {d_(2026, 6, 11): 10000.0}  # 회차 날짜(6/10) 뒤 기록: 다음 계산이 반영
    rows = prev_rows(logs, protected=True)
    out, summ = outlook(rows, logs, protected=True)
    assert [o["status"] for o in out] == ["etc_only"] * 4 and summ["status_start"] == "etc_only"


def test_season_that_starts_today_starts_the_outlook_from_dr_zero():
    rows = []  # 시작일 = 회차 날짜라 어제까지 물수지 줄이 없다
    out, summ = outlook(rows)
    assert len(out) == 4 and summ["dr_start_mm"] == 0.0
    assert out[0]["dr_center_mm"] == pytest.approx(wb_step(0.0, 0.0, 4.0, 120.0, 60.0)[3])
