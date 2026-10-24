"""예보로 일사량(Rs) 추정. 단기예보에는 일사가 없어서 기온차·강수·하늘상태로 추정한다.

원본: cropwater `rs_model.py` (feat/cycle02-forecast).
계수를 다시 맞추는 함수(fit, fit_s4)는 검증용이라 옮기지 않았다.
  S1: FAO-56 식(50) Hargreaves, kRs 0.16 (내륙 기본값)
  S3: 식(50) + 강수유무 — Rs/Ra = a + b·√(Tmax−Tmin) + c·강수유무  (지점 계수, 춘천 2025 관측으로 맞춤)
  S4: S3 + 하늘상태 — Rs/Ra = a + b·√ΔT + c·강수확률 하루 최대 + d·구름많음 비율 + e·흐림 비율 (선행일별 계수)
모든 추정값은 0.05·Ra ~ Rso 사이로 자른다.
"""

from typing import Any

import numpy as np

from croplib_dp.core.fao56 import extra_radiation

S4_NAMES = ("a", "b", "c", "d", "e")
S4_RAIN = "pop_max"  # S4 강수 입력: 강수확률 하루 최대(0~1). cropwater #17 결정(2026-09-30)
KRS_FAO = 0.16


def ra_rso(lat: float, elev: float, doy: Any) -> tuple[np.ndarray, np.ndarray]:
    """천문일사 Ra(식21)와 맑은 날 일사 Rso(식37)"""
    ra = np.array([extra_radiation(lat, int(j)) for j in np.atleast_1d(doy)])
    return ra, (0.75 + 2e-5 * elev) * ra


def clip_rs(rs: Any, ra: Any, rso: Any) -> np.ndarray:
    return np.minimum(np.maximum(rs, 0.05 * ra), rso)


def rs_s1(tmax: Any, tmin: Any, ra: Any, rso: Any, krs: float = KRS_FAO) -> np.ndarray:
    dT = np.maximum(np.asarray(tmax, float) - np.asarray(tmin, float), 0.0)
    return clip_rs(krs * np.sqrt(dT) * ra, ra, rso)


def rs_s3(
    tmax: Any, tmin: Any, rain_flag: Any, ra: Any, rso: Any, a: float, b: float, c: float
) -> np.ndarray:
    dT = np.maximum(np.asarray(tmax, float) - np.asarray(tmin, float), 0.0)
    return clip_rs((a + b * np.sqrt(dT) + c * np.asarray(rain_flag, float)) * ra, ra, rso)


def s4_design(tmax: Any, tmin: Any, rain: Any, cloudy: Any, overcast: Any) -> np.ndarray:
    """S4 설명변수 [1, √(Tmax−Tmin), 강수 입력, 구름많음 비율, 흐림 비율]"""
    dT = np.maximum(np.asarray(tmax, float) - np.asarray(tmin, float), 0.0)
    return np.column_stack(
        [
            np.ones(len(dT)),
            np.sqrt(dT),
            np.asarray(rain, float),
            np.asarray(cloudy, float),
            np.asarray(overcast, float),
        ]
    )


def rs_s4(
    tmax: Any, tmin: Any, rain: Any, cloudy: Any, overcast: Any, ra: Any, rso: Any, coef: Any
) -> np.ndarray:
    """S4 Rs. coef: (a, b, c, d, e) 한 벌 또는 행마다 한 벌(n×5 배열)"""
    X = s4_design(tmax, tmin, rain, cloudy, overcast)
    co = np.asarray(coef, float)
    kt = X @ co if co.ndim == 1 else (X * co).sum(axis=1)
    ra = np.asarray(ra, float)
    return clip_rs(kt * ra, ra, np.asarray(rso, float))
