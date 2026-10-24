"""cropwater 대조 테스트: 같은 가짜 입력에서 cropwater와 같은 숫자가 나오는가.

기준값 `tests/data/cropwater_golden.json`은 cropwater `feat/cycle02-forecast` 브랜치
(현장 PC가 H3 시험 중인 코드)를 그대로 실행해 만든 것이다.
만드는 스크립트: `tests/data/make_cropwater_golden.py` (cropwater 폴더를 인자로 받음).
  - multi: cropwater_multi.compute_station + compute_water_balance (Kc 1, 결측 7일 평균, 무관수)
  - wb   : fcst_wb.observed_wb + soil_status (사과 시나리오 3 기후보정 Kc, 관수 1회, 03-25~11-05)
cropwater는 결과를 소수 둘째·셋째 자리에서 반올림해 내므로 그 자리까지 비교한다.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from croplib_dp.core.daily_weather import StationMeta, daily_weather
from croplib_dp.core.fao56 import kc_climate_adjust
from croplib_dp.core.water_balance import CropParams, Soil, observed_water_balance

GOLDEN = json.loads((Path(__file__).parent / "data" / "cropwater_golden.json").read_text(encoding="utf-8"))
NAME_TO_CODE = {
    "관수 필요": "need",
    "주의": "caution",
    "보통": "normal",
    "과습 경고": "wet",
    "휴면기": "dormant",
}


def _asos_items():
    """make_cropwater_golden.py의 items()와 같은 가짜 ASOS 행"""
    out = []
    for i in range(30):
        d = dt.date(2026, 5, 1) + dt.timedelta(days=i)
        if i == 11:
            continue
        it = dict(
            tm=d.isoformat(),
            maxTa=f"{22 + (i % 7) * 1.3:.1f}",
            minTa=f"{10 + (i % 5) * 0.9:.1f}",
            avgTa=f"{16 + (i % 6) * 1.1:.1f}",
            avgRhm=f"{55 + (i % 9) * 3:.1f}",
            minRhm=f"{30 + (i % 8) * 2.5:.1f}",
            avgWs=f"{1.2 + (i % 4) * 0.6:.1f}",
            avgPv=f"{11 + (i % 5) * 0.8:.1f}",
            avgTd=f"{8 + (i % 5) * 0.7:.1f}",
            avgPa=f"{1003 - (i % 3) * 2.5:.1f}",
            sumGsr=f"{14 + (i % 10) * 1.4:.2f}",
            sumSsHr=f"{5 + (i % 6) * 1.2:.1f}",
            sumLrgEv="",
            sumRn="",
        )
        if i == 4:
            it["sumRn"] = "25.5"
        if i == 14:
            it["sumGsr"] = ""
        if i == 17:
            it["avgPv"] = ""
        if i == 18:
            it["avgPv"] = ""
            it["avgTd"] = ""
        if i == 19:
            it["maxTa"] = ""
        if i == 21:
            it["sumRn"] = "80.0"
        if i == 25:
            it["sumRn"] = "3.0"
        out.append(it)
    return out


def test_daily_eto_and_kc1_water_balance_match_cropwater_multi():
    weather = daily_weather(_asos_items(), StationMeta(lat=37.90262, elev_m=75.82, wind_height_m=10.0))
    wb = observed_water_balance(weather, None, Soil(taw_mm=120.0, raw_mm=60.0))
    gold = GOLDEN["multi"]
    assert len(weather) == len(gold) == 30
    for w, b, g in zip(weather, wb, gold, strict=True):
        assert w["date"].isoformat() == g["date"]
        assert w["filled"] == g["filled"], g["date"]
        if g["eto"] is not None:
            assert w["eto_mm"] == pytest.approx(g["eto"], abs=1e-9), g["date"]
        assert w["eto_mm"] == pytest.approx(g["eto_wb"], abs=5e-4), g["date"]
        assert (w["rain_mm"] or 0.0) == pytest.approx(g["rain"]), g["date"]
        assert b["dr_mm"] == pytest.approx(g["dr"], abs=5e-3), g["date"]
        assert b["dp_mm"] == pytest.approx(g["dp"], abs=5e-3), g["date"]
        assert b["ks"] == pytest.approx(g["ks"], abs=5e-4), g["date"]


def test_kc_curve_water_balance_and_status_match_cropwater_fcst_wb():
    kc_mid = kc_climate_adjust(1.20, 1.5, 55, 3.2)
    kc_end = kc_climate_adjust(0.95, 1.5, 55, 3.2, is_end=True)
    assert kc_mid == pytest.approx(GOLDEN["kc_mid"]) and kc_end == pytest.approx(GOLDEN["kc_end"])
    crop = CropParams(
        season_start=dt.date(2026, 4, 1),
        l_ini=20,
        l_dev=70,
        l_mid=90,
        l_late=30,
        kc_ini=0.50,
        kc_mid=kc_mid,
        kc_end=kc_end,
    )
    start = dt.date(2026, 3, 25)
    days = [
        dict(date=start + dt.timedelta(days=i), eto_mm=e, rain_mm=p, filled=False)
        for i, (e, p) in enumerate(zip(GOLDEN["eto"], GOLDEN["rain"], strict=True))
    ]
    soil = Soil(taw_mm=120.0, raw_mm=60.0, dr0_mm=0.0, ea=0.95)
    wb = observed_water_balance(days, crop, soil, irrigation={dt.date(2026, 7, 1): 30.0})
    gold = GOLDEN["wb"]
    assert len(wb) == len(gold)
    for r, g in zip(wb, gold, strict=True):
        assert r["date"].isoformat() == g["date"]
        assert r["kc"] == pytest.approx(g["kc"], abs=1e-12), g["date"]
        assert r["etc_mm"] == pytest.approx(g["etc"], abs=1e-9), g["date"]
        assert r["dr_mm"] == pytest.approx(g["dr"], abs=1e-9), g["date"]
        assert r["ks"] == pytest.approx(g["ks"], abs=1e-12), g["date"]
        assert r["dp_mm"] == pytest.approx(g["dp"], abs=1e-9), g["date"]
        assert r["status"] == NAME_TO_CODE[g["status"]], g["date"]
    assert {r["status"] for r in wb} == {
        "need",
        "caution",
        "normal",
        "wet",
        "dormant",
    }  # 다섯 상태가 모두 나오는 입력
