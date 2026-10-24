"""예보 일사 추정과 예보 ETo (core/solar.py, core/forecast_eto.py), 계수 파일 (io/coefficients.py).

cropwater `tests/test_fcst.py`·`tests/test_ops.py`(feat/cycle02-forecast)의 해당 테스트를 가져왔다.
계수를 다시 맞추는 테스트(fit, fit_s4)는 그 코드를 옮기지 않아 뺐다.
"""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from croplib_dp.core.forecast_eto import forecast_table
from croplib_dp.core.solar import ra_rso, rs_s1, rs_s3, rs_s4
from croplib_dp.core.water_balance import CropParams
from croplib_dp.io.coefficients import load_error_rows, load_s3_coef, load_s4_coef

TS = pd.Timestamp
CROP = CropParams(dt.date(2026, 4, 1), 20, 70, 90, 30, 0.5, 1.1, 0.9)


def test_rs_clip_between_005ra_and_rso():
    ra, rso = ra_rso(37.9, 75.8, [100])
    assert rs_s1(40, 0, ra, rso, 0.30)[0] == pytest.approx(rso[0])
    assert rs_s3(10, 10, 1, ra, rso, -0.1, 0.19, -0.11)[0] == pytest.approx(0.05 * ra[0])


def test_rs_s4_per_row_coefficients():
    rng = np.random.default_rng(5)
    n = 200
    tx = 20 + 8 * rng.random(n)
    tn = tx - (4 + 10 * rng.random(n))
    fl = (rng.random(n) < 0.3).astype(float)
    cl, ov = rng.random(n) * 0.5, rng.random(n) * 0.5
    ra = 30 + 10 * rng.random(n)
    co = np.array([0.25, 0.11, -0.05, -0.09, -0.23])
    rso = ra * 0.76
    one = rs_s4(tx, tn, fl, cl, ov, ra, rso, co)
    per_row = rs_s4(tx, tn, fl, cl, ov, ra, rso, np.tile(co, (n, 1)))
    raw = (co[0] + co[1] * np.sqrt(tx - tn) + co[2] * fl + co[3] * cl + co[4] * ov) * ra
    assert np.allclose(one, per_row) and (one <= rso + 1e-12).all()
    assert np.allclose(one, np.minimum(np.maximum(raw, 0.05 * ra), rso))


def test_coefficient_files_for_chuncheon():
    s3 = load_s3_coef("101")
    assert (s3["method"], s3["a"], s3["b"], s3["c"]) == ("S3", -0.0971, 0.187, -0.1139)
    other = load_s3_coef("216")  # 계수가 없는 지점 → FAO-56 기본값(S1)
    assert other["method"] == "S1" and other["a"] is None and other["krs"] == 0.16
    s4 = load_s4_coef("101")
    assert list(s4.lead_day) == [0, 1, 2, 3, 4] and s4.loc[0, "a"] == pytest.approx(0.2403)
    assert load_s4_coef("216").empty
    err = load_error_rows()
    assert set(err.stn) == {"101"} and set(err.kind) == {"day", "cum3"}


def _st():
    run = TS("2026-06-10 02:00")
    return pd.DataFrame(
        dict(
            run_name="아침",
            run=run,
            target=[TS("2026-06-11"), TS("2026-06-12"), TS("2026-06-11")],
            lead_day=[1, 2, 1],
            src_lead=[1, 2, 2],
            Tmax=28.0,
            Tmin=18.0,
            ea=1.8,
            u10=2.0,
            rain=0.0,
            rain_flag=0,
            sky_cloudy=[0.2, np.nan, 0.2],
            sky_overcast=[0.1, np.nan, 0.1],
            pop=0.2,
            pop_max=[0.3, np.nan, 0.3],
            rain_exp=0.0,
        )
    )


def test_forecast_table_s3_fallback_and_src_lead():
    s4 = load_s4_coef("101")
    ft = forecast_table(_st(), 37.9, 75.8, load_s3_coef("101"), s4, CROP.kc)
    assert list(ft.rs_method) == ["S4", "S3", "S4"]  # 하늘상태가 없는 행은 S3
    assert ft.ETo_main.iloc[1] == pytest.approx(ft.ETo_S3.iloc[1])
    c = s4.set_index("lead_day")
    assert ft.s4_a.iloc[2] == pytest.approx(c.loc[2, "a"]) and ft.s4_a.iloc[0] == pytest.approx(c.loc[1, "a"])
    assert ft.ETo_S4.iloc[2] != pytest.approx(ft.ETo_S4.iloc[0])  # 같은 입력이라도 대체 발표 선행일의 계수
    assert list(ft.Kc) == pytest.approx([CROP.kc(dt.date(2026, 6, d)) for d in (11, 12, 11)])
    assert np.allclose(ft.ETc_main, ft.Kc * ft.ETo_main)


def test_forecast_table_without_sky_coefficients_uses_s3():
    ft = forecast_table(_st(), 37.9, 75.8, load_s3_coef("101"), load_s4_coef("216"), CROP.kc)
    assert set(ft.rs_method) == {"S3"} and "ETo_S4" not in ft
    assert np.allclose(ft.ETo_main, ft.ETo_S3)


def test_forecast_eto_matches_penman_monteith():
    from croplib_dp.core.fao56 import eto_penman_monteith, wind_2m

    ft = forecast_table(_st(), 37.9, 75.8, load_s3_coef("101"), load_s4_coef("101"), CROP.kc)
    r = ft.iloc[0]
    exp = eto_penman_monteith(
        28.0, 18.0, r.Rs_S4, wind_2m(2.0, 10.0), 1.8, 75.8, 37.9, r.target.dayofyear, None
    )
    assert r.ETo_main == pytest.approx(exp)
