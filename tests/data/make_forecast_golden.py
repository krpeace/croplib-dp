"""cropwater(feat/cycle02-forecast) 운영 서비스 계산으로 예보 대조 기준값을 만든다. 가짜 입력만 쓴다.

실행: python -I tests/data/make_forecast_golden.py <cropwater 폴더> > tests/data/forecast_golden.json
cropwater 의 운영 경로(cropwater_ops.ops_prepare → build_service → fcst_wb.service_from)를 파일 없이 그대로
따라간다.
"""

import datetime as dt
import json
import os
import sys
import tempfile

CW = sys.argv[1]
sys.path.insert(0, CW)

import fcst_wb as W  # noqa: E402
import kma_fcst as K  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from cropwater_fcst import forecast_table, load_s4_fixed  # noqa: E402
from fao56_core import kc_climate_adjust  # noqa: E402
from fcst_archive import load_archive, ops_service_table  # noqa: E402
from rs_model import load_coef  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from forecast_fixture import ELEV, LAT, LON, TS, all_issues, issue_items, obs_frame  # noqa: E402


def main():
    issues = all_issues()
    items = [x for iss in issues for x in issue_items(iss)]
    with tempfile.TemporaryDirectory() as tmp:
        st = K.Store(tmp)
        for iss in issues:
            df = K.items_frame(issue_items(iss))
            st.store_issue("73_134", iss, df, K.check_issue(df, iss), iss)
        arch = load_archive(st.fcst_files("73_134"))
    stab = ops_service_table(
        arch, lat=LAT, lon=LON, max_age_h=24, start=TS("2026-06-08"), end=TS("2026-06-10 17:00")
    )
    obs = obs_frame()
    kc_mid = kc_climate_adjust(1.20, 1.2, 48.0, 3.2)
    kc_end = kc_climate_adjust(0.95, 1.5, 55.0, 3.2, is_end=True)
    kp = dict(bud=dt.date(2026, 4, 1), L=(20, 70, 90, 30), kc_ini=0.50, kc_mid=kc_mid, kc_end=kc_end)
    s4 = load_s4_fixed("101", os.path.join(CW, "rs_sky_coef.csv"))
    coef = load_coef("101", os.path.join(CW, "rs_coef.csv"))
    ft_all = forecast_table(stab, obs, LAT, ELEV, coef, kp, s4, s3_fallback=True)
    ft_all.attrs = {}
    err = W.pooled_errors(W.load_error_rows(os.path.join(CW, "fcst_error_table.csv")), "101")
    soil = dict(fc=0.25, wp=0.12, zr=1.0, p=0.5, taw=130.0, raw=65.0, dr0=0.0, ea=0.95)
    runs = []
    for rn, run in (
        ("아침", "2026-06-09 02:00"),
        ("저녁", "2026-06-09 17:00"),
        ("아침", "2026-06-10 02:00"),
        ("저녁", "2026-06-10 17:00"),
    ):
        run = TS(run)
        rows = ft_all[(ft_all.run == run) & (ft_all.run_name == rn)]
        drop = rows.index[rows["ETc_main"].isna()]  # build_service: 대체 발표가 담지 못한 마지막 날
        ft = ft_all.drop(index=drop)
        D = run.normalize()
        owb = W.observed_wb(obs, kp, soil, fill=W.fill_from_forecast(ft), end=D - pd.Timedelta(days=1))
        last = W.last_observed(owb, D)
        obs_last = last if (last is not None and last < D - pd.Timedelta(days=1)) else None
        ol = W.service_outlook(ft, owb, soil, err, rn, run, obs_last=obs_last)
        d = ol["days"]
        days = []
        for r in d.itertuples():
            days.append(
                dict(
                    lead_day=int(r.lead_day),
                    target=str(pd.Timestamp(r.target).date()),
                    main=bool(r.main),
                    eto=float(r.ETo_main),
                    kc=float(r.Kc),
                    etc=float(r.ETc_main),
                    etc_err=float(r.etc_err),
                    rain=None if pd.isna(r.rain) else float(r.rain),
                    rain_exp=None if pd.isna(r.rain_exp) else float(r.rain_exp),
                    pop_max=None if pd.isna(r.pop_max) else float(r.pop_max),
                    dr_center=float(r.Dr_center),
                    dr_early=float(r.Dr_early),
                    dr_late=float(r.Dr_late),
                    rs_method=r.rs_method,
                    src_run=str(r.src_run),
                    backup=bool(r.backup),
                    status=W.soil_status(r.Dr_center, soil["raw"], r.Kc, observed=False),
                )
            )
        wb = [
            dict(
                date=str(x.date.date()),
                dr=float(x.Dr),
                eto=float(x.ETo),
                src=x.ETo_src,
                filled=bool(x.tainted),
            )
            for x in owb.itertuples()
            if x.date >= TS("2026-06-01")
        ]
        runs.append(
            dict(
                run_name=rn,
                run=str(run),
                obs_last=None if obs_last is None else str(obs_last.date()),
                start=float(ol["start"]),
                dr_obs_prev=float(ol["dr_obs_prev"]),
                need={k: v for k, v in ol["need"].items()},
                cum3=[float(ol["cum3"][0]), float(ol["cum3"][1]), ol["cum3"][2]],
                advice=[None if pd.isna(x) else float(x) for x in ol["advice"]],
                pre=[dict(date=str(p["date"].date()), kind=p["kind"]) for p in ol["pre"]],
                days=days,
                wb=wb,
            )
        )
    json.dump(
        dict(
            n_items=len(items),
            kc_mid=kc_mid,
            kc_end=kc_end,
            runs=runs,
        ),
        sys.stdout,
        ensure_ascii=False,
    )


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
