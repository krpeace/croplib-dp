"""cropwater 예보 대조 테스트: 같은 가짜 단기예보·관측에서 cropwater 운영 서비스와 같은 3일 전망이 나오는가.

기준값 `tests/data/forecast_golden.json`은 cropwater `feat/cycle02-forecast` 운영 경로
(kma_fcst.Store → fcst_archive.ops_service_table → cropwater_fcst.forecast_table(S4 고정, S3 대체)
 → fcst_wb.observed_wb(예보로 채움) → fcst_wb.service_outlook)를 그대로 실행해 만든 것이다.
만드는 스크립트: `tests/data/make_forecast_golden.py`, 가짜 입력: `tests/data/forecast_fixture.py`.
입력에는 빠진 발표 2개(직전 발표로 대신), 하늘상태가 빠진 발표 1개(S3),
관측 지연(전날 관측 없음), 큰 비가 들어 있다.
"""

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent / "data"))

from forecast_fixture import ELEV, LAT, LON, TS, all_issues, issue_items, obs_frame  # noqa: E402

from croplib_dp.core.forecast_daily import (  # noqa: E402
    ForecastArchive,
    items_frame,
    service_table,
    usable_items,
)
from croplib_dp.core.forecast_eto import forecast_table  # noqa: E402
from croplib_dp.core.outlook import (  # noqa: E402
    fill_from_forecast,
    last_observed,
    outlook_rows,
    pooled_errors,
    service_outlook,
    wb_frame,
)
from croplib_dp.core.water_balance import CropParams, Soil, observed_water_balance  # noqa: E402
from croplib_dp.io.coefficients import load_error_rows, load_s3_coef, load_s4_coef  # noqa: E402

GOLDEN = json.loads((Path(__file__).parent / "data" / "forecast_golden.json").read_text(encoding="utf-8"))
NAME_TO_CODE = {
    "관수 필요": "need",
    "주의": "caution",
    "보통": "normal",
    "과습 경고": "wet",
    "휴면기": "dormant",
}
TAW, RAW, EA = 130.0, 65.0, 0.95


@pytest.fixture(scope="module")
def setup():
    issues = all_issues()
    items = [x for iss in issues for x in issue_items(iss)]
    assert len(items) == GOLDEN["n_items"]
    df = items_frame(items)
    arch = ForecastArchive.from_items(pd.concat([usable_items(df, iss) for iss in issues]))
    stab = service_table(arch, lat=LAT, lon=LON, start=TS("2026-06-08"), end=TS("2026-06-10 17:00"))
    crop = CropParams(
        season_start=dt.date(2026, 4, 1),
        l_ini=20,
        l_dev=70,
        l_mid=90,
        l_late=30,
        kc_ini=0.50,
        kc_mid=GOLDEN["kc_mid"],
        kc_end=GOLDEN["kc_end"],
    )
    ft_all = forecast_table(stab, LAT, ELEV, load_s3_coef("101"), load_s4_coef("101"), crop.kc)
    err = pooled_errors(load_error_rows(), "101")
    obs = obs_frame()
    days = [
        dict(date=pd.Timestamp(d).date(), eto_mm=float(e), rain_mm=float(r), filled=False)
        for d, e, r in zip(obs.date, obs.ETo_obs, obs.rain, strict=True)
    ]
    return ft_all, crop, err, days


@pytest.mark.parametrize(
    "i", range(len(GOLDEN["runs"])), ids=[f"{r['run_name']} {r['run']}" for r in GOLDEN["runs"]]
)
def test_outlook_matches_cropwater(setup, i):
    ft_all, crop, err, days = setup
    g = GOLDEN["runs"][i]
    run, rn = TS(g["run"]), g["run_name"]
    rows = ft_all[(ft_all.run == run) & (ft_all.run_name == rn)]
    ft = ft_all.drop(index=rows.index[rows["ETc_main"].isna()])  # 대체 발표가 담지 못한 마지막 날은 뺀다
    D = run.normalize()
    wb = observed_water_balance(
        days,
        crop,
        Soil(taw_mm=TAW, raw_mm=RAW, dr0_mm=0.0, ea=EA),
        forecast_fill=fill_from_forecast(ft),
        end=(D - pd.Timedelta(days=1)).date(),
    )
    owb = wb_frame(wb)
    last = last_observed(owb, D)
    obs_last = last if (last is not None and last < D - pd.Timedelta(days=1)) else None
    assert (None if obs_last is None else str(obs_last.date())) == g["obs_last"]

    # 관측 물수지 (6/1부터): 예보로 채운 날 포함
    mine = [r for r in wb if r["date"] >= dt.date(2026, 6, 1)]
    assert len(mine) == len(g["wb"])
    for r, w in zip(mine, g["wb"], strict=True):
        assert r["date"].isoformat() == w["date"]
        assert r["dr_mm"] == pytest.approx(w["dr"], abs=1e-9), w["date"]
        assert r["eto_mm"] == pytest.approx(w["eto"], abs=1e-9), w["date"]
        assert r["eto_src"] == w["src"] and r["filled"] == w["filled"], w["date"]

    ol = service_outlook(ft, owb, TAW, RAW, EA, err, rn, run, obs_last=obs_last)
    assert ol["start"] == pytest.approx(g["start"], abs=1e-9)
    assert ol["dr_obs_prev"] == pytest.approx(g["dr_obs_prev"], abs=1e-9)
    assert ol["need"] == g["need"]
    assert [ol["cum3"][0], ol["cum3"][1]] == pytest.approx(g["cum3"][:2], abs=1e-9) and ol["cum3"][2] == g[
        "cum3"
    ][2]
    adv = [None if pd.isna(x) else x for x in ol["advice"]]
    assert adv == (
        [pytest.approx(x, abs=1e-9) for x in g["advice"]] if g["advice"][0] is not None else [None, None]
    )
    assert [dict(date=str(p["date"].date()), kind=p["kind"]) for p in ol["pre"]] == g["pre"]

    d = ol["days"]
    assert len(d) == len(g["days"])
    out = outlook_rows(ol, RAW)
    for r, o, x in zip(d.itertuples(), out, g["days"], strict=True):
        assert int(r.lead_day) == x["lead_day"] and str(pd.Timestamp(r.target).date()) == x["target"]
        assert bool(r.main) == x["main"]
        for col, key in (
            ("ETo_main", "eto"),
            ("Kc", "kc"),
            ("ETc_main", "etc"),
            ("etc_err", "etc_err"),
            ("Dr_center", "dr_center"),
            ("Dr_early", "dr_early"),
            ("Dr_late", "dr_late"),
        ):
            assert getattr(r, col) == pytest.approx(x[key], abs=1e-9), (x["target"], key)
        for col, key in (("rain", "rain"), ("rain_exp", "rain_exp"), ("pop_max", "pop_max")):
            v = getattr(r, col)
            assert (None if pd.isna(v) else pytest.approx(v, abs=1e-9)) == x[key], (x["target"], key)
        assert (
            r.rs_method == x["rs_method"] and str(r.src_run) == x["src_run"] and bool(r.backup) == x["backup"]
        )
        assert o["status"] == NAME_TO_CODE[x["status"]]
        assert o["pop_pct"] == (None if x["pop_max"] is None else pytest.approx(x["pop_max"] * 100))


def test_golden_covers_the_cases():
    """기준값이 확인하려는 경우를 모두 담고 있는지: 대체 발표, S3, 관측 지연, 저녁 출발, 관수 필요 예상일"""
    runs = GOLDEN["runs"]
    days = [x for r in runs for x in r["days"]]
    assert any(x["backup"] for x in days) and {x["rs_method"] for x in days} == {"S3", "S4"}
    assert any(r["obs_last"] for r in runs) and any(p["kind"] == "today" for r in runs for p in r["pre"])
    assert {r["need"]["center"] for r in runs} >= {0, 1, 2}
    assert {x["status"] for x in days} == {"관수 필요", "주의", "보통"}
