"""앞으로 3일 물 전망 (DB `water_outlook`).

원본: cropwater `fcst_wb.py`의 서비스 경로 (feat/cycle02-forecast)
  - service_outlook, 오차표(pooled_errors·err_lookup), 세 경로(run_paths)
  - 관측이 빠진 날 채우기(fill_from_forecast), 관측 지연(last_observed)
  - 그날 예보 고르기(day_forecast)
검증용 경로(예보 강수 그대로·비 무시·관측 강수·기준선·참값)와 판정 집계는 옮기지 않았다.

  출발: 관측 물수지의 마지막 관측일 끝 부족한 물(Dr)
    - 아침(02시): 전날까지 관측이 있으면 바로 D+0~D+3
    - 저녁(17시): 그날(D)을 그날 아침 발표 D+0 예보로 먼저 진행한 뒤 D+1~D+4
    - 관측이 늦어 전날(D−1)이 비어 있으면 그날을 아침 D+0 예보로 먼저 진행 (범위도 함께, cropwater G5)
  세 경로 (관수하지 않으면)
    - 중심: 예보 ETc, 기대 강수(시각별 강수량 × 강수확률)
    - 빠르면: ETc × (1 + r), 비 없음 (관측 강수를 아는 날은 그 강수)
    - 늦으면: ETc × (1 − r), 예보 강수 전부
    r = 월·발표·선행일별 예보 ETo 상대 오차(RMSE ÷ 관측 평균, 오차표). 칸이 없으면 0.25
  주 지표는 처음 3개 대상일, 마지막 날은 참고.
"""

import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from croplib_dp.core import CALC_VERSION
from croplib_dp.core.fao56 import wb_step
from croplib_dp.core.water_balance import soil_status

MAIN_DAYS = 3  # 서비스 주 지표: 처음 3개 대상일
ERR_MIN_N = 15  # 오차표: 월 칸 표본이 이보다 적으면 전체 월 값을 씀
ERR_DEFAULT_REL = 0.25  # 오차표에 칸이 없을 때의 상대 오차(보수적 기본값)
ERR_COLS = ["stn", "year", "kind", "run_name", "lead_day", "month", "n", "rmse", "mbe", "obs_mean"]


# ── 오차표 ──────────────────────────────────────────────────────────────
def pooled_errors(rows: pd.DataFrame, stn: str, exclude_year: int | None = None) -> pd.DataFrame:
    """오차표 행 → 지점의 여러 해를 합친 표(칸별 MSE를 표본 수로 가중). 지점 행이 없으면 stn '0'(기본값) 행,
    그것도 없으면 있는 지점을 모두 합친다."""
    t = rows[rows.stn.astype(str) == str(stn)]
    src = str(stn)
    if not len(t):
        t, src = rows[rows.stn.astype(str) == "0"], "0"
    if not len(t):
        t, src = rows, "전 지점(" + ",".join(sorted(set(rows.stn.astype(str)))) + ")"
    if exclude_year is not None:
        t = t[t.year.astype(int) != int(exclude_year)]
    cols = ["kind", "run_name", "lead_day", "month", "n", "rmse", "mbe", "obs_mean", "years", "stn_src"]
    if not len(t):
        return pd.DataFrame(columns=cols)
    t = t.assign(sse=t.n * t.rmse**2, se=t.n * t.mbe, so=t.n * t.obs_mean)
    g = t.groupby(["kind", "run_name", "lead_day", "month"]).agg(
        n=("n", "sum"),
        sse=("sse", "sum"),
        se=("se", "sum"),
        so=("so", "sum"),
        years=("year", lambda s: ",".join(str(x) for x in sorted(set(s)))),
    )
    g = g.reset_index()
    g["rmse"], g["mbe"], g["obs_mean"] = np.sqrt(g.sse / g.n), g.se / g.n, g.so / g.n
    g["stn_src"] = src
    return g[cols]


def err_lookup(err: pd.DataFrame | None, kind: str, run_name: str, lead_day: int, month: int):
    """오차표 칸 → (RMSE, 상대 오차 = RMSE ÷ 관측 평균, 표본 수, '월별'/'전체 월'/'기본값').
    월 칸 표본이 ERR_MIN_N보다 적으면 전체 월 값"""
    if err is None or len(err) == 0:
        return np.nan, ERR_DEFAULT_REL, 0, "기본값"
    t = err[(err.kind == kind) & (err.run_name == run_name) & (err.lead_day == int(lead_day))]
    m = t[t.month == int(month)]
    if len(m) and int(m.n.iloc[0]) >= ERR_MIN_N:
        r, src = m.iloc[0], "월별"
    else:
        a = t[t.month == 0]
        if not len(a):
            return np.nan, ERR_DEFAULT_REL, 0, "기본값"
        r, src = a.iloc[0], "전체 월"
    rel = float(r.rmse / r.obs_mean) if r.obs_mean > 0 else ERR_DEFAULT_REL
    return float(r.rmse), rel, int(r.n), src


# ── 관측 물수지와 예보 잇기 ─────────────────────────────────────────────
def fill_from_forecast(ft: pd.DataFrame, col: str = "ETo_main") -> dict:
    """관측이 빠진 날을 채울 값: 그날 아침 발표 D+0 예보 {date: (ETo, 기대 강수)}"""
    m = ft[(ft.run_name == "아침") & (ft.lead_day == 0) & ft[col].notna()]
    rain = m["rain_exp"].fillna(m["rain"]) if "rain_exp" in m else m["rain"]
    return {
        pd.Timestamp(t).date(): (float(e), float(0.0 if pd.isna(r) else r))
        for t, e, r in zip(m.target, m[col], rain, strict=True)
    }


def wb_frame(rows: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    """관측 물수지 목록(water_balance.observed_water_balance)
    → 날짜 색인 표(Dr, P, I_net, obs_row, ETo_src)"""
    out = []
    for r in rows:
        rain = r.get("rain_mm")
        out.append(
            dict(
                date=pd.Timestamp(r["date"]),
                Dr=float(r["dr_mm"]),
                P=0.0 if rain is None or (isinstance(rain, float) and math.isnan(rain)) else float(rain),
                I_net=float(r.get("irr_net_mm") or 0.0),
                obs_row=bool(r.get("obs_row", rain is not None)),
                ETo_src=r.get("eto_src"),
            )
        )
    return pd.DataFrame(out)


def last_observed(owb: pd.DataFrame, before: Any) -> pd.Timestamp | None:
    """관측 물수지에서 관측 ETo가 있는 마지막 날(before 전). 없으면 None"""
    m = owb[(owb.ETo_src == "관측") & (pd.to_datetime(owb.date) < pd.Timestamp(before))]
    return pd.Timestamp(m.date.max()) if len(m) else None


def day_forecast(ft: pd.DataFrame, day: Any, etc_col: str = "ETc_main"):
    """하루(day)를 먼저 진행할 예보 행: 그날 아침 발표 D+0.
    없으면 그날을 대상으로 한 가장 최근 서비스 발표(그날 02시까지)"""
    day = pd.Timestamp(day)
    t = pd.to_datetime(ft.target)
    m = ft[(ft.run_name == "아침") & (ft.lead_day == 0) & (t == day) & ft[etc_col].notna()]
    if len(m):
        return m.iloc[0]
    m = ft[(t == day) & ft[etc_col].notna() & (pd.to_datetime(ft.run) <= day + pd.Timedelta(hours=2))]
    return m.sort_values("run").iloc[-1] if len(m) else None


def path(dr0: float, etc: list, rain: list, taw: float, raw: float) -> list:
    """출발 고갈량 dr0에서 관수 없이 날마다 wb_step → 끝 고갈량 목록"""
    out, dr = [], dr0
    for e, p in zip(etc, rain, strict=True):
        dr = wb_step(dr, p, e, taw, raw)[3]
        out.append(dr)
    return out


def first_need(drs: list, raw: float) -> int | None:
    """경로에서 처음 Dr ≥ RAW인 순번(1부터). 없으면 None"""
    for i, v in enumerate(drs, 1):
        if not pd.isna(v) and v >= raw:
            return i
    return None


def _pre_day(m, day: pd.Timestamp, etc_col: str, rain_obs: float | None = None, irr: float = 0.0) -> dict:
    """관측 대신 아침 D+0 예보로 하루를 먼저 진행할 입력.
    rain_obs: 그날 관측 강수를 이미 알면 세 경로 모두 그 값"""
    rain0 = 0.0 if pd.isna(m["rain"]) else float(m["rain"])
    rexp = float(m["rain_exp"]) if "rain_exp" in m and pd.notna(m["rain_exp"]) else rain0
    known = rain_obs is not None and not pd.isna(rain_obs)
    if known:
        rain0 = rexp = float(rain_obs)
    return dict(
        etc=float(m[etc_col]),
        rain=rain0,
        rain_exp=rexp,
        rel=float(m["rel"]),
        irr=float(irr),
        rain_known=known,
        date=pd.Timestamp(day),
        Kc=float(m["Kc"]),
        ETo=float(m["ETo_main"]),
        pop_max=m.get("pop_max", np.nan),
    )


def run_paths(dr0: float, x: dict, taw: float, raw: float) -> dict:
    """출발 고갈량과 입력 목록 → 경로별 끝 고갈량 목록 (중심·빠르면·늦으면). 관수량은 모든 경로에 더한다"""
    n = len(x["etc"])
    irr = x.get("irr") or [0.0] * n
    known = x.get("known") or [False] * n

    def w(rain):
        return [a + b for a, b in zip(rain, irr, strict=True)]

    return dict(
        center=path(dr0, x["etc"], w(x["rain_exp"]), taw, raw),
        early=path(
            dr0,
            [e * (1 + r) for e, r in zip(x["etc"], x["rel"], strict=True)],
            w([p if k else 0.0 for p, k in zip(x["rain"], known, strict=True)]),
            taw,
            raw,
        ),
        late=path(
            dr0,
            [e * max(1 - r, 0.0) for e, r in zip(x["etc"], x["rel"], strict=True)],
            w(x["rain"]),
            taw,
            raw,
        ),
    )


def _run_inputs(g: pd.DataFrame, pre: list, etc_col: str) -> dict:
    raw_rain = g["rain"].astype(float).fillna(0.0)
    exp_rain = (g["rain_exp"].astype(float).fillna(raw_rain) if "rain_exp" in g else raw_rain).fillna(0.0)
    return dict(
        etc=[p["etc"] for p in pre] + list(g[etc_col].astype(float)),
        rain=[p["rain"] for p in pre] + list(raw_rain),
        rain_exp=[p["rain_exp"] for p in pre] + list(exp_rain),
        rel=[p["rel"] for p in pre] + list(g["rel"]),
        irr=[p.get("irr", 0.0) for p in pre] + [0.0] * len(g),
        known=[bool(p.get("rain_known", False)) for p in pre] + [False] * len(g),
    )


def service_outlook(
    ft: pd.DataFrame,
    owb: pd.DataFrame,
    taw: float,
    raw: float,
    ea: float,
    err: pd.DataFrame | None,
    run_name: str,
    run: Any,
    etc_col: str = "ETc_main",
    obs_last: Any = None,
) -> dict:
    """한 서비스 발표의 전망.
    obs_last: 관측이 있는 마지막 날(없으면 D−1).
              그 뒤 D−1까지는 관측이 아직 없는 날 → 아침 D+0 예보로 먼저 진행.
    반환: dict(days=대상일 표, start=출발 고갈량(중심), dr_obs_prev, prev, pre, lag_days,
              need{center, early, late},
              cum3=(3일 ETc 합, ± 오차, 출처, 상대 오차), advice=(순관수량, 공급 관수량))"""
    d = ft[(ft.run_name == run_name) & (ft.run == pd.Timestamp(run))].sort_values("lead_day").copy()
    if d.empty:
        raise ValueError(f"{run_name} {run} 발표가 예보표에 없습니다")
    if d[etc_col].isna().any():
        raise ValueError(f"{run_name} {run} 발표의 예보 ETc가 비어 있습니다(필수 요소 결측)")
    d["month"] = pd.to_datetime(d.target).dt.month
    # 직전 발표로 대신한 행은 대체 발표 종류·선행일의 오차
    en = d["err_name"] if "err_name" in d else pd.Series(run_name, index=d.index)
    el = d["err_lead"] if "err_lead" in d else d["lead_day"]
    e = [err_lookup(err, "day", n_, k, m) for n_, k, m in zip(en, el, d.month, strict=True)]
    d["eto_rmse"], d["rel"], d["err_n"], d["err_src"] = (
        [x[0] for x in e],
        [x[1] for x in e],
        [x[2] for x in e],
        [x[3] for x in e],
    )
    d["etc_err"] = d[etc_col] * d.rel  # ± 오차 = 예보 ETc × 상대 오차 r
    D = pd.Timestamp(run).normalize()
    o = owb.set_index("date")
    prev = pd.Timestamp(obs_last) if obs_last is not None else D - pd.Timedelta(days=1)
    if prev not in o.index:
        raise ValueError(f"관측 물수지에 {prev:%Y-%m-%d}(출발일)가 없습니다")
    lag_days = list(pd.date_range(prev + pd.Timedelta(days=1), D - pd.Timedelta(days=1)))
    pre = []
    for day, kind in [(x, "lag") for x in lag_days] + ([(D, "today")] if run_name == "저녁" else []):
        m = day_forecast(ft, day, etc_col)
        if m is None:
            raise ValueError(
                f"{day:%Y-%m-%d}을 진행할 예보가 없습니다(아침 발표 D+0)"
                if kind == "lag"
                else f"저녁 발표는 그날({D:%Y-%m-%d}) 아침 발표의 D+0 예보가 필요합니다"
            )
        m = m.copy()
        m["rel"] = err_lookup(err, "day", "아침", 0, day.month)[1]
        has = day in o.index
        rain_obs = (
            float(o.loc[day, "P"])
            if (kind == "lag" and has and bool(o.loc[day].get("obs_row", False)))
            else None
        )
        irr = float(o.loc[day, "I_net"]) if (kind == "lag" and has) else 0.0
        pre.append(dict(_pre_day(m, day, etc_col, rain_obs, irr), kind=kind))
    x = _run_inputs(d, pre, etc_col)
    dr0 = float(o.loc[prev, "Dr"])
    res = run_paths(dr0, x, taw, raw)
    n0 = len(pre)
    for k in ("center", "early", "late"):
        d[f"Dr_{k}"] = res[k][n0:]
    for i, p in enumerate(pre):
        p.update({f"Dr_{k}": res[k][i] for k in ("center", "early", "late")})
    d["order"] = range(1, len(d) + 1)
    d["main"] = d.order <= MAIN_DAYS
    starts = {k: (res[k][n0 - 1] if n0 else dr0) for k in ("center", "early", "late")}
    start = starts["center"]
    # 관수 필요 예상일은 서비스 엑셀처럼 주 지표 3일만 본다(마지막 참고일은 보지 않음, 2026-10-25 사용자 결정)
    need = {
        k: (0 if starts[k] >= raw else first_need(res[k][n0 : n0 + MAIN_DAYS], raw))
        for k in ("center", "early", "late")
    }
    main = d[d.main]
    c3 = err_lookup(err, "cum3", run_name, int(main.lead_day.iloc[0]), int(main.month.iloc[0]))
    if "err_name" in main and bool(main.get("backup", pd.Series(False)).any()):
        cb = err_lookup(
            err, "cum3", str(main.err_name.iloc[0]), int(main.err_lead.iloc[0]), int(main.month.iloc[0])
        )
        c3 = cb if cb[3] != "기본값" else c3  # 대체 발표 칸이 오차표에 없으면 서비스 발표 칸
    s3 = float(main[etc_col].sum())
    cum3 = (s3, s3 * c3[1], c3[3], c3[1])
    k_need = need["center"]
    dr_need = start if k_need == 0 else (float(d.Dr_center.iloc[k_need - 1]) if k_need else np.nan)
    advice = (dr_need, dr_need / ea) if not pd.isna(dr_need) else (np.nan, np.nan)
    return dict(
        days=d,
        start=start,
        dr_obs_prev=dr0,
        prev=prev,
        pre=pre,
        lag_days=lag_days,
        need=need,
        cum3=cum3,
        advice=advice,
        run_name=run_name,
        run=pd.Timestamp(run),
    )


def _num(v: Any) -> float | None:
    return None if v is None or pd.isna(v) else float(v)


def outlook_rows(ol: dict, raw: float) -> list[dict]:
    """서비스 전망 → `water_outlook` 줄 목록 (run_id, scenario_id는 쓰는 쪽이 붙인다).
    상태는 예보 날 기준(과습 판정 안 함). pop_pct는 그날 가장 높은 강수확률(%)."""
    rows = []
    for r in ol["days"].itertuples():
        pop = getattr(r, "pop_max", None)
        rows.append(
            dict(
                target_date=pd.Timestamp(r.target).date(),
                lead_day=int(r.lead_day),
                eto_mm=_num(r.ETo_main),
                etc_mm=_num(r.ETc_main),
                etc_err_mm=_num(r.etc_err),
                rain_fcst_mm=_num(r.rain),
                pop_pct=None if _num(pop) is None else float(pop) * 100,
                rain_expected_mm=_num(getattr(r, "rain_exp", None)),
                dr_center_mm=_num(r.Dr_center),
                dr_early_mm=_num(r.Dr_early),
                dr_late_mm=_num(r.Dr_late),
                status=soil_status(r.Dr_center, raw, r.Kc, observed=False),
                calc_version=CALC_VERSION,
            )
        )
    return rows


def outlook_summary(ol: dict, raw: float, kc_start: float | None) -> dict:
    """서비스 전망 → `water_outlook_summary` 한 줄 (run_id, scenario_id는 쓰는 쪽이 붙인다).
    서비스 엑셀 "관수 전망" 위쪽과 같다. kc_start: 출발 날짜의 Kc (휴면기·과습 판정용).
    출발 날짜가 관측 물수지 날이면(먼저 진행한 날이 없으면) 과습도 판정한다."""
    d = ol["days"]
    main = d[d["main"]]
    pre = ol["pre"]
    start_date = (pre[-1]["date"] if pre else ol["prev"]).date()
    net, gross = ol["advice"]
    rain_exp = main["rain_exp"] if "rain_exp" in main else main["rain"]
    return dict(
        horizon_days=MAIN_DAYS,
        start_date=start_date,
        dr_start_mm=_num(ol["start"]),
        status_start=soil_status(ol["start"], raw, kc_start, observed=not pre),
        need_center_in=ol["need"]["center"],
        need_early_in=ol["need"]["early"],
        need_late_in=ol["need"]["late"],
        advice_net_mm=_num(net),
        advice_gross_mm=_num(gross),
        etc_sum_mm=_num(ol["cum3"][0]),
        etc_sum_err_mm=_num(ol["cum3"][1]),
        rain_sum_mm=float(main["rain"].astype(float).fillna(0.0).sum()),
        rain_expected_sum_mm=float(rain_exp.astype(float).fillna(0.0).sum()),
        calc_version=CALC_VERSION,
    )
