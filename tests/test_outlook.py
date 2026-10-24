"""앞으로 3일 전망 (core/outlook.py), 예보로 채운 관측 물수지, Kc 기후보정 (core/kc.py).

cropwater `tests/test_wb.py`·`tests/test_ops.py`(feat/cycle02-forecast)의 서비스 전망 테스트를 가져왔다.
Kc 1, TAW 120, RAW 60 (cropwater 테스트와 같은 값). 입력은 모두 가짜 값이다.
"""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from croplib_dp.core.fao56 import kc_climate_adjust, wb_step, wind_2m
from croplib_dp.core.kc import DEFAULT_RHMIN, DEFAULT_U2, adjust_crop, stage_climate, stage_windows
from croplib_dp.core.outlook import (
    ERR_DEFAULT_REL,
    err_lookup,
    fill_from_forecast,
    last_observed,
    outlook_rows,
    pooled_errors,
    service_outlook,
    wb_frame,
)
from croplib_dp.core.water_balance import CropParams, Soil, observed_water_balance

TS = pd.Timestamp
SOIL = Soil(taw_mm=120.0, raw_mm=60.0, dr0_mm=0.0, ea=0.95)


def _days(start, eto, rain):
    s = TS(start).date()
    return [
        dict(date=s + dt.timedelta(days=i), eto_mm=e, rain_mm=p, filled=e is None)
        for i, (e, p) in enumerate(zip(eto, rain, strict=True))
    ]


def _owb(days, **kw):
    return wb_frame(observed_water_balance(days, None, SOIL, **kw))


def _ft(D, etc=4.0):
    """cropwater test_wb._ft: 아침 D+0~D+3, 저녁 D+1~D+4. D+1에 비 10 mm(강수확률 60%)"""
    rows = []
    for rn, h, leads in (("아침", 2, range(0, 4)), ("저녁", 17, range(1, 5))):
        run = TS(D) + pd.Timedelta(hours=h)
        for k in leads:
            rain = 10.0 if k == 1 else 0.0
            rows.append(
                dict(
                    run_name=rn,
                    run=run,
                    lead_day=k,
                    target=TS(D) + pd.Timedelta(days=k),
                    Kc=1.0,
                    ETc_main=etc,
                    ETo_main=etc,
                    rain=rain,
                    rain_exp=0.6 * rain,
                    pop_max=0.6 if k == 1 else 0.1,
                )
            )
    return pd.DataFrame(rows)


def _path(dr0, etc, rain):
    out, dr = [], dr0
    for e, p in zip(etc, rain, strict=True):
        dr = wb_step(dr, p, e, 120.0, 60.0)[3]
        out.append(dr)
    return out


# ── 오차표 ──────────────────────────────────────────────────────────────
def _err_rows():
    rows = []
    for year, bias in ((2025, 0.7), (2026, 0.3)):
        for k in range(4):
            rows.append(dict(stn="101", year=year, kind="day", run_name="아침", lead_day=k, month=0, n=100,
                             rmse=0.5 + 0.1 * k, mbe=bias, obs_mean=4.0))  # fmt: skip
            rows.append(dict(stn="101", year=year, kind="day", run_name="아침", lead_day=k, month=5, n=20,
                             rmse=0.4, mbe=bias, obs_mean=5.0))  # fmt: skip
            rows.append(dict(stn="101", year=year, kind="day", run_name="아침", lead_day=k, month=6, n=5,
                             rmse=0.9, mbe=bias, obs_mean=5.0))  # fmt: skip
    return pd.DataFrame(rows)


def test_error_table_pool_and_lookup():
    pool = pooled_errors(_err_rows(), "101")
    one = pool[(pool.kind == "day") & (pool.lead_day == 1) & (pool.month == 0)].iloc[0]
    assert one.mbe == pytest.approx(0.5) and one.years == "2025,2026" and one.n == 200
    other = pooled_errors(_err_rows(), "101", exclude_year=2026)
    assert other[(other.lead_day == 1) & (other.month == 0)].mbe.iloc[0] == pytest.approx(0.7)
    rmse, rel, n, src = err_lookup(pool, "day", "아침", 1, 5)
    assert src == "월별" and n == 40 and rel == pytest.approx(0.4 / 5.0)
    assert err_lookup(pool, "day", "아침", 1, 6)[3] == "전체 월"  # 6월 칸 표본 10 < 15 → 전체 월
    assert err_lookup(pool, "day", "아침", 1, 9)[3] == "전체 월"  # 9월 칸 없음
    assert err_lookup(pool, "day", "저녁", 1, 5) == (
        pytest.approx(np.nan, nan_ok=True),
        ERR_DEFAULT_REL,
        0,
        "기본값",
    )
    assert err_lookup(None, "day", "아침", 1, 5)[1] == ERR_DEFAULT_REL
    assert (
        pooled_errors(_err_rows(), "999").stn_src.iloc[0].startswith("전 지점")
    )  # 지점 행이 없으면 다른 지점 오차


# ── 예보로 채운 관측 물수지 ─────────────────────────────────────────────
def test_observed_wb_fill_from_forecast_and_end():
    days = _days("2026-05-01", [5.0, 5.0, None, 5.0, 5.0, 5.0], [0, 0, 0, 0, 50, 0])
    days = [
        d for d in days if d["date"] != dt.date(2026, 5, 4)
    ]  # 5/4 행 자체가 없음 → 관측 물수지가 날을 채우지 않음
    days.insert(3, dict(date=dt.date(2026, 5, 4), eto_mm=None, rain_mm=None, filled=True))
    fill = {dt.date(2026, 5, 3): (4.0, 0.0), dt.date(2026, 5, 4): (3.0, 2.0), dt.date(2026, 5, 7): (6.0, 1.0)}
    w = observed_water_balance(days, None, SOIL, forecast_fill=fill, end=dt.date(2026, 5, 7))
    by = {r["date"].day: r for r in w}
    assert (
        by[3]["eto_mm"] == 4.0 and by[3]["eto_src"] == "예보" and by[3]["rain_mm"] == 0
    )  # 행은 있음 → 관측 강수
    assert (
        by[4]["rain_mm"] == 2.0 and by[4]["filled"] and not by[4]["obs_row"]
    )  # 행이 없는 날은 강수도 예보로
    assert not by[2]["filled"] and by[3]["filled"]
    assert by[5]["dr_mm"] == 0.0 and not by[5]["filled"]  # 큰 비로 Dr 0 → 영향 끝
    assert (
        by[7]["eto_src"] == "예보" and by[7]["rain_mm"] == 1.0 and by[7]["filled"]
    )  # 관측보다 뒤 → 예보로 채움
    assert len(w) == 7


def test_fill_from_forecast_uses_morning_d0():
    f = fill_from_forecast(_ft("2026-06-10"))
    assert f == {dt.date(2026, 6, 10): (4.0, 0.0)}


# ── 서비스 전망 ─────────────────────────────────────────────────────────
def test_service_outlook_need_and_advice():
    D = "2026-06-10"
    owb = _owb(_days("2026-05-25", [5.0] * 16, [0.0] * 16))  # 6/9 끝 Dr ≥ RAW
    ol = service_outlook(_ft(D), owb, 120.0, 60.0, 0.95, None, "아침", TS(D) + pd.Timedelta(hours=2))
    start = float(owb.set_index("date").loc[TS("2026-06-09"), "Dr"])
    assert start > 60 and ol["start"] == pytest.approx(start) and ol["need"]["center"] == 0
    assert ol["advice"][0] == pytest.approx(start) and ol["advice"][1] == pytest.approx(start / 0.95)
    owb2 = _owb(_days("2026-05-31", [5.0] * 10, [0.0] * 10))  # 6/9 끝 Dr 50 → 54, 52(비 6), 56, 60
    ol2 = service_outlook(_ft(D), owb2, 120.0, 60.0, 0.95, None, "저녁", TS(D) + pd.Timedelta(hours=17))
    d = ol2["days"]
    assert (
        ol2["need"]["center"] == 3 and d.Dr_center.iloc[2] == pytest.approx(60.0) and ol2["need"]["early"] < 3
    )
    assert ol2["cum3"][0] == pytest.approx(12.0) and len(ol2["pre"]) == 1 and ol2["pre"][0]["kind"] == "today"
    rel = ERR_DEFAULT_REL  # 오차표가 없으면 0.25
    dr0 = 50.0
    assert list(d.Dr_early) == pytest.approx(_path(dr0, [4.0 * (1 + rel)] * 5, [0.0] * 5)[1:])
    assert list(d.Dr_late) == pytest.approx(_path(dr0, [4.0 * (1 - rel)] * 5, [0.0, 10.0, 0.0, 0.0, 0.0])[1:])
    assert list(d.main) == [True, True, True, False]


def test_service_outlook_backup_uses_source_error():
    D = "2026-06-10"
    ft = _ft(D)
    m = ft.run_name == "아침"
    ft["err_name"], ft["err_lead"], ft["backup"] = "아침", ft["lead_day"], False
    ft.loc[m, "err_name"], ft.loc[m, "err_lead"], ft.loc[m, "backup"] = (
        "저녁",
        ft.loc[m, "lead_day"] + 1,
        True,
    )
    err = pd.DataFrame(
        [
            dict(
                kind="day",
                run_name=rn,
                lead_day=k,
                month=0,
                n=100,
                rmse=0.1 * (k + 1) + (0.05 if rn == "저녁" else 0),
                mbe=0.0,
                obs_mean=4.0,
            )  # fmt: skip
            for rn in ("아침", "저녁")
            for k in range(5)
        ]
        + [
            dict(kind="cum3", run_name="저녁", lead_day=1, month=0, n=100, rmse=1.2, mbe=0.0, obs_mean=12.0),
            dict(kind="cum3", run_name="아침", lead_day=0, month=0, n=100, rmse=2.4, mbe=0.0, obs_mean=12.0),
        ]
    )
    owb = _owb(_days("2026-06-01", [5.0] * 9, [0.0] * 9))
    ol = service_outlook(ft, owb, 120.0, 60.0, 0.95, err, "아침", TS(D) + pd.Timedelta(hours=2))
    assert list(ol["days"].rel.round(4)) == [
        round((0.1 * (k + 2) + 0.05) / 4, 4) for k in range(4)
    ]  # 저녁 D+1~D+4
    assert ol["cum3"][3] == pytest.approx(0.1)  # 3일 누적도 대체 발표(저녁 D+1) 칸


def test_service_outlook_obs_lag():
    """전날 관측이 없으면 마지막 관측일에서 출발해 전날을 아침 D+0 예보로 먼저 진행(범위 포함)"""
    ft = pd.concat([_ft("2026-06-09"), _ft("2026-06-10")], ignore_index=True)
    ft.loc[(ft.run == TS("2026-06-09 02:00")) & (ft.lead_day == 0), ["rain", "rain_exp"]] = [
        8.0,
        4.8,
    ]  # 6/9 비 예보
    days = _days("2026-06-01", [5.0] * 8, [0.0] * 8)  # 6/8까지, 6/9 관측 아직 없음
    owb = _owb(days, forecast_fill=fill_from_forecast(ft), end=dt.date(2026, 6, 9))
    assert last_observed(owb, TS("2026-06-10")) == TS("2026-06-08")
    ol = service_outlook(
        ft, owb, 120.0, 60.0, 0.95, None, "아침", TS("2026-06-10 02:00"), obs_last=TS("2026-06-08")
    )
    assert [p["kind"] for p in ol["pre"]] == ["lag"] and ol["lag_days"] == [TS("2026-06-09")]
    dr8 = float(owb.set_index("date").loc[TS("2026-06-08"), "Dr"])
    assert ol["start"] == pytest.approx(wb_step(dr8, 4.8, 4.0, 120.0, 60.0)[3])  # 중심 = 기대 강수
    assert ol["pre"][0]["Dr_early"] == pytest.approx(
        wb_step(dr8, 0.0, 4.0 * 1.25, 120.0, 60.0)[3]
    )  # 빠르면 = 비 없음
    # 전날 관수 기록과 관측 강수(ASOS 행은 있고 ETo만 빠짐)는 세 경로 모두에 들어감
    days2 = _days("2026-06-01", [5.0] * 8 + [None], [0.0] * 8 + [3.0])
    w2 = observed_water_balance(
        days2, None, SOIL, irrigation={dt.date(2026, 6, 9): 10 / 0.95}, forecast_fill=fill_from_forecast(ft)
    )
    owb2 = wb_frame(w2)
    ol2 = service_outlook(
        ft,
        owb2,
        120.0,
        60.0,
        0.95,
        None,
        "아침",
        TS("2026-06-10 02:00"),
        obs_last=last_observed(owb2, TS("2026-06-10")),
    )
    p = ol2["pre"][0]
    assert p["rain_known"] and p["rain"] == 3.0 and p["irr"] == pytest.approx(10.0)
    assert p["Dr_early"] == pytest.approx(wb_step(dr8, 3.0, 5.0, 120.0, 60.0, 10.0)[3])


def test_outlook_rows_contract_columns_and_no_wet():
    D = "2026-06-10"
    owb = _owb(
        _days("2026-06-05", [5.0, 5.0, 5.0, 5.0, 0.5], [0.0, 0.0, 0.0, 0.0, 200.0])
    )  # 6/9 큰 비 → Dr 0
    ol = service_outlook(_ft(D, etc=0.0), owb, 120.0, 60.0, 0.95, None, "아침", TS(D) + pd.Timedelta(hours=2))
    rows = outlook_rows(ol, 60.0)
    assert [r["lead_day"] for r in rows] == [0, 1, 2, 3]
    assert set(rows[0]) >= {
        "target_date", "lead_day", "eto_mm", "etc_mm", "etc_err_mm", "rain_fcst_mm", "pop_pct",
        "rain_expected_mm", "dr_center_mm", "dr_early_mm", "dr_late_mm", "status", "calc_version",
    }  # fmt: skip
    assert all(
        r["dr_center_mm"] == 0.0 and r["status"] == "normal" for r in rows
    )  # 예보 날은 과습 판정 안 함
    assert rows[1]["pop_pct"] == pytest.approx(60.0) and rows[1]["rain_fcst_mm"] == 10.0
    assert rows[0]["target_date"] == dt.date(2026, 6, 10)


# ── Kc 기후보정 ─────────────────────────────────────────────────────────
CROP = CropParams(dt.date(2026, 4, 1), 20, 70, 90, 30, 0.50, 1.20, 0.95)


def test_stage_windows():
    (ms, me), (ls, le) = stage_windows(CROP)
    assert (ms, me, ls, le) == (
        dt.date(2026, 6, 30),
        dt.date(2026, 9, 27),
        dt.date(2026, 9, 28),
        dt.date(2026, 10, 27),
    )


def test_stage_climate_and_adjust():
    days = pd.date_range("2026-06-25", "2026-10-10")
    weather = [
        dict(date=d.date(), u2_ms=wind_2m(2.0, 10.0), rh_min_pct=60.0 if d >= TS("2026-09-28") else 40.0)
        for d in days
    ]
    c = stage_climate(weather, CROP)
    assert c.u2_mid == pytest.approx(wind_2m(2.0, 10.0)) and c.n_mid == 90
    assert c.rh_mid == pytest.approx(40.0)
    assert c.n_end == 13 and c.rh_end == pytest.approx(60.0)  # 9/28~10/10 13일 ≥ 10
    a = adjust_crop(CROP, c, 3.2)
    assert a.kc_ini == 0.50
    assert a.kc_mid == pytest.approx(kc_climate_adjust(1.20, c.u2_mid, c.rh_mid, 3.2))
    assert a.kc_end == pytest.approx(kc_climate_adjust(0.95, c.u2_end, 60.0, 3.2, is_end=True))


def test_stage_climate_defaults_when_short():
    weather = [
        dict(date=dt.date(2026, 7, 1) + dt.timedelta(days=i), u2_ms=3.0, rh_min_pct=30.0) for i in range(9)
    ]
    c = stage_climate(weather, CROP)
    assert c.u2_mid is None and c.rh_mid is None and c.n_mid == 9  # 10일 미만 → 기본값
    a = adjust_crop(CROP, c, 3.2)
    assert a.kc_mid == pytest.approx(kc_climate_adjust(1.20, DEFAULT_U2, DEFAULT_RHMIN, 3.2))
    assert adjust_crop(CROP, c, None) == CROP  # 작물 높이가 없으면 보정하지 않음
