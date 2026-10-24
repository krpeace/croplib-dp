"""Kc 기후보정 (FAO-56 식62·65): 지점의 생육 중기·후기 평균 풍속과 최저습도로 Kc_mid·Kc_end를 고친다.

원본: cropwater `cropwater_station.py`의 Kc 자동집계(_stage_agg)와 설정 시트 [7] (feat/cycle02-forecast).
  - 중기: 작기 시작 + L_ini + L_dev 부터 L_mid 일, 후기: 그 뒤 L_late 일
  - 그 구간의 관측 날 중 값이 있는 날의 평균. 10일보다 적으면 기본값(u2 1.5 m/s, RHmin 55%)
  - 표값(DB crop_kc_sets)은 보정 전 값이고, 보정한 Kc는 water_balance_daily.kc에 남는다
"""

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from croplib_dp.core.fao56 import kc_climate_adjust
from croplib_dp.core.water_balance import CropParams

MIN_DAYS = 10  # 구간 평균에 필요한 최소 날 수
DEFAULT_U2 = 1.5  # m/s
DEFAULT_RHMIN = 55.0  # %


@dataclass(frozen=True)
class StageClimate:
    """생육 단계 평균 기상. 값이 None이면 날이 모자라 기본값을 쓴 것"""

    u2_mid: float | None
    rh_mid: float | None
    u2_end: float | None
    rh_end: float | None
    n_mid: int
    n_end: int


def stage_windows(crop: CropParams) -> tuple[tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    """(중기 시작, 끝), (후기 시작, 끝) — 끝 날 포함"""
    mid_start = crop.season_start + dt.timedelta(days=crop.l_ini + crop.l_dev)
    mid_end = mid_start + dt.timedelta(days=crop.l_mid - 1)
    late_start = mid_end + dt.timedelta(days=1)
    late_end = crop.season_start + dt.timedelta(days=crop.l_ini + crop.l_dev + crop.l_mid + crop.l_late - 1)
    return (mid_start, mid_end), (late_start, late_end)


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if len(vals) >= MIN_DAYS else None


def _agg(
    weather: list[Mapping[str, Any]], start: dt.date, end: dt.date
) -> tuple[float | None, float | None, int]:
    u2s = [w["u2_ms"] for w in weather if start <= w["date"] <= end and w.get("u2_ms") is not None]
    rhs = [w["rh_min_pct"] for w in weather if start <= w["date"] <= end and w.get("rh_min_pct") is not None]
    return _mean(u2s), _mean(rhs), len(u2s)


def stage_climate(weather: Iterable[Mapping[str, Any]], crop: CropParams) -> StageClimate:
    """일 기상값(daily_weather 결과: date, u2_ms, rh_min_pct) → 생육 중기·후기 평균"""
    weather = list(weather)
    (ms, me), (ls, le) = stage_windows(crop)
    u2m, rhm, nm = _agg(weather, ms, me)
    u2e, rhe, ne = _agg(weather, ls, le)
    return StageClimate(u2_mid=u2m, rh_mid=rhm, u2_end=u2e, rh_end=rhe, n_mid=nm, n_end=ne)


def adjust_crop(crop: CropParams, climate: StageClimate, h_m: float | None) -> CropParams:
    """표값 Kc → 기후보정한 Kc (Kc_ini는 그대로). h_m(작물 높이)이 없으면 보정하지 않는다"""
    if h_m is None:
        return crop
    u2m = climate.u2_mid if climate.u2_mid is not None else DEFAULT_U2
    rhm = climate.rh_mid if climate.rh_mid is not None else DEFAULT_RHMIN
    u2e = climate.u2_end if climate.u2_end is not None else DEFAULT_U2
    rhe = climate.rh_end if climate.rh_end is not None else DEFAULT_RHMIN
    return replace(
        crop,
        kc_mid=kc_climate_adjust(crop.kc_mid, u2m, rhm, h_m),
        kc_end=kc_climate_adjust(crop.kc_end, u2e, rhe, h_m, is_end=True),
    )
