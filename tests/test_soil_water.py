"""작물 공통(흙만 본 물 상태) 판정·계산 시험 (core/soil_water.py, 이슈 #24)."""

import datetime as dt

import pytest

from croplib_dp.core.soil_water import (
    AW_AMPLE_PCT,
    AW_DRY_PCT,
    AW_FAIR_PCT,
    POOL_DP_MIN_MM,
    POOL_DR_MAX_MM,
    SOIL_P,
    SOIL_WATER_STATUSES,
    SOIL_ZR_M,
    aw_pct,
    common_soil,
    days_to_dry,
    soil_water_rows,
    soil_water_status,
)


def test_constants_match_contract_v1_5():
    assert SOIL_WATER_STATUSES == ("winter", "pool", "ample", "fair", "dry", "very_dry")
    assert (SOIL_ZR_M, SOIL_P) == (0.5, 0.40)
    assert (AW_AMPLE_PCT, AW_FAIR_PCT, AW_DRY_PCT) == (80.0, 60.0, 30.0)
    assert (POOL_DP_MIN_MM, POOL_DR_MAX_MM) == (1.0, 0.05)


def test_status_boundaries_winter_pool_and_aw_pct():
    """시험 1: 경계값마다(80/60/30 바로 위·아래, DP 0.9/1.0 mm, 전날 DP만 있음, 12·2·3월 1일)."""
    taw = 100.0
    # 12월·2월·3월 1일 경계
    assert soil_water_status(dt.date(2026, 12, 1), 0.0, taw, dp_today_mm=10.0) == "winter"
    assert soil_water_status(dt.date(2026, 1, 15), 10.0, taw) == "winter"
    assert soil_water_status(dt.date(2026, 2, 1), 10.0, taw) == "winter"
    assert soil_water_status(dt.date(2026, 2, 28), 10.0, taw) == "winter"
    assert soil_water_status(dt.date(2026, 3, 1), 10.0, taw) == "ample"
    assert soil_water_status(dt.date(2026, 11, 30), 10.0, taw) == "ample"

    day = dt.date(2026, 5, 10)
    # 물 고임(pool): 그날 끝 Dr < 0.05 mm 이고, 그날 또는 전날 DP >= 1.0 mm
    assert soil_water_status(day, 0.0, taw, dp_today_mm=1.0, dp_prev_mm=0.0) == "pool"
    assert soil_water_status(day, 0.049, taw, dp_today_mm=1.0, dp_prev_mm=0.0) == "pool"
    assert soil_water_status(day, 0.0, taw, dp_today_mm=0.9, dp_prev_mm=0.9) == "ample"
    assert soil_water_status(day, 0.0, taw, dp_today_mm=0.0, dp_prev_mm=1.0) == "pool"  # 전날 DP만 있음
    assert soil_water_status(day, 0.05, taw, dp_today_mm=5.0, dp_prev_mm=5.0) == "ample"  # Dr >= 0.05

    # aw_pct 80 / 60 / 30 경계 바로 위·아래 (taw=100 일 때 dr = 100 - aw)
    assert soil_water_status(day, 19.9, taw) == "ample"  # aw = 80.1
    assert soil_water_status(day, 20.0, taw) == "ample"  # aw = 80.0
    assert soil_water_status(day, 20.1, taw) == "fair"  # aw = 79.9
    assert soil_water_status(day, 39.9, taw) == "fair"  # aw = 60.1
    assert soil_water_status(day, 40.0, taw) == "fair"  # aw = 60.0
    assert soil_water_status(day, 40.1, taw) == "dry"  # aw = 59.9
    assert soil_water_status(day, 69.9, taw) == "dry"  # aw = 30.1
    assert soil_water_status(day, 70.0, taw) == "dry"  # aw = 30.0
    assert soil_water_status(day, 70.1, taw) == "very_dry"  # aw = 29.9


def test_march_1_restarts_from_dr_zero_and_winter_rows_blank():
    """시험 3: 2월 말 Dr과 상관없이 3/1 끝 Dr이 그날 물수지로만 정해짐. 12~2월은 winter·빈 칸."""
    with_feb = [
        dict(date=dt.date(2026, 2, 27), eto_mm=5.0, rain_mm=0.0, filled=False),
        dict(date=dt.date(2026, 2, 28), eto_mm=6.0, rain_mm=0.0, filled=False),
        dict(date=dt.date(2026, 3, 1), eto_mm=3.2, rain_mm=0.0, filled=False),
        dict(date=dt.date(2026, 3, 2), eto_mm=2.8, rain_mm=0.0, filled=False),
    ]
    from_mar = with_feb[2:]

    rows_with_feb = soil_water_rows(with_feb, fc=0.25, wp=0.12)
    rows_from_mar = soil_water_rows(from_mar, fc=0.25, wp=0.12)

    # 2월 줄은 winter이고 dr_mm·aw_pct·dp_mm·days_to_dry가 모두 None (eto_mm·rain_mm은 채움)
    for r in rows_with_feb[:2]:
        assert r["status"] == "winter"
        assert r["dr_mm"] is None and r["aw_pct"] is None
        assert r["dp_mm"] is None and r["days_to_dry"] is None
        assert r["eto_mm"] is not None and r["rain_mm"] == 0.0

    # 3/1 끝 Dr은 2월 기상과 상관없이 3/1 하루 물수지(Dr = 3.2)로만 정해진다
    mar1_a, mar2_a = rows_with_feb[2], rows_with_feb[3]
    mar1_b, mar2_b = rows_from_mar[0], rows_from_mar[1]
    assert mar1_a["dr_mm"] == pytest.approx(3.2)
    assert mar1_a["dr_mm"] == pytest.approx(mar1_b["dr_mm"])
    assert mar2_a["dr_mm"] == pytest.approx(6.0)
    assert mar2_a["dr_mm"] == pytest.approx(mar2_b["dr_mm"])


def test_december_rows_are_winter_with_blank_water_balance_fields():
    days = [
        dict(date=dt.date(2026, 11, 30), eto_mm=2.0, rain_mm=0.0, filled=False),
        dict(date=dt.date(2026, 12, 1), eto_mm=1.5, rain_mm=3.0, filled=False),
    ]
    rows = soil_water_rows(days, fc=0.25, wp=0.12)
    assert rows[0]["status"] == "ample" and rows[0]["dr_mm"] == pytest.approx(2.0)
    dec1 = rows[1]
    assert dec1["status"] == "winter"
    assert dec1["dr_mm"] is None and dec1["aw_pct"] is None
    assert dec1["dp_mm"] is None and dec1["days_to_dry"] is None
    assert dec1["eto_mm"] == pytest.approx(1.5) and dec1["rain_mm"] == pytest.approx(3.0)


def test_days_to_dry_only_when_fair_and_positive_eto():
    """시험 5: days_to_dry는 fair일 때만 채우고, 평균 ETo가 0이면 빈 칸."""
    raw = 26.0  # 양토 TAW 65 mm, p 0.40 -> RAW 26 mm
    # fair이고 평균 ETo > 0 이면 올림((RAW - Dr) / 평균 ETo)
    assert days_to_dry("fair", dr_mm=15.0, raw_mm=raw, eto_mean_mm=3.0) == 4  # ceil(11 / 3) = 4
    assert days_to_dry("fair", dr_mm=20.0, raw_mm=raw, eto_mean_mm=3.0) == 2  # ceil(6 / 3) = 2
    # fair가 아니면 모두 None
    for st in ("winter", "pool", "ample", "dry", "very_dry", None):
        assert days_to_dry(st, dr_mm=15.0, raw_mm=raw, eto_mean_mm=3.0) is None
    # 평균 ETo가 0 또는 None이면 None
    assert days_to_dry("fair", dr_mm=15.0, raw_mm=raw, eto_mean_mm=0.0) is None
    assert days_to_dry("fair", dr_mm=15.0, raw_mm=raw, eto_mean_mm=None) is None


def test_soil_water_rows_7d_sums_and_prev_day_dp_pool():
    soil = common_soil(0.25, 0.12)
    assert soil.taw_mm == pytest.approx(65.0) and soil.raw_mm == pytest.approx(26.0)
    assert aw_pct(13.0, 65.0) == pytest.approx(80.0)

    # 첫날 큰 비(DP 발생), 둘째 날 비 없고 ETo=0 -> 전날 DP >= 1 이고 끝 Dr < 0.05 이므로 둘째 날도 pool
    days = [
        dict(date=dt.date(2026, 3, 1), eto_mm=2.0, rain_mm=10.0, filled=False),
        dict(date=dt.date(2026, 3, 2), eto_mm=0.0, rain_mm=0.0, filled=False),
        dict(date=dt.date(2026, 3, 3), eto_mm=0.0, rain_mm=0.0, filled=False),
    ]
    rows = soil_water_rows(days, fc=0.25, wp=0.12)
    assert rows[0]["dp_mm"] == pytest.approx(8.0) and rows[0]["status"] == "pool"
    assert rows[1]["dp_mm"] == pytest.approx(0.0) and rows[1]["status"] == "pool"
    assert rows[2]["status"] == "ample"  # 사흘째는 그날·전날 모두 DP = 0
    assert rows[2]["eto_7d_mm"] == pytest.approx(2.0)
    assert rows[2]["rain_7d_mm"] == pytest.approx(10.0)
