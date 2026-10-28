"""작물 공통(흙만 본 물 상태) 계산과 상태 판정 (DB `soil_water_daily` 한 줄, 데이터 계약 v1.5).

작물용 `soil_status`(`need`, `caution`, `normal`, `wet`, `dormant`)와 섞지 않는다.
고정 조건: 겉흙 깊이 Zr = 0.5 m, 증발 수요 = ETo × 1.0 (Kc = 1), p = 0.40,
해마다 3월 1일에 Dr = 0(포장용수량)으로 새로 시작해 11월 30일까지 계산한다.
12~2월은 줄만 두고 `status = 'winter'`, `dr_mm`·`aw_pct`·`dp_mm`·`days_to_dry`는 비운다.

상태 판정 (데이터 계약 v1.5, 위에서부터 처음 맞는 것)
  winter    겨울철     12~2월
  pool      물 고임    그날 끝 Dr < 0.05 mm 이고, 그날 또는 전날 DP ≥ 1 mm
  ample     충분       aw_pct ≥ 80
  fair      보통       60 ≤ aw_pct < 80
  dry       건조       30 ≤ aw_pct < 60
  very_dry  매우 건조  aw_pct < 30
경계값을 바꾸면 core/__init__.py 의 CALC_VERSION 을 올린다.
"""

import datetime as dt
import math
from collections.abc import Iterable, Mapping
from typing import Any

from croplib_dp.core import CALC_VERSION
from croplib_dp.core.water_balance import Soil, _missing, observed_water_balance

SOIL_WATER_STATUSES = ("winter", "pool", "ample", "fair", "dry", "very_dry")
SOIL_ZR_M = 0.5  # 고정 겉흙 깊이 (m)
SOIL_P = 0.40  # 고정 고갈 허용 분율 (RAW = 0.40 × TAW -> 남은 물 60%)
SOIL_DR0_MM = 0.0  # 해마다 3/1 출발 고갈량 (0 = 포장용수량)
WINTER_MONTHS = (12, 1, 2)  # 겨울철 (판정 안 함)
POOL_DR_MAX_MM = 0.05  # 물 고임: 그날 끝 Dr이 이보다 작으면 0으로 본다
POOL_DP_MIN_MM = 1.0  # 물 고임: 그날 또는 전날 심층침투(DP) 최소 기준 (mm)
AW_AMPLE_PCT = 80.0  # 충분 하한 (%)
AW_FAIR_PCT = 60.0  # 보통 하한 (%, RAW 경계 = 1 - p)
AW_DRY_PCT = 30.0  # 건조 하한 (%, Ks = 0.5 지점)
WINDOW_DAYS = 7  # 최근 합·평균 계산 일수


def common_soil(
    fc: float,
    wp: float,
    zr_m: float = SOIL_ZR_M,
    p: float = SOIL_P,
    dr0_mm: float = SOIL_DR0_MM,
) -> Soil:
    """작물 공통 흙 조건 (`Soil.from_soil(fc, wp, zr_m=0.5, p=0.40, dr0_mm=0)`)."""
    return Soil.from_soil(fc, wp, zr_m=zr_m, p=p, dr0_mm=dr0_mm)


def aw_pct(dr_mm: float | None, taw_mm: float) -> float | None:
    """흙 속 남은 물 비율(%) = (TAW − Dr) ÷ TAW × 100."""
    if _missing(dr_mm) or taw_mm <= 0:
        return None
    return max(0.0, min(100.0, (float(taw_mm) - float(dr_mm)) / float(taw_mm) * 100.0))


def soil_water_status(
    day: dt.date,
    dr_mm: float | None,
    taw_mm: float,
    dp_today_mm: float | None = None,
    dp_prev_mm: float | None = None,
) -> str | None:
    """작물 공통 흙 물 상태 코드 6종. 위에서부터 처음 맞는 것."""
    if day.month in WINTER_MONTHS:
        return "winter"
    if _missing(dr_mm):
        return None
    has_dp = (not _missing(dp_today_mm) and float(dp_today_mm) >= POOL_DP_MIN_MM) or (
        not _missing(dp_prev_mm) and float(dp_prev_mm) >= POOL_DP_MIN_MM
    )
    if float(dr_mm) < POOL_DR_MAX_MM and has_dp:
        return "pool"
    aw = aw_pct(dr_mm, taw_mm)
    if aw is None:
        return None
    if aw >= AW_AMPLE_PCT:
        return "ample"
    if aw >= AW_FAIR_PCT:
        return "fair"
    if aw >= AW_DRY_PCT:
        return "dry"
    return "very_dry"


def days_to_dry(
    status: str | None,
    dr_mm: float | None,
    raw_mm: float,
    eto_mean_mm: float | None,
) -> int | None:
    """비가 오지 않으면 건조(`dry`)까지 남은 날. `fair`일 때만 계산하고 평균 ETo가 0이면 None."""
    if status != "fair" or _missing(dr_mm) or _missing(eto_mean_mm) or float(eto_mean_mm) <= 0:
        return None
    return math.ceil(max(0.0, float(raw_mm) - float(dr_mm)) / float(eto_mean_mm))


def soil_water_rows(
    days: Iterable[Mapping[str, Any]],
    fc: float,
    wp: float,
    zr_m: float = SOIL_ZR_M,
    p: float = SOIL_P,
) -> list[dict]:
    """날짜순 일 기상값(date, eto_mm, rain_mm, filled) → 작물 공통 `soil_water_daily` 줄 목록.
    해마다 3/1에 Dr = 0으로 새로 시작해 11/30까지 `observed_water_balance(crop=None, ...)`로 돌리고,
    12~2월은 `status = 'winter'`와 빈 물수지 칸으로 둔다."""
    soil = common_soil(fc, wp, zr_m=zr_m, p=p)
    day_list = sorted(days, key=lambda d: d["date"])
    active_by_year: dict[int, list[Mapping[str, Any]]] = {}
    for d in day_list:
        day = d["date"]
        if day.month not in WINTER_MONTHS:
            active_by_year.setdefault(day.year, []).append(d)

    wb_by_date: dict[dt.date, dict] = {}
    dp_prev_by_date: dict[dt.date, float | None] = {}
    for _, yr_days in sorted(active_by_year.items()):
        wb = observed_water_balance(yr_days, None, soil)
        prev_dp: float | None = None
        for r in wb:
            wb_by_date[r["date"]] = r
            dp_prev_by_date[r["date"]] = prev_dp
            prev_dp = r["dp_mm"]

    rows: list[dict] = []
    for i, d in enumerate(day_list):
        day = d["date"]
        win_start = day - dt.timedelta(days=WINDOW_DAYS - 1)
        win = [x for x in day_list[max(0, i - WINDOW_DAYS + 1) : i + 1] if win_start <= x["date"] <= day]
        etos = [float(x["eto_mm"]) for x in win if not _missing(x.get("eto_mm"))]
        rains = [float(x["rain_mm"]) for x in win if not _missing(x.get("rain_mm"))]
        eto_7d = sum(etos) if etos else None
        rain_7d = sum(rains) if rains else None
        eto_mean = (sum(etos) / len(etos)) if etos else None

        if day.month in WINTER_MONTHS:
            eto = None if _missing(d.get("eto_mm")) else float(d["eto_mm"])
            rain = None if _missing(d.get("rain_mm")) else float(d["rain_mm"])
            obs_row = bool(d["obs_row"]) if "obs_row" in d else rain is not None
            filled = bool(d.get("filled")) or _missing(eto) or not obs_row
            rows.append(
                dict(
                    date=day,
                    eto_mm=eto,
                    rain_mm=rain,
                    eto_7d_mm=eto_7d,
                    rain_7d_mm=rain_7d,
                    dp_mm=None,
                    dr_mm=None,
                    taw_mm=soil.taw_mm,
                    raw_mm=soil.raw_mm,
                    aw_pct=None,
                    status="winter",
                    days_to_dry=None,
                    filled=filled,
                    calc_version=CALC_VERSION,
                )
            )
        else:
            wb_row = wb_by_date[day]
            dr_val = wb_row["dr_mm"]
            dp_val = wb_row["dp_mm"]
            aw_val = aw_pct(dr_val, soil.taw_mm)
            st = soil_water_status(
                day, dr_val, soil.taw_mm, dp_today_mm=dp_val, dp_prev_mm=dp_prev_by_date[day]
            )
            dtd = days_to_dry(st, dr_val, soil.raw_mm, eto_mean)
            rows.append(
                dict(
                    date=day,
                    eto_mm=wb_row["eto_mm"],
                    rain_mm=wb_row["rain_mm"],
                    eto_7d_mm=eto_7d,
                    rain_7d_mm=rain_7d,
                    dp_mm=dp_val,
                    dr_mm=dr_val,
                    taw_mm=soil.taw_mm,
                    raw_mm=soil.raw_mm,
                    aw_pct=aw_val,
                    status=st,
                    days_to_dry=dtd,
                    filled=bool(wb_row["filled"]),
                    calc_version=CALC_VERSION,
                )
            )
    return rows
