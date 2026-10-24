"""물 수집기: 한 회차(02시 아침·17시 저녁)를 처음부터 끝까지 돌린다.

원본: cropwater `cropwater_ops.py`의 Collector·run_slot·slot_result (feat/cycle02-forecast).
서버(GitHub Actions)는 매번 빈 컴퓨터에서 시작한다.
그래서 현장 PC처럼 받은 예보를 폴더에 쌓지 않고 회차마다 새로 받는다.

  1) 사전 자료 읽기 (Supabase: 지점·작물·작형·재배 방식·토성·계산 조건)
  2) 예보 지점(has_forecast)마다 단기예보 받기
       - 서비스 발표를 발표 + 10분부터 0·5·10·15·20·30·45·60·80분에 다시 시도, 마감은 발표 + 90분
       - 첫 시도 뒤 앞선 24시간 발표 8개를 한 번씩 받음 (직전 발표 대체·관측 결측일 채우기용)
       - 인증·요청 오류(fatal)는 바로 멈춤. 마감 뒤에 실행하면 한 번만 시도(지연 수집)
  3) 지점마다 ASOS 일자료(작기 시작 ~ 어제) 한 번 조회 → 일 기상값·ETo
  4) 계산 조건마다 Kc 기후보정 → 관측 물수지(빠진 날은 아침 D+0 예보로) → (예보 지점) 3일 전망
  5) DB 표와 같은 모양의 줄(forecast_runs, weather_daily, water_balance_daily, water_outlook)과 수집 기록

DB에 쓰기는 물3에서 더한다. 지금은 결과를 파일로 남긴다(`python -m croplib_dp.collectors.water`).
"""

import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from croplib_dp.core import CALC_VERSION
from croplib_dp.core.daily_weather import daily_weather
from croplib_dp.core.forecast_daily import (
    ForecastArchive,
    check_issue,
    items_frame,
    service_table,
    usable_items,
)
from croplib_dp.core.forecast_eto import forecast_table
from croplib_dp.core.kc import adjust_crop, stage_climate
from croplib_dp.core.outlook import (
    MAIN_DAYS,
    fill_from_forecast,
    last_observed,
    outlook_rows,
    pooled_errors,
    service_outlook,
    wb_frame,
)
from croplib_dp.core.water_balance import observed_water_balance
from croplib_dp.io import kma
from croplib_dp.io.coefficients import load_error_rows, load_s3_coef, load_s4_coef
from croplib_dp.io.contract import SPECS, check_rows
from croplib_dp.io.dictionaries import (
    Dictionaries,
    crop_height,
    crop_params,
    load_dictionaries,
    soil_of,
    station_meta,
)

SLOTS = {"아침": 2, "저녁": 17}
ISSUE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
AVAIL_MIN = 10  # 발표 10분 뒤부터 제공(활용가이드)
DEADLINE_MIN = 90  # 마감 = 발표 + 90분 (03:30, 18:30). cropwater 2026-10-15 결정
RETRY_MIN = (0, 5, 10, 15, 20, 30, 45, 60, 80)  # 회차 시작(발표 + 10분)부터의 재시도 시각(분)
CATCHUP_H = 24  # 함께 받는 앞선 발표 범위(시간)
COLLECTOR = "actions"
RESULTS = (
    "정시 성공",
    "백업: S3",
    "백업: 직전 발표",
    "지연 수집",
    "실패",
)  # 데이터 계약 forecast_runs.result


# ── 시계 ────────────────────────────────────────────────────────────────
def kst_now() -> pd.Timestamp:
    """현재 한국 시각(시간대 정보 없음). 서버 시간대 설정과 무관하게 UTC + 9시간"""
    t = dt.datetime.now(dt.UTC) + dt.timedelta(hours=9)
    return pd.Timestamp(t.replace(tzinfo=None, microsecond=0))


class Clock:
    def now(self) -> pd.Timestamp:
        return kst_now()

    def sleep(self, sec: float) -> None:
        if sec > 0:
            time.sleep(sec)


def slot_times(issue: Any) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(회차 시작 = 발표 + 10분, 마감 = 발표 + 90분)"""
    issue = pd.Timestamp(issue)
    return issue + pd.Timedelta(minutes=AVAIL_MIN), issue + pd.Timedelta(minutes=DEADLINE_MIN)


def current_slot(now: Any) -> tuple[str, pd.Timestamp]:
    """지금 시각의 회차 = 시작 시각(발표 + 10분)이 지금 이전인 가장 최근 회차 → (구분, 발표시각)"""
    now = pd.Timestamp(now)
    cands = []
    for back in (0, 1):
        d = now.normalize() - pd.Timedelta(days=back)
        for name, h in SLOTS.items():
            iss = d + pd.Timedelta(hours=h)
            if slot_times(iss)[0] <= now + pd.Timedelta(minutes=1):
                cands.append((iss, name))
    iss, name = max(cands)
    return name, iss


def parse_slot(s: str) -> tuple[str, pd.Timestamp]:
    """'2026-10-24 02' → ('아침', 2026-10-24 02:00)"""
    t = pd.Timestamp(s if len(str(s)) > 13 else f"{s}:00")
    name = {h: n for n, h in SLOTS.items()}.get(t.hour)
    if name is None or t.minute:
        raise ValueError("서비스 발표는 02시·17시입니다 (예: '2026-10-24 02')")
    return name, t


def _iso(t: Any) -> str | None:
    """한국 시각 → 시간대가 붙은 문자열 (DB timestamptz)"""
    if t is None:
        return None
    return pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%S+09:00")


# ── 수집 기록 ───────────────────────────────────────────────────────────
@dataclass
class RunLog:
    entries: list[dict] = field(default_factory=list)
    raw: list[tuple[str, bytes]] = field(default_factory=list)  # (이름, 응답 원문) — 시험 실행 결과 파일용

    def add(
        self, clock: Clock, kind: str, target: str, attempt: int, res: dict, status: str, note: str = ""
    ) -> None:
        self.entries.append(
            dict(
                logged_at=f"{clock.now():%Y-%m-%d %H:%M:%S}",
                kind=kind,
                target=target,
                attempt=attempt,
                http=res.get("http"),
                code=res.get("code"),
                msg=res.get("msg"),
                n_items=len(res.get("items", [])),
                total=res.get("total"),
                status=status,
                elapsed_s=res.get("elapsed_s"),
                note=kma.mask_key(note or res.get("error", ""))[:200],
            )
        )


# ── 단기예보 받기 ───────────────────────────────────────────────────────
class ForecastCollector:
    """격자 하나의 단기예보를 받는다. 받은 발표는 계산에 쓸 항목만(usable_items) 모아 둔다."""

    def __init__(self, key: str, nx: int, ny: int, http: kma.Http, clock: Clock, log: RunLog):
        self.key, self.nx, self.ny, self.http, self.clock, self.log = key, int(nx), int(ny), http, clock, log
        self.issues: dict[pd.Timestamp, pd.DataFrame] = {}
        self.complete8: dict[pd.Timestamp, bool] = {}
        self.raw_pages: dict[pd.Timestamp, list[bytes]] = {}  # 계산에 쓴 발표의 응답 원문 (Storage 보관용)
        self.fatal = ""

    def get_issue(self, issue: Any, attempt: int = 1) -> tuple[str, dict | None]:
        """발표 1개를 한 번 받아 완결성 점검. 반환: (상태 ok/partial/incomplete/nodata/retry/fatal, 점검)"""
        issue = pd.Timestamp(issue)
        res = kma.fetch_vilage(self.key, issue, self.nx, self.ny, self.http)
        for page, body in res["bodies"]:
            self.log.raw.append((f"fcst_{self.nx}_{self.ny}_{issue:%Y%m%d_%H%M}_p{page}", body))
        chk, status = None, res["status"]
        if res["status"] == "ok":
            df = items_frame(res["items"])
            chk = check_issue(df, issue)
            status = "ok" if chk["complete8"] else ("partial" if chk["required_ok"] else "incomplete")
            if chk["required_ok"] and not self.complete8.get(issue):
                self.issues[issue] = usable_items(df, issue)
                self.complete8[issue] = chk["complete8"]
                self.raw_pages[issue] = [body for _, body in res["bodies"]]
        if status == "fatal":
            self.fatal = f"[{res.get('code') or res.get('http')}] {res.get('msg') or res.get('error')}"
        note = "; ".join(chk["missing"]) if chk and chk["missing"] else ""
        self.log.add(self.clock, "fcst", f"{issue:%Y-%m-%d %H:%M}", attempt, res, status, note)
        return status, chk

    def catch_up(self, slot: Any) -> int:
        """앞선 CATCHUP_H 시간 안에 아직 받지 않은 발표를 한 번씩(가장 최근부터)"""
        slot = pd.Timestamp(slot)
        got = 0
        for back in range(3, CATCHUP_H + 1, 3):
            iss = slot - pd.Timedelta(hours=back)
            if iss.hour not in ISSUE_HOURS or iss in self.issues or self.fatal:
                continue
            st, _ = self.get_issue(iss, attempt=1)
            got += st in ("ok", "partial")
        return got

    def collect_service(self, issue: Any, wait: bool = True) -> dict:
        """서비스 발표를 마감까지 받기. wait=False면 지금 한 번만(시험 실행).
        반환 dict(received_at, complete8, required_ok, attempts, last, late)"""
        issue = pd.Timestamp(issue)
        start, deadline = slot_times(issue)
        out = dict(received_at=None, complete8=False, required_ok=False, attempts=0, last="", late=False)
        now = self.clock.now()
        offsets: list[int | None] = [o for o in RETRY_MIN if start + pd.Timedelta(minutes=o) <= deadline]
        if now > deadline:  # 마감 뒤에 실행: 한 번만(지연 수집)
            offsets, out["late"] = [None], True
        elif not wait:
            offsets = [None]
        caught = False
        for o in offsets:
            if o is not None:
                t_try = start + pd.Timedelta(minutes=o)
                now = self.clock.now()
                if now < t_try:
                    self.clock.sleep((t_try - now).total_seconds())
                elif now > deadline:
                    break
            out["attempts"] += 1
            st, chk = self.get_issue(issue, out["attempts"])
            out["last"] = st
            if chk is not None and chk["required_ok"]:
                out["required_ok"] = True
                if out["received_at"] is None:
                    out["received_at"] = self.clock.now()
            if st == "ok":
                out.update(complete8=True, received_at=self.clock.now())
            if not caught:  # 첫 시도 뒤: 직전 발표를 먼저 확보
                self.catch_up(issue)
                caught = True
            if st in ("ok", "fatal"):
                break
        if not caught:
            self.catch_up(issue)
        return out

    def raw_path(self, issue: Any) -> str:
        """Storage 버킷 raw 안의 위치: fcst/<격자>/<연>/<월>/<발표>.json.gz
        발표마다 한 파일이고, 다시 받으면 덮어쓴다."""
        issue = pd.Timestamp(issue)
        return f"fcst/{self.nx}_{self.ny}/{issue:%Y}/{issue:%m}/{issue:%Y%m%d_%H%M}.json.gz"

    def raw_files(self) -> dict[str, bytes]:
        """계산에 쓴 발표마다 응답 원문(여러 쪽)을 한 파일로 압축.
        단기예보 API는 지난 하루치만 주므로 여기 쌓아 둔다."""
        out = {}
        for issue, pages in self.raw_pages.items():
            doc = dict(
                issue=f"{issue:%Y-%m-%d %H:%M}",
                grid=f"{self.nx}_{self.ny}",
                pages=[
                    p.decode("utf-8", "replace") if isinstance(p, bytes | bytearray) else str(p)
                    for p in pages
                ],
            )
            out[self.raw_path(issue)] = gzip.compress(
                json.dumps(doc, ensure_ascii=False).encode("utf-8"), mtime=0
            )
        return out

    def archive(self) -> ForecastArchive:
        a = ForecastArchive()
        if self.issues:
            a.add_items(pd.concat([self.issues[k] for k in sorted(self.issues)]))
        return a


# ── 결과 분류 ───────────────────────────────────────────────────────────
def slot_result(col: dict, svc: dict) -> str:
    """회차 결과 (데이터 계약 forecast_runs.result 다섯 가지). cropwater slot_result와 같은 순서.
    cropwater의 '백업: 직전 발표 (S3)'는 '백업: 직전 발표'로 두고 일사 방식 칸(rs_method)에 S3를 남긴다."""
    if not svc.get("ok"):
        return "실패"
    if col.get("late") and col.get("required_ok") and not svc.get("backup"):
        return "지연 수집"
    if col.get("complete8") and not svc.get("backup") and svc.get("rs_method") == "S4":
        return "정시 성공"
    if svc.get("backup"):
        return "백업: 직전 발표"
    return "백업: S3"


# ── 한 지점 계산 ────────────────────────────────────────────────────────
def _jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, dt.date | pd.Timestamp):
            out[k] = v.isoformat()[:10] if not isinstance(v, pd.Timestamp) else v.date().isoformat()
        elif isinstance(v, float) and v != v:  # NaN
            out[k] = None
        else:
            out[k] = v
    return out


WB_COLS = (
    "kc",
    "eto_mm",
    "eto_src",
    "etc_mm",
    "rain_mm",
    "irr_net_mm",
    "dr_prev_mm",
    "ks",
    "etc_adj_mm",
    "dp_mm",
    "dr_mm",
    "status",
    "filled",
    "calc_version",
)
WEATHER_COLS = (
    "tmax_c",
    "tmin_c",
    "tavg_c",
    "rh_mean_pct",
    "rh_min_pct",
    "wind_ms",
    "u2_ms",
    "sunshine_h",
    "rs_mj",
    "rain_mm",
    "eto_mm",
    "filled",
)


@dataclass
class SlotResult:
    run_name: str
    issue: pd.Timestamp
    forecast_runs: list[dict] = field(default_factory=list)
    weather_daily: list[dict] = field(default_factory=list)
    water_balance_daily: list[dict] = field(default_factory=list)
    water_outlook: list[dict] = field(default_factory=list)
    log: RunLog = field(default_factory=RunLog)
    stations: list[dict] = field(default_factory=list)  # 지점별 요약 (사람이 읽는 보고)
    raw_files: dict[str, bytes] = field(default_factory=dict)  # Storage 버킷 raw 에 올릴 파일
    problems: list[str] = field(default_factory=list)  # 계약 검사·올리기에서 생긴 문제
    uploaded: bool = False

    @property
    def failed(self) -> bool:
        return (
            any(r["result"] == "실패" for r in self.forecast_runs)
            or any(not s["asos_ok"] for s in self.stations)
            or bool(self.problems)
        )


ASOS_RETRY_S = (20, 60)  # ASOS 일시 오류(시간 초과·서버 오류)면 20초, 60초 뒤 다시 (모두 3번)


def fetch_weather(key, stn_id, st, start, end, http, clock, log) -> tuple[list[dict], dict]:
    """ASOS start~end → (일 기상값 목록, ASOS 조회 결과). 일시 오류(retry)는 두 번 더 시도한다"""
    for attempt in range(1, len(ASOS_RETRY_S) + 2):
        res = kma.fetch_asos_days(key, stn_id, start, end, http)
        for page, body in res["bodies"]:
            log.raw.append((f"asos_{stn_id}_{start:%Y%m%d}_{end:%Y%m%d}_a{attempt}_p{page}", body))
        log.add(clock, "asos", f"{stn_id} {start}~{end}", attempt, res, res["status"])
        if res["status"] != "retry" or attempt > len(ASOS_RETRY_S):
            break
        clock.sleep(ASOS_RETRY_S[attempt - 1])
    items = [it for it in res["items"] if start <= dt.date.fromisoformat(str(it.get("tm", ""))[:10]) <= end]
    return daily_weather(items, station_meta(st)), res


def run_station(
    name: str,
    issue: pd.Timestamp,
    stn_id: str,
    d: Dictionaries,
    weather: list[dict],
    col: dict | None,
    collector: ForecastCollector | None,
    err_all: pd.DataFrame,
    result: SlotResult,
) -> dict:
    """한 지점의 계산 조건 모두 → 결과 줄. 반환: 지점 요약"""
    st = d.stations[stn_id]
    D = issue.normalize()
    prev = (D - pd.Timedelta(days=1)).date()
    scen = d.scenarios_for(stn_id, D.year)
    summary = dict(stn_id=stn_id, name=st.get("name_ko"), n_weather=len(weather), scenarios=[], error="")
    forecast = collector is not None
    stab = pd.DataFrame()
    if forecast:
        arch = collector.archive()
        stab = service_table(
            arch,
            lat=float(st["lat"]),
            lon=float(st["lon"]),
            start=issue - pd.Timedelta(hours=CATCHUP_H),
            end=issue,
        )
        s3, s4 = load_s3_coef(stn_id), load_s4_coef(stn_id)
        err = pooled_errors(err_all, stn_id)
    svc = dict(ok=forecast, backup=None, rs_method=None, src_run=None, obs_prev=None, error="")
    if forecast and collector.fatal:
        svc.update(ok=False, error=collector.fatal)
    elif forecast and (stab.empty or stab[(stab.run == issue) & (stab.run_name == name)].empty):
        svc.update(ok=False, error="24시간 안에 쓸 수 있는 발표 없음")
    methods: set[str] = set()
    for s in scen:
        crop0 = crop_params(d, s)
        crop = adjust_crop(crop0, stage_climate(weather, crop0), crop_height(d, s))
        soil = soil_of(s)
        days = [w for w in weather if w["date"] >= crop.season_start]
        fill, ft = {}, None
        if forecast and svc["ok"]:
            ft_all = forecast_table(stab, float(st["lat"]), float(st["elev_m"]), s3, s4, crop.kc)
            rows = ft_all[(ft_all.run == issue) & (ft_all.run_name == name)]
            main = rows.sort_values("lead_day").head(MAIN_DAYS)
            if main["ETc_main"].isna().any():
                svc.update(ok=False, error="주 지표 3일 중 예보 ETc가 빈 날 있음")
            else:
                ft = ft_all.drop(index=rows.index[rows["ETc_main"].isna()])  # 대체 발표가 담지 못한 마지막 날
                fill = fill_from_forecast(ft)
        end = prev if forecast and svc["ok"] else None
        wb = observed_water_balance(days, crop, soil, forecast_fill=fill, end=end) if days or end else []
        for r in wb:
            result.water_balance_daily.append(
                _jsonable(dict(scenario_id=s["scenario_id"], date=r["date"], **{k: r[k] for k in WB_COLS}))
            )
        info = dict(scenario_id=s["scenario_id"], kc_mid=round(crop.kc_mid, 4), kc_end=round(crop.kc_end, 4))
        if wb:
            info.update(
                last_date=str(wb[-1]["date"]), dr_mm=round(wb[-1]["dr_mm"], 1), status=wb[-1]["status"]
            )
        if ft is not None and svc["ok"] and wb:
            try:
                owb = wb_frame(wb)
                last = last_observed(owb, D)
                obs_last = last if (last is not None and last < D - pd.Timedelta(days=1)) else None
                ol = service_outlook(
                    ft, owb, soil.taw_mm, soil.raw_mm, soil.ea, err, name, issue, obs_last=obs_last
                )
                days_ = ol["days"]
                methods |= set(days_["rs_method"].dropna())
                svc.update(
                    backup=bool(days_["backup"].iloc[0]), src_run=pd.Timestamp(days_["src_run"].iloc[0])
                )
                svc["obs_prev"] = ol["prev"].date()
                for o in outlook_rows(ol, soil.raw_mm):
                    result.water_outlook.append(
                        _jsonable(
                            dict(
                                run_at=_iso(issue),
                                stn_id=stn_id,
                                collector=COLLECTOR,
                                scenario_id=s["scenario_id"],
                                **o,
                            )
                        )
                    )
                info.update(start_dr_mm=round(ol["start"], 1), need=ol["need"])
            except Exception as e:  # 계산 오류도 회차 실패로 남긴다(조용히 넘어가지 않음)
                svc.update(ok=False, error=f"{type(e).__name__}: {e}"[:200])
        summary["scenarios"].append(info)
    if forecast:
        svc["rs_method"] = "S3" if "S3" in methods else ("S4" if methods else None)
        on_time = bool(
            col["complete8"]
            and col["received_at"] is not None
            and pd.Timestamp(col["received_at"]) <= slot_times(issue)[1]
            and not col.get("late")
        )
        res_name = slot_result(col, svc)
        result.forecast_runs.append(
            dict(
                run_at=_iso(issue),
                run_name=name,
                stn_id=stn_id,
                started_at=col["started_at"],
                finished_at=_iso(collector.clock.now()),
                result=res_name,
                on_time=on_time,
                src_run_at=_iso(svc["src_run"]) if svc.get("backup") else None,
                rs_method=svc["rs_method"],
                obs_prev_date=None if svc["obs_prev"] is None else svc["obs_prev"].isoformat(),
                attempts=max(int(col["attempts"]), 1),
                collector=COLLECTOR,
                raw_path=_raw_path(collector, svc.get("src_run") or issue),
                error=(svc["error"] or None),
            )
        )
        summary.update(result=res_name, on_time=on_time, attempts=col["attempts"], error=svc["error"])
    return summary


def _raw_path(collector: "ForecastCollector", issue: Any) -> str | None:
    issue = pd.Timestamp(issue)
    return collector.raw_path(issue) if issue in collector.raw_pages else None


def run_slot(
    name: str,
    issue: Any,
    d: Dictionaries,
    key: str,
    http: kma.Http = kma.http_get,
    clock: Clock | None = None,
    stations: list[str] | None = None,
    wait: bool = True,
) -> SlotResult:
    """한 회차 전체. stations: 돌릴 지점(없으면 사전의 모든 지점)"""
    clock = clock or Clock()
    issue = pd.Timestamp(issue)
    result = SlotResult(run_name=name, issue=issue)
    D = issue.normalize()
    prev = (D - pd.Timedelta(days=1)).date()
    fetched_at = _iso(clock.now())
    err_all = load_error_rows()
    ids = stations or sorted(d.stations, key=lambda k: d.stations[k].get("sort_order", 0))
    for stn_id in ids:
        st = d.stations[stn_id]
        scen = d.scenarios_for(stn_id, D.year)
        collector, col = None, None
        if st.get("has_forecast") and st.get("grid_nx") is not None:
            started = _iso(clock.now())
            collector = ForecastCollector(key, st["grid_nx"], st["grid_ny"], http, clock, result.log)
            col = collector.collect_service(issue, wait=wait)
            col["started_at"] = started
        start = min((dt.date.fromisoformat(str(s["season_start"])[:10]) for s in scen), default=prev)
        weather, res = (
            fetch_weather(key, stn_id, st, start, prev, http, clock, result.log)
            if start <= prev
            else ([], {})
        )
        if collector is not None and collector.fatal:
            weather = []
        for w in weather:
            result.weather_daily.append(
                _jsonable(
                    dict(
                        stn_id=stn_id,
                        date=w["date"],
                        **{k: w[k] for k in WEATHER_COLS},
                        fetched_at=fetched_at,
                        calc_version=CALC_VERSION,
                    )
                )
            )
        summary = run_station(name, issue, stn_id, d, weather, col, collector, err_all, result)
        if collector is not None:
            result.raw_files.update(collector.raw_files())
        summary["asos_ok"] = res.get("status") == "ok" if res else True
        summary["asos_last"] = str(weather[-1]["date"]) if weather else None
        summary["asos_status"] = res.get("status") if res else "조회 안 함"
        result.stations.append(summary)
    return result


# ── 파일로 남기기 ───────────────────────────────────────────────────────
def summary_text(r: SlotResult) -> str:
    lines = [f"# 물 수집 {r.run_name} {r.issue:%Y-%m-%d %H:%M} (계산 {CALC_VERSION})", ""]
    for s in r.stations:
        head = f"- {s['stn_id']} {s.get('name') or ''}: ASOS {s['asos_status']} (마지막 {s['asos_last']})"
        if "result" in s:
            head += f", 예보 {s['result']} (정시 {s['on_time']}, 시도 {s['attempts']})"
        if s.get("error"):
            head += f", 오류: {s['error']}"
        lines.append(head)
        for c in s["scenarios"]:
            lines.append(
                f"  - {c['scenario_id']}: {c.get('last_date')} Dr {c.get('dr_mm')} mm {c.get('status')}"
                + (f", 전망 출발 {c['start_dr_mm']} mm, 관수 필요 예상 {c['need']}" if "need" in c else "")
                + f" (Kc 중기 {c['kc_mid']}, 후기 {c['kc_end']})"
            )
    if r.problems:
        lines += ["", "## 문제"] + [f"- {x}" for x in r.problems]
    lines += [
        "",
        f"DB 올리기: {'완료' if r.uploaded else '안 함'}",
        f"줄 수: forecast_runs {len(r.forecast_runs)}, weather_daily {len(r.weather_daily)}, "
        f"water_balance_daily {len(r.water_balance_daily)}, water_outlook {len(r.water_outlook)}",
    ]
    return "\n".join(lines) + "\n"


def write_outputs(r: SlotResult, out_dir: Path) -> Path:
    d = out_dir / f"{r.issue:%Y%m%d_%H%M}"
    d.mkdir(parents=True, exist_ok=True)
    for t in ("forecast_runs", "weather_daily", "water_balance_daily", "water_outlook"):
        (d / f"{t}.json").write_text(
            json.dumps(getattr(r, t), ensure_ascii=False, indent=1), encoding="utf-8"
        )
    (d / "collect_log.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in r.log.entries), encoding="utf-8"
    )
    (d / "summary.md").write_text(summary_text(r), encoding="utf-8")
    raw = d / "raw"
    raw.mkdir(exist_ok=True)
    for name, body in r.log.raw:
        with gzip.open(raw / f"{name}.json.gz", "wb") as f:
            f.write(body if isinstance(body, bytes | bytearray) else str(body).encode("utf-8"))
    return d


# ── DB에 올리기 (물3) ───────────────────────────────────────────────────
RUN_KEY = ("run_at", "stn_id", "collector")  # water_outlook 줄에 run_id 대신 붙여 둔 회차 열쇠


def check_contract(r: SlotResult, taw_by_scenario: dict[str, float]) -> list[str]:
    """네 표를 계약 v1.2로 검사. 어긋난 곳 설명 목록"""
    return (
        check_rows("forecast_runs", r.forecast_runs)
        + check_rows("weather_daily", r.weather_daily)
        + check_rows("water_balance_daily", r.water_balance_daily, taw_by_scenario)
        + check_rows("water_outlook", r.water_outlook, taw_by_scenario, ignore=("run_id",), extra=RUN_KEY)
    )


def _run_key(row: dict) -> tuple:
    return (pd.Timestamp(row["run_at"]), str(row["stn_id"]), row["collector"])


def publish(r: SlotResult, writer: Any, taw_by_scenario: dict[str, float]) -> SlotResult:
    """계약 검사 → 원자료(Storage) → weather_daily → water_balance_daily → forecast_runs → water_outlook.
    계약 검사에 걸리면 계산 결과는 올리지 않고, 회차 기록만 '실패'와 이유를 담아 올린다.
    원자료 올리기가 실패하면 기록에 남기고 raw_path를 비운 채 계속한다(계산 결과가 더 중요)."""
    problems = check_contract(r, taw_by_scenario)
    if problems:
        r.problems += problems
        reason = ("계약 검사: " + problems[0])[:500]
        runs = [dict(x, result="실패", on_time=False, error=reason) for x in r.forecast_runs]
        if runs and not check_rows("forecast_runs", runs):
            writer.upsert("forecast_runs", runs, SPECS["forecast_runs"].key)
        return r
    for path, data in sorted(r.raw_files.items()):
        try:
            writer.upload("raw", path, data)
        except RuntimeError as e:
            r.problems.append(str(e)[:300])
            for x in r.forecast_runs:
                if x["raw_path"] == path:
                    x["raw_path"] = None
    writer.upsert("weather_daily", r.weather_daily, SPECS["weather_daily"].key)
    writer.upsert("water_balance_daily", r.water_balance_daily, SPECS["water_balance_daily"].key)
    saved = writer.upsert("forecast_runs", r.forecast_runs, SPECS["forecast_runs"].key, returning=True)
    run_id = {_run_key(x): x["run_id"] for x in saved}
    outlook = []
    for o in r.water_outlook:
        k = _run_key(o)
        if k not in run_id:
            raise RuntimeError(f"water_outlook 의 회차 {k} 가 forecast_runs 에 없습니다")
        outlook.append({"run_id": run_id[k], **{c: v for c, v in o.items() if c not in RUN_KEY}})
    writer.upsert("water_outlook", outlook, SPECS["water_outlook"].key)
    r.uploaded = True
    return r


def taw_map(d: Dictionaries) -> dict[str, float]:
    return {s["scenario_id"]: float(s["taw_mm"]) for s in d.scenarios}


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"환경변수 {name}가 없습니다 (GitHub Secrets 또는 .env)")
    return v


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="물 수집기 한 회차 (결과는 파일로, DB 쓰기는 물3)")
    p.add_argument("--slot", help="발표시각 '2026-10-24 02' (없으면 지금 회차)")
    p.add_argument("--out", default="out", help="결과 폴더")
    p.add_argument("--stations", help="지점 번호 쉼표 목록 (없으면 전체)")
    p.add_argument("--no-wait", action="store_true", help="재시도 없이 지금 한 번만 받기 (시험 실행)")
    p.add_argument("--write", action="store_true", help="계약 검사를 통과하면 Supabase에 쓰기")
    a = p.parse_args(argv)
    clock = Clock()
    name, issue = parse_slot(a.slot) if a.slot else current_slot(clock.now())
    d = load_dictionaries(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))
    stations = [s.strip() for s in a.stations.split(",")] if a.stations else None
    r = run_slot(name, issue, d, _env("DATA_GO_KR"), clock=clock, stations=stations, wait=not a.no_wait)
    if a.write:
        from croplib_dp.io.supabase_write import SupabaseWriter

        publish(r, SupabaseWriter(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY")), taw_map(d))
    out = write_outputs(r, Path(a.out))
    print(summary_text(r))
    print(f"결과 폴더: {out}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
