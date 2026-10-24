"""관측 물수지와 토양수분 상태 판정 (core/water_balance.py).

cropwater `tests/test_wb.py`(feat/cycle02-forecast)의 관측 물수지·상태 판정 테스트를 가져왔다.
상태는 이름 대신 데이터 계약 v1 코드로 낸다:
  need(관수 필요) · caution(주의) · normal(보통) · wet(과습) · dormant(휴면기).
입력은 모두 가짜 값이다.
"""

import datetime as dt

import pytest

from croplib_dp.core import CALC_VERSION
from croplib_dp.core.fao56 import wb_step
from croplib_dp.core.water_balance import STATUSES, CropParams, Soil, observed_water_balance, soil_status

SOIL = Soil(taw_mm=120.0, raw_mm=60.0, dr0_mm=0.0, ea=0.95)
D0 = dt.date(2026, 5, 1)


def _days(eto, rain, filled=None):
    filled = filled or [False] * len(eto)
    return [
        dict(date=D0 + dt.timedelta(days=i), eto_mm=e, rain_mm=p, filled=f)
        for i, (e, p, f) in enumerate(zip(eto, rain, filled, strict=True))
    ]


# ── 상태 판정 ─────────────────────────────────────────────────────────
def test_status_codes_match_contract():
    assert STATUSES == ("need", "caution", "normal", "wet", "dormant")


def test_soil_status_wet_and_dry():
    """휴면기 → 과습(관측 날만) → 관수 필요 → 주의(≥ RAW ÷ 2) → 보통. cropwater 사용자 결정 B안(2026-10-24)"""
    s = soil_status
    assert s(0.0, 60, kc=1.0) == "wet" and s(8.9e-16, 60, kc=1.0) == "wet"  # 끝 Dr 0 (부동소수 오차 포함)
    assert s(0.3, 60, kc=1.0) == "normal" and s(29.9, 60, kc=1.0) == "normal"
    assert s(30.0, 60, kc=1.0) == "caution" and s(59.9, 60, kc=1.0) == "caution"
    assert s(60.0, 60, kc=1.0) == "need"
    assert s(0.0, 60, kc=1.0, observed=False) == "normal"  # 예보 날: 과습 판정 안 함
    assert s(30.0, 60, kc=1.0, observed=False) == "caution" and s(61.0, 60, kc=1.0, observed=False) == "need"
    assert s(6.0, 60, kc=1.0, caution_mm=6.0) == "caution" and s(5.9, 60, kc=1.0, caution_mm=6.0) == "normal"
    assert s(0.0, 60, kc=0.0) == "dormant" and s(70.0, 60, kc=0.0) == "dormant"
    assert s(0.0, 60, kc=None) == "normal"  # Kc를 모르면 과습 판정 안 함
    assert s(None, 60, kc=1.0) is None and s(float("nan"), 60, kc=1.0) is None  # 자료 없음


def test_soil_status_never_outside_codes():
    for i in range(0, 241):
        for kc in (None, 0.0, 1.0):
            for obs in (True, False):
                st = soil_status(i / 2, 60, kc=kc, observed=obs)
                assert st in STATUSES
                assert not (st == "wet" and not obs)


# ── 관측 물수지 ───────────────────────────────────────────────────────
def test_observed_wb_matches_wb_step_with_irrigation_log():
    eto, rain = [5.0] * 10, [0, 0, 0, 0, 30, 0, 0, 0, 0, 0]
    irr = {dt.date(2026, 5, 8): 20 / 0.95}  # 공급 21.05 mm → 순 20 mm
    w = observed_water_balance(_days(eto, rain), None, SOIL, irrigation=irr)
    dr = 0.0
    for r, p in zip(w, rain, strict=True):
        inet = 20.0 if r["date"] == dt.date(2026, 5, 8) else 0.0
        ks, etc_adj, dp, dr_new = wb_step(dr, p, 5.0, 120.0, 60.0, inet)
        assert r["dr_prev_mm"] == pytest.approx(dr)
        assert r["dr_mm"] == pytest.approx(dr_new) and r["ks"] == pytest.approx(ks)
        assert r["etc_adj_mm"] == pytest.approx(etc_adj) and r["dp_mm"] == pytest.approx(dp)
        dr = dr_new
    assert w[7]["irr_net_mm"] == pytest.approx(20.0) and w[6]["irr_net_mm"] == 0.0
    assert not any(r["filled"] for r in w)
    assert all(r["eto_src"] == "관측" and r["calc_version"] == CALC_VERSION for r in w)


def test_observed_wb_taint_follows_filled_until_dr_zero():
    """채운 날에 기대는 상태(filled): 채운 날부터 끝 Dr이 0이 되기 전까지"""
    eto = [5.0, 5.0, 4.0, 3.0, 5.0, 5.0]
    rain = [0, 0, 0, 2.0, 50, 0]
    w = observed_water_balance(_days(eto, rain, [False, False, True, True, False, False]), None, SOIL)
    assert [r["filled"] for r in w] == [False, False, True, True, False, False]
    assert w[4]["dr_mm"] == 0.0  # 큰 비로 Dr 0 → 영향 끝
    assert w[2]["eto_src"] == "없음" and w[1]["eto_src"] == "관측"

    w = observed_water_balance(_days(eto, [0] * 6, [False, False, True, False, False, False]), None, SOIL)
    assert [r["filled"] for r in w] == [False, False, True, True, True, True]  # Dr이 0이 안 되면 계속


def test_missing_eto_means_no_crop_water_use():
    """ETo가 비어 오면 ETc 0 (cropwater fcst_wb.observed_wb와 같음), 채운 날로 표시"""
    w = observed_water_balance(_days([5.0, None], [0, 0]), None, SOIL)
    assert w[1]["etc_mm"] == 0.0 and w[1]["dr_mm"] == pytest.approx(5.0)
    assert w[1]["eto_src"] == "없음" and w[1]["filled"]


def test_none_rain_counts_as_zero():
    w = observed_water_balance(_days([5.0, 5.0], [None, 0]), None, SOIL)
    assert w[0]["rain_mm"] is None and w[0]["dr_mm"] == pytest.approx(5.0)


def test_kc_curve_and_dormancy():
    crop = CropParams(
        season_start=dt.date(2026, 5, 3),
        l_ini=2,
        l_dev=2,
        l_mid=2,
        l_late=2,
        kc_ini=0.5,
        kc_mid=1.0,
        kc_end=0.7,
    )
    w = observed_water_balance(_days([4.0] * 12, [0] * 12), crop, SOIL)
    assert [r["kc"] for r in w] == pytest.approx([0, 0, 0.5, 0.5, 0.75, 1.0, 1.0, 1.0, 0.85, 0.7, 0, 0])
    assert [r["status"] for r in w[:2]] == ["dormant", "dormant"]
    assert w[0]["dr_mm"] == 0.0 and w[0]["etc_mm"] == 0.0
    assert w[-1]["status"] == "dormant"


def test_status_per_day():
    w = observed_water_balance(_days([10.0] * 7, [0, 0, 0, 0, 0, 0, 100]), None, SOIL)
    assert [r["status"] for r in w] == ["normal", "normal", "caution", "caution", "caution", "need", "wet"]


def test_dr0_start():
    w = observed_water_balance(_days([5.0], [0]), None, Soil(taw_mm=120.0, raw_mm=60.0, dr0_mm=40.0))
    assert w[0]["dr_prev_mm"] == 40.0 and w[0]["dr_mm"] == pytest.approx(45.0)


def test_soil_from_fc_wp():
    s = Soil.from_soil(fc=0.25, wp=0.12, zr_m=1.0, p=0.5)
    assert s.taw_mm == pytest.approx(130.0) and s.raw_mm == pytest.approx(65.0)  # 계약 v1 사과·양토 기본 조건
