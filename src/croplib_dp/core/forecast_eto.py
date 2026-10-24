"""예보 ETo·ETc (서비스 발표 표 → 대상일별 예보 기준 증발산량과 작물 증발산량).

원본: cropwater `cropwater_fcst.forecast_table` (운영 경로: S4 고정 계수, S4를 못 쓰는 행은 S3, 결정 #12).
검증용 열(관측값 붙이기, 기준선, 교차검증)은 옮기지 않았다.
  - 주 방법(★): 하늘상태가 있으면 S4, 없으면 S3 → 열 Rs_main·ETo_main·ETc_main, 행마다 rs_method
  - S4 계수는 선행일별 고정값. 직전 발표로 대신한 행은 대체 발표 기준 선행일(src_lead)의 계수를 쓴다
  - 예보 풍속은 10 m 기준, 기압은 고도로 추정(FAO-56 식7)
"""

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from croplib_dp.core.fao56 import eto_penman_monteith, wind_2m
from croplib_dp.core.solar import KRS_FAO, S4_NAMES, S4_RAIN, ra_rso, rs_s1, rs_s3, rs_s4

FCST_ANEM = 10.0  # 단기예보 풍속(WSD) 기준 높이 (m)


def eto_series(
    tmax, tmin, ea, u10, rs, dates, lat: float, elev: float, anem: float = FCST_ANEM
) -> np.ndarray:
    """행별 PM ETo. 기압은 고도로 추정(예보에는 기압이 없음)"""
    out = []
    for tx, tn, e, u, r, d in zip(tmax, tmin, ea, u10, rs, dates, strict=True):
        if any(pd.isna(v) for v in (tx, tn, e, u, r)):
            out.append(np.nan)
            continue
        out.append(
            eto_penman_monteith(tx, tn, r, wind_2m(u, anem), e, elev, lat, pd.Timestamp(d).dayofyear, None)
        )
    return np.array(out, dtype=float)


def rs_estimate(
    tmax, tmin, flag, dates, lat: float, elev: float, coef: Mapping[str, Any], method: str = "S3"
):
    """Rs 추정 → (Rs, Ra, Rso). S3 = 식(50)+강수유무(계수 a·b·c, 없으면 S1로 대체), S1 = 식(50) kRs 0.16"""
    ra, rso = ra_rso(lat, elev, pd.DatetimeIndex(dates).dayofyear)
    if method == "S3" and coef.get("a") is not None:
        return rs_s3(tmax, tmin, flag, ra, rso, coef["a"], coef["b"], coef["c"]), ra, rso
    return rs_s1(tmax, tmin, ra, rso, KRS_FAO), ra, rso


def forecast_table(
    st: pd.DataFrame,
    lat: float,
    elev: float,
    s3_coef: Mapping[str, Any],
    s4_fixed: pd.DataFrame | None,
    kc_of: Callable[[pd.Timestamp], float],
) -> pd.DataFrame:
    """서비스 발표 표(forecast_daily.service_table) → 예보 ETo(S4·S3·S1)·Kc·ETc.
    s3_coef: S3 계수 dict(a, b, c). s4_fixed: DataFrame[lead_day, a~e] (없으면 S3만).
    kc_of: 날짜 → 그날 Kc (water_balance.CropParams.kc)."""
    df = st.copy()
    df["target"] = pd.to_datetime(df["target"])
    df["run_date"] = df["run"].dt.normalize()
    for c in ("rain_flag", "sky_cloudy", "sky_overcast", "pop", "pop_max", "rain_exp"):
        if c in df:
            df[c] = pd.to_numeric(df[c])
    rs3, ra, rso = rs_estimate(df.Tmax, df.Tmin, df.rain_flag, df.target, lat, elev, s3_coef, "S3")
    rs1, _, _ = rs_estimate(df.Tmax, df.Tmin, df.rain_flag, df.target, lat, elev, s3_coef, "S1")
    df["Ra"], df["Rso"], df["Rs_S3"], df["Rs_S1"] = ra, rso, rs3, rs1
    df["u2"] = df["u10"].map(lambda u: np.nan if pd.isna(u) else wind_2m(u, FCST_ANEM))
    df["ETo_S3"] = eto_series(df.Tmax, df.Tmin, df.ea, df.u10, df.Rs_S3, df.target, lat, elev)
    df["ETo_S1"] = eto_series(df.Tmax, df.Tmin, df.ea, df.u10, df.Rs_S1, df.target, lat, elev)
    methods = ["S3", "S1"]
    has_sky = (
        "sky_cloudy" in df and df["sky_cloudy"].notna().any() and S4_RAIN in df and df[S4_RAIN].notna().any()
    )
    if has_sky and s4_fixed is not None and len(s4_fixed):
        m = s4_fixed.set_index("lead_day")
        klead = df["src_lead"] if "src_lead" in df else df["lead_day"]
        co = np.array(
            [
                m.loc[k, list(S4_NAMES)].to_numpy(float) if k in m.index else np.full(len(S4_NAMES), np.nan)
                for k in klead
            ]
        ).reshape(len(df), len(S4_NAMES))
        for j, n in enumerate(S4_NAMES):
            df[f"s4_{n}"] = co[:, j]
        df["Rs_S4"] = rs_s4(df.Tmax, df.Tmin, df[S4_RAIN], df.sky_cloudy, df.sky_overcast, df.Ra, df.Rso, co)
        df["ETo_S4"] = eto_series(df.Tmax, df.Tmin, df.ea, df.u10, df.Rs_S4, df.target, lat, elev)
        methods.insert(0, "S4")
    main = methods[0]
    df["Rs_main"], df["ETo_main"] = df[f"Rs_{main}"], df[f"ETo_{main}"]
    # 운영 결측 규칙(#12): 하늘상태·강수확률이 없는 행은 S3
    df["rs_method"] = np.where(df["ETo_main"].notna(), main, None)
    if main == "S4":
        fb = df["ETo_S4"].isna() & df["ETo_S3"].notna()
        df.loc[fb, "Rs_main"] = df.loc[fb, "Rs_S3"]
        df.loc[fb, "ETo_main"] = df.loc[fb, "ETo_S3"]
        df.loc[fb, "rs_method"] = "S3"
    df["Kc"] = [float(kc_of(t)) for t in df.target]
    for c in methods + ["main"]:
        df[f"ETc_{c}"] = df["Kc"] * df[f"ETo_{c}"]
    return df
