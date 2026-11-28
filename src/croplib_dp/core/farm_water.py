"""농장 물수지 (회원 농장 한 곳의 물수지 · 3일 전망, 이슈 #131, 결정 0040).

DB·API·파일을 모르는 순수 함수만 둔다. 공개 조건의 계산 함수(`observed_water_balance` · `service_outlook` ·
`outlook_rows` · `outlook_summary`)를 그대로 부르고, 농장에만 있는 것을 더한다:
  - 작기 고르기: `farm_seasons` 의 날짜로 정한다(`season_year` 로 고르지 않는다). 해가 바뀌어도 Dr 를 이어받는다.
  - 관수 기록: 그날 작기 안의 기록만, mm = L ÷ 관수 면적, 근권에는 × 관수효율.
  - 시설 재배: 그날까지 기록이 0건인 날은 `etc_only`, 첫 기록 날의 끝 Dr = 0, 다음 날부터 비 0 물수지.
  - 예보표 되살리기: 공개 전망 줄(`water_outlook`)에서 `service_outlook` 이 읽는 표(ft)를 만든다.
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pandas as pd

from croplib_dp.core import CALC_VERSION
from croplib_dp.core.fao56 import wb_step
from croplib_dp.core.forecast_daily import error_kind
from croplib_dp.core.outlook import (
    _start_status,
    lag_days_without_forecast,
    last_observed,
    outlook_rows,
    outlook_summary,
    service_outlook,
    service_outlook_etc_only,
    wb_frame,
)
from croplib_dp.core.water_balance import CropParams, Soil, observed_water_balance, soil_status

PAST_SEASON_GRACE_DAYS = (
    3  # 끝난 지 이 일수 안인 지난 작기는 마지막 날 값을 관측으로 바꾸려고 한 번 더 계산한다
)


def num(v: Any) -> float | None:
    """DB 값(숫자 · 숫자 글자 · Decimal · 빈 값) → float. 읽을 수 없으면 오류(조용히 0으로 두지 않는다)."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError(f"숫자가 아닙니다: {v!r}")
    if isinstance(v, int | float):
        return None if isinstance(v, float) and math.isnan(v) else float(v)
    if isinstance(v, Decimal):
        return float(v)
    return float(str(v))


def to_date(v: Any) -> dt.date:
    if isinstance(v, pd.Timestamp):
        return v.date()
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])


# ── 회원 설정 (CropMon v2.6, 이슈 #140) ──────────────────────────────────────
STAGE_COLS = ("l_ini_d", "l_dev_d", "l_mid_d", "l_late_d")


def irrigation_efficiency(code: Any, efficiencies: Mapping[str, Any]) -> float:
    """관수 방식 코드 → 관수효율(`irrigation_methods.efficiency`, 0 < x <= 1). 코드가 비었거나 사전에 없거나
    값을 읽을 수 없으면 ValueError — 기본값으로 대신하지 않는다."""
    v = num(efficiencies.get(code)) if code else None
    if v is None or not 0 < v <= 1:
        raise ValueError("관수 방식 값을 읽지 못함")
    return v


def with_stage_days(crop: CropParams, settings: Mapping) -> CropParams:
    """회원의 생육 단계 일수(`l_ini_d` ~ `l_late_d`)가 넷 다 있으면 덮어쓰고, 넷 다 비면 그대로.
    일부만 있거나 1 이상의 정수가 아니면 ValueError(0 일은 Kc 곡선이 0 으로 나눈다)."""
    raw = [settings.get(c) for c in STAGE_COLS]
    if all(v is None for v in raw):
        return crop
    days = [num(v) for v in raw]
    if any(v is None or v < 1 or not v.is_integer() for v in days):
        raise ValueError("단계 일수 값이 올바르지 않음")
    ini, dev, mid, late = (int(v) for v in days)
    return replace(crop, l_ini=ini, l_dev=dev, l_mid=mid, l_late=late)


def season_last_day(crop: CropParams) -> dt.date:
    """작기 마지막 날(끝 포함) = 시작일 + 네 단계 일수 합 - 1"""
    return crop.season_start + dt.timedelta(days=crop.l_ini + crop.l_dev + crop.l_mid + crop.l_late - 1)


# ── 작기 고르기 ──────────────────────────────────────────────────────────────
def current_season(seasons: Iterable[Mapping], today: dt.date) -> Mapping | None:
    """지금 줄: 끝이 오늘 이후(끝나는 날 당일 포함)인 줄 가운데 시작이 가장 이른 것 = 작기 중이거나 아직 시작 전인
    가장 가까운 줄(DB `farm_current_season` 과 같은 정의). 없으면 None."""
    cand = [s for s in seasons if to_date(s["end_date"]) >= today]
    return min(cand, key=lambda s: to_date(s["start_date"])) if cand else None


def seasons_to_calc(
    seasons: Sequence[Mapping], today: dt.date, grace: int = PAST_SEASON_GRACE_DAYS
) -> list[Mapping]:
    """이번에 다시 계산할 작기 줄: 끝난 지 `grace` 일 안인 지난 줄(마지막 날 값이 예보 채움으로 남지 않게 관측으로 한 번
    더) + 지금 줄. 그보다 오래 전에 끝난 줄은 다시 계산하지 않고 남긴다."""
    past = [
        s
        for s in seasons
        if to_date(s["end_date"]) < today <= to_date(s["end_date"]) + dt.timedelta(days=grace)
    ]
    past.sort(key=lambda s: to_date(s["start_date"]))
    cur = current_season(seasons, today)
    return past + ([cur] if cur is not None else [])


# ── 관수 기록 ────────────────────────────────────────────────────────────────
def daily_logs(logs: Iterable[Mapping], start: dt.date, end: dt.date) -> dict[dt.date, float]:
    """관수 기록 줄(date · volume_l) → {날짜: 그날 합(L)}. [start, end] 안의 기록만(작기 밖 기록은 넣지 않는다)."""
    out: dict[dt.date, float] = {}
    for r in logs:
        d = to_date(r["date"])
        v = num(r["volume_l"])
        if v is not None and start <= d <= end:
            out[d] = out.get(d, 0.0) + v
    return out


def first_record(by_date: Mapping[dt.date, float]) -> dt.date | None:
    """이번 작기의 첫 기록 날(없으면 None). 첫 기록을 지우면 다음 기록이 첫 기록이 된다."""
    return min(by_date) if by_date else None


# ── 물수지 줄 ────────────────────────────────────────────────────────────────
def water_rows(
    *,
    season: Mapping,
    crop: CropParams,
    soil: Soil,
    area_m2: float,
    weather: Sequence[Mapping],
    fill: Mapping[dt.date, tuple[float, float]],
    through: dt.date,
    logs_l: Mapping[dt.date, float],
    protected: bool,
    calc_version: str = CALC_VERSION,
) -> list[dict]:
    """한 작기의 물수지 줄: 작기 시작일 ~ through. `farm_water_daily` 열(farm_id 제외) + 안쪽 열 obs_row.
    weather: 날짜순 일 기상값(date · eto_mm · rain_mm · filled). fill: 관측이 빠진 날을 채울 아침 D+0 예보.
    logs_l: {날짜: 그날 기록 합(L)} — 작기 안 기록(daily_logs 결과).
    노지는 `observed_water_balance` 그대로(관수 포함). 시설(protected)은 그날까지 기록 0건인 날 etc_only, 첫 기록 날의
    끝 Dr = 0, 다음 날부터 비 0 · ETc 더하고 관수 mm 뺀 물수지(판정은 노지와 같은 soil_status)."""
    start = to_date(season["start_date"])
    if through < start:
        return []
    days = [w for w in weather if start <= to_date(w["date"]) <= through]
    area = float(area_m2)
    in_window = {d: v for d, v in logs_l.items() if start <= d <= through}
    first = first_record(in_window) if protected else None
    if not protected:
        irrigation = {d: v / area for d, v in in_window.items()}  # 공급 mm (근권에는 × ea)
        rows = observed_water_balance(
            days, crop, soil, irrigation=irrigation, forecast_fill=dict(fill), end=through
        )
    else:
        rows = observed_water_balance(days, crop, soil, forecast_fill=dict(fill), end=through, protected=True)
        if first is not None:
            _protected_balance_from_first_record(rows, first, in_window, area, soil)
    out = []
    for r in rows:
        row = dict(r)
        row["farm_season_id"] = season["farm_season_id"]
        row["irr_l"] = float(in_window.get(r["date"], 0.0))
        row["taw_mm"], row["raw_mm"] = soil.taw_mm, soil.raw_mm
        row["calc_version"] = calc_version
        out.append(row)
    return out


def _protected_balance_from_first_record(
    rows: list[dict], first: dt.date, logs_l: Mapping[dt.date, float], area: float, soil: Soil
) -> None:
    """시설 재배: 첫 기록 날(first)의 끝 Dr = 0(그날 관수로 뿌리층이 찼다고 봄), 다음 날부터 비 0 · ETc 더하고 관수 mm
    빼는 물수지. rows 를 제자리에서 고친다. first 앞 날은 그대로 etc_only."""
    dr, tainted = 0.0, False
    for r in rows:
        day = r["date"]
        if day < first:
            continue
        i_net = float(logs_l.get(day, 0.0)) / area * soil.ea
        filled_day = bool(r["filled"])
        if day == first:
            r.update(
                irr_net_mm=i_net,
                dr_prev_mm=None,
                ks=None,
                etc_adj_mm=None,
                dp_mm=None,
                dr_mm=0.0,
                status=soil_status(0.0, soil.raw_mm, r["kc"], observed=True),
                filled=filled_day,
            )
            dr, tainted = 0.0, filled_day
            continue
        ks, etc_adj, dp, dr_new = wb_step(dr, 0.0, r["etc_mm"], soil.taw_mm, soil.raw_mm, i_net)
        tainted = filled_day or (tainted and dr_new > 0)
        r.update(
            irr_net_mm=i_net,
            dr_prev_mm=dr,
            ks=ks,
            etc_adj_mm=etc_adj,
            dp_mm=dp,
            dr_mm=dr_new,
            status=soil_status(dr_new, soil.raw_mm, r["kc"], observed=True),
            filled=tainted,
        )
        dr = dr_new


def stale_dates(
    existing: Iterable[dt.date],
    seasons: Sequence[Mapping],
    new_dates: set[dt.date],
    cur_start: dt.date | None,
    cur_end: dt.date | None,
) -> list[dt.date]:
    """지울 날짜: 그 농장의 기존 farm_water_daily 날짜 중
    ① 어떤 farm_seasons 줄(지난 줄 포함)의 날짜 범위에도 안 드는 날(작기를 늦추거나 작형을 바꿨거나 지난 줄이 잘렸다),
    ② 지금 작기 범위 안인데 이번에 만들지 않은 날. 끝난 지난 작기 줄(범위 안, 다시 계산하지 않음)은 남긴다."""
    spans = [(to_date(s["start_date"]), to_date(s["end_date"])) for s in seasons]
    out = []
    for d in sorted(set(existing)):
        if not any(a <= d <= b for a, b in spans):
            out.append(d)
        elif (
            cur_start is not None and cur_end is not None and cur_start <= d <= cur_end and d not in new_dates
        ):
            out.append(d)
    return out


# ── 예보표 되살리기 ───────────────────────────────────────────────────────────
def _kst(v: Any) -> pd.Timestamp:
    ts = pd.Timestamp(v)
    return ts.tz_convert("Asia/Seoul").tz_localize(None) if ts.tzinfo is not None else ts


def rebuild_ft(runs: Iterable[Mapping], outlook_src: Iterable[Mapping], kc_of) -> pd.DataFrame:
    """공개 전망 줄 → `service_outlook` 이 읽는 예보표(ft). 예보의 ETo · 비 · 강수확률은 지점 값이라 회차마다 공개 전망
    줄 한 시나리오분(scenario_id 가장 앞)에서 읽고, ETc 는 농장 작형의 Kc(대상일) × ETo 로 새로 만든다(수집기와 같은 식).
    runs: forecast_runs 줄(run_id · run_at · run_name · src_run_at). outlook_src: water_outlook 줄.
    직전 발표로 대신한 회차(src_run_at 있음)는 backup · err_name · err_lead 를 수집기와 같은 규칙으로 단다."""
    by_run: dict[Any, dict[str, list[Mapping]]] = {}
    for o in outlook_src:
        by_run.setdefault(o["run_id"], {}).setdefault(o["scenario_id"], []).append(o)
    rows = []
    for run in sorted(runs, key=lambda r: _kst(r["run_at"])):
        scen = by_run.get(run["run_id"])
        if not scen:
            continue
        pick = scen[min(scen)]
        run_ts = _kst(run["run_at"])
        src = _kst(run["src_run_at"]) if run.get("src_run_at") else None
        backup = src is not None
        for o in sorted(pick, key=lambda x: int(x["lead_day"])):
            target = pd.Timestamp(o["target_date"])
            eto = num(o.get("eto_mm"))
            pop = num(o.get("pop_pct"))
            kc = float(kc_of(target))
            lead = int(o["lead_day"])
            rows.append(
                dict(
                    run_name=run["run_name"],
                    run=run_ts,
                    lead_day=lead,
                    target=target,
                    ETo_main=eto if eto is not None else math.nan,
                    Kc=kc,
                    ETc_main=kc * eto if eto is not None else math.nan,
                    rain=num(o.get("rain_fcst_mm")),
                    rain_exp=num(o.get("rain_expected_mm")),
                    pop_max=pop / 100 if pop is not None else math.nan,
                    src_run=src if backup else run_ts,
                    backup=backup,
                    err_name=error_kind(src) if backup else run["run_name"],
                    err_lead=(target.normalize() - src.normalize()).days if backup else lead,
                )
            )
    return pd.DataFrame(rows)


# ── 3일 전망 ─────────────────────────────────────────────────────────────────
def farm_outlook(
    *,
    ft: pd.DataFrame,
    rows: Sequence[Mapping],
    first: dt.date | None,
    logs_l: Mapping[dt.date, float],
    area_m2: float,
    soil: Soil,
    crop: CropParams,
    err: pd.DataFrame | None,
    run_name: str,
    run: Any,
    protected: bool,
) -> tuple[list[dict], dict]:
    """농장 한 곳의 3일 전망 줄과 요약 한 줄(farm_id · run_id 는 쓰는 쪽이 붙인다).
    노지는 공개 조건과 같은 `service_outlook`. 시설은 출발일까지 기록이 0건이면 `etc_only` 전망(ETc · 3일 합만),
    있으면 같은 Dr 세 경로를 비 0 으로 돌린다(전망 줄의 비 열에는 예보 값을 기록으로 남긴다).
    회차 날짜 이후의 관수 기록은 그날 단계에 관수로 넣는다: 아침 회차는 오늘이 lead 0 단계, 저녁 회차는 출발일.
    시설의 첫 기록이 회차 날짜이면 그날 끝 Dr = 0 이고 전망은 다음 날부터 Dr 경로다(요약 출발도 그날 끝 기준)."""
    run = pd.Timestamp(run)
    D = run.normalize().date()
    prev = D - dt.timedelta(days=1)
    area = float(area_m2)
    irr_future = {d: v / area * soil.ea for d, v in logs_l.items() if d >= D}
    if protected and (first is None or first > D):
        ol = service_outlook_etc_only(ft, err, run_name, run)
        zero = None
    else:
        zero = first if (protected and first == D) else None
        dr_rows = [r for r in rows if r["dr_mm"] is not None and to_date(r["date"]) <= prev]
        if zero is not None or not any(to_date(r["date"]) == prev for r in dr_rows):
            # 시설 첫 기록이 오늘이거나, 작기가 오늘 시작해 어제 줄이 없다: 출발 Dr = 0
            dr_rows = [
                dict(
                    date=prev,
                    dr_mm=0.0,
                    rain_mm=0.0,
                    irr_net_mm=0.0,
                    obs_row=True,
                    eto_src="관측",
                )
            ]
        owb = wb_frame(dr_rows)
        last = last_observed(owb, pd.Timestamp(D))
        obs_last = last if (last is not None and last < pd.Timestamp(D) - pd.Timedelta(days=1)) else None
        if obs_last is not None and lag_days_without_forecast(ft, obs_last, run):
            obs_last = None  # 지연일의 아침 D+0 예보가 없으면 어제부터 출발(공개 조건과 같은 규칙)
        ol = service_outlook(
            ft,
            owb,
            soil.taw_mm,
            soil.raw_mm,
            soil.ea,
            err,
            run_name,
            run,
            obs_last=obs_last,
            irr_future=irr_future or None,
            dr_zero=zero,
            rain_zero=protected,
        )
    out_rows = outlook_rows(ol, soil.raw_mm)
    start_day = (
        ol["start_date"] if ol.get("etc_only") else (ol["pre"][-1]["date"] if ol["pre"] else ol["prev"])
    ).date()
    summ = outlook_summary(ol, soil.raw_mm, crop.kc(start_day), crop.kc(D))
    if zero is not None:  # 첫 기록이 오늘: 출발은 오늘 끝(Dr = 0), 과습 판정은 관측 날이 아니라 하지 않는다
        summ.update(
            start_date=D,
            dr_start_mm=0.0,
            status_start=_start_status(0.0, soil.raw_mm, crop.kc(D), crop.kc(D), observed=False),
        )
    return out_rows, summ
