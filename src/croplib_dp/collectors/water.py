"""물 수집기: 한 회차(02시 아침·17시 저녁)를 처음부터 끝까지 돌린다.

원본: cropwater `cropwater_ops.py`의 Collector·run_slot·slot_result (feat/cycle02-forecast).
서버(GitHub Actions)는 매번 빈 컴퓨터에서 시작한다.
그래서 현장 PC처럼 받은 예보를 폴더에 쌓지 않고 회차마다 새로 받는다.

  1) 사전 자료 읽기 (Supabase: 지점·작물·작형·재배 방식·토성·계산 조건)
  2) 예보 지점(has_forecast)마다 단기예보 받기
       - 서비스 발표를 발표 + 10분부터 0·5·10·15·20·30·45·60·80분에 다시 시도, 마감은 발표 + 90분
       - 첫 시도 뒤 앞선 24시간 발표 8개를 한 번씩 받음 (직전 발표 대체·관측 결측일 채우기용)
       - 인증·요청 오류(fatal)는 바로 멈춤. 마감 뒤에 실행하면 한 번만 시도(지연 수집)
  3) 지점마다 ASOS 일자료(작기 시작 또는 3/1 ~ 어제) 한 번 조회 → 일 기상값·ETo
  4) 계산 조건마다 Kc 기후보정 → 관측 물수지(빠진 날은 아침 D+0 예보로) → (예보 지점) 3일 전망
     예보 지점(has_forecast)은 토성 12종 작물 공통 물 상태(`soil_water_daily`)도 함께 계산한다.
  4') 관측만 하는 지점(운영 is_active 인데 예보 지점이 아님, Actions 변수 OBS_ALL_STATIONS=true 일 때만,
  결정 0026):
     예보·계산 조건(사과·배추)·forecast_runs 없이 ASOS(1/1~어제)와 토성 12종 작물 공통 물 상태만 계산한다.
     평소 회차는 최근 14일 줄만 올리고(창 시작 전날 Dr 을 DB 줄에서 읽어 이어 계산, 없으면 전체 재계산 +
     경고),
     ASOS 실패는 회차 실패가 아니라 경고(재시도 2번, 단계 20분 상한). 소급(--soil-backfill)은 20곳씩.
  5) DB 표와 같은 모양의 줄(forecast_runs, weather_daily, water_balance_daily, soil_water_daily,
     water_outlook, water_outlook_summary)과 수집 기록
"""

import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
import urllib.parse
from collections.abc import Mapping, Sequence
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
from croplib_dp.core.grid import check_station_grid, forecast_ready
from croplib_dp.core.kc import adjust_crop, stage_climate
from croplib_dp.core.monitor import named
from croplib_dp.core.outlook import (
    MAIN_DAYS,
    fill_from_forecast,
    last_observed,
    outlook_rows,
    outlook_summary,
    pooled_errors,
    service_outlook,
    wb_frame,
)
from croplib_dp.core.retention import ROW_BYTES
from croplib_dp.core.soil_water import MissingPrior, soil_water_rows, window_needs_prior
from croplib_dp.core.water_balance import observed_water_balance
from croplib_dp.io import kma
from croplib_dp.io.coefficients import load_error_rows, load_s3_coef, load_s4_coef
from croplib_dp.io.contract import SPECS, check_rows
from croplib_dp.io.dictionaries import (
    Dictionaries,
    column_exists,
    crop_height,
    crop_params,
    load_dictionaries,
    read_pages,
    read_table,
    rest_get,
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
SOIL_UPLOAD_DAYS = 14  # 평소 회차에서 올리는 최근 일수 (--soil-backfill 이면 그해 1/1부터 전체)
OBS_ASOS_RETRY_S = (30, 90)  # 관측만 하는 지점의 ASOS 재시도(모두 3번): 대표 지점의 (30, 90, 180)보다 짧게
OBS_STAGE_MAX_S = 20 * 60  # 관측만 하는 지점 단계 전체의 시간 상한. 넘으면 남은 지점은 다음 회차로
OBS_BACKFILL_MAX = 20  # 소급은 한 번에 관측만 하는 지점 20곳까지 (사용자 확정 2026-11-08)
OBS_SWITCH = "OBS_ALL_STATIONS"  # Actions 변수: true 일 때만 관측 전 지점(기본 꺼짐)
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
    cropwater의 '백업: 직전 발표 (S3)'는 '백업: 직전 발표'로 두고 일사 방식 칸(rs_method)에 S3를 남긴다.
    svc["rs_base"]: 그 지점의 평소 일사 방식. 하늘상태 계수(S4)가 없는 지점(춘천 밖, 물6)은 S3가 평소 방식이라
    S3로 계산해도 '백업'이 아니다. 없으면 S4(춘천)로 본다."""
    if not svc.get("ok"):
        return "실패"
    if col.get("late") and col.get("required_ok") and not svc.get("backup"):
        return "지연 수집"
    if col.get("complete8") and not svc.get("backup") and svc.get("rs_method") == svc.get("rs_base", "S4"):
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
SOIL_WATER_COLS = (
    "eto_mm",
    "rain_mm",
    "eto_7d_mm",
    "rain_7d_mm",
    "dp_mm",
    "dr_mm",
    "taw_mm",
    "raw_mm",
    "aw_pct",
    "status",
    "days_to_dry",
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
    "rs_source",
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
    soil_water_daily: list[dict] = field(default_factory=list)
    water_outlook: list[dict] = field(default_factory=list)
    water_outlook_summary: list[dict] = field(default_factory=list)
    log: RunLog = field(default_factory=RunLog)
    stations: list[dict] = field(default_factory=list)  # 지점별 요약 (사람이 읽는 보고)
    raw_files: dict[str, bytes] = field(default_factory=dict)  # Storage 버킷 raw 에 올릴 파일
    problems: list[str] = field(default_factory=list)  # 계약 검사·올리기에서 생긴 문제
    warnings: list[str] = field(
        default_factory=list
    )  # 실패는 아니지만 알릴 것(격자 비어 있음·다름 결정 0025, 관측 전용 지점 문제 결정 0026)
    uploaded: bool = False

    @property
    def failed(self) -> bool:
        return (
            any(r["result"] == "실패" for r in self.forecast_runs)
            or any(not s["asos_ok"] for s in self.stations if not s.get("obs_only"))
            or bool(self.problems)
        )


ASOS_RETRY_S = (30, 90, 180)  # ASOS 일시 오류(시간 초과·서버 오류)면 30초·90초·3분 뒤 다시 (모두 4번)


def fetch_weather(
    key, stn_id, st, start, end, http, clock, log, retry_s: Sequence[int] = ASOS_RETRY_S
) -> tuple[list[dict], dict]:
    """ASOS start~end → (일 기상값 목록, ASOS 조회 결과). 일시 오류(retry)는 retry_s 만큼 더 시도한다
    (대표 지점은 30·90·180초 = 세 번 더, 관측만 하는 지점은 30·90초 = 두 번 더)"""
    for attempt in range(1, len(retry_s) + 2):
        res = kma.fetch_asos_days(key, stn_id, start, end, http)
        for page, body in res["bodies"]:
            log.raw.append((f"asos_{stn_id}_{start:%Y%m%d}_{end:%Y%m%d}_a{attempt}_p{page}", body))
        log.add(clock, "asos", f"{stn_id} {start}~{end}", attempt, res, res["status"])
        if res["status"] != "retry" or attempt > len(retry_s):
            break
        clock.sleep(retry_s[attempt - 1])
    items = [it for it in res["items"] if start <= dt.date.fromisoformat(str(it.get("tm", ""))[:10]) <= end]
    return daily_weather(items, station_meta(st)), res


def _soil_season_start(day: dt.date, backfill: bool = False) -> dt.date:
    """작물 공통 물 상태 계산 시작일: 소급(--soil-backfill)이나 1~2월은 그해 1/1, 평소 3~12월은 그해 3/1."""
    return dt.date(day.year, 1, 1) if (backfill or day.month < 3) else dt.date(day.year, 3, 1)


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
    soil_backfill: bool = False,
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
    if forecast:
        svc["rs_base"] = "S4" if len(s4) else "S3"  # 하늘상태 계수가 없는 지점은 S3가 평소 방식
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
        info = dict(
            scenario_id=s["scenario_id"],
            crop_id=s.get("crop_id"),
            is_default=bool(s.get("is_default")),
            kc_mid=round(crop.kc_mid, 4),
            kc_end=round(crop.kc_end, 4),
        )
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
                summ = outlook_summary(ol, soil.raw_mm, crop.kc(_start_day(ol)), crop.kc(D.date()))
                result.water_outlook_summary.append(
                    _jsonable(
                        dict(
                            run_at=_iso(issue),
                            stn_id=stn_id,
                            collector=COLLECTOR,
                            scenario_id=s["scenario_id"],
                            **summ,
                        )
                    )
                )
                info.update(start_dr_mm=round(ol["start"], 1), need=ol["need"])
            except Exception as e:  # 계산 오류도 회차 실패로 남긴다(조용히 넘어가지 않음)
                svc.update(ok=False, error=f"{type(e).__name__}: {e}"[:200])
        summary["scenarios"].append(info)
    if st.get("has_forecast") and weather and d.soils:
        soil_start = _soil_season_start(prev, backfill=soil_backfill)
        soil_days = [w for w in weather if w["date"] >= soil_start]
        cutoff = None if soil_backfill else (prev - dt.timedelta(days=SOIL_UPLOAD_DAYS - 1))
        soils_sorted = sorted(d.soils.values(), key=lambda x: (x.get("sort_order", 0), x["soil_code"]))
        for soil_row in soils_sorted:
            if soil_row.get("fc") is None or soil_row.get("wp") is None:
                continue
            s_rows = soil_water_rows(soil_days, float(soil_row["fc"]), float(soil_row["wp"]))
            for r in s_rows:
                if cutoff is not None and r["date"] < cutoff:
                    continue
                result.soil_water_daily.append(
                    _jsonable(
                        dict(
                            stn_id=stn_id,
                            soil_code=soil_row["soil_code"],
                            date=r["date"],
                            **{k: r[k] for k in SOIL_WATER_COLS},
                        )
                    )
                )
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


def _start_day(ol: dict) -> dt.date:
    return (ol["pre"][-1]["date"] if ol["pre"] else ol["prev"]).date()


def _raw_path(collector: "ForecastCollector", issue: Any) -> str | None:
    issue = pd.Timestamp(issue)
    return collector.raw_path(issue) if issue in collector.raw_pages else None


def collect_stations(d: Dictionaries) -> list[str]:
    """기본으로 도는 지점: 수집 지점(has_forecast). 전 지점으로 넓힐 때(#28)는 has_forecast 만 켜면
    수집·감시·slot_done 이 함께 따라간다 (결정 0007 보충, 0017)."""
    return sorted(
        (k for k, st in d.stations.items() if st.get("has_forecast")),
        key=lambda k: d.stations[k].get("sort_order", 0),
    )


def _is_true(v: Any) -> bool:
    return v is True or str(v).strip().lower() == "true"


def obs_all_enabled(env: Mapping[str, str] | None = None) -> bool:
    """관측 물 계산을 운영 지점 전부로 넓힐지. Actions 변수 OBS_ALL_STATIONS 가 true 일 때만 켜짐(기본 꺼짐).
    비상 정지 변수(RETENTION_AUTO_APPLY)와 반대로 "켜는" 변수다. 새 수집은 승인된 뒤에만 켜져야 한다."""
    env = os.environ if env is None else env
    return str(env.get(OBS_SWITCH, "")).strip().lower() == "true"


def observation_stations(d: Dictionaries) -> list[str]:
    """관측 대상 = 운영 지점(is_active). 폐지·휴지 지점은 새 줄을 쓰지 않는다(결정 0024)."""
    return sorted(
        (k for k, st in d.stations.items() if _is_true(st.get("is_active"))),
        key=lambda k: (d.stations[k].get("sort_order", 0), k),
    )


def observation_only_stations(d: Dictionaries) -> list[str]:
    """관측만 하는 지점 = 운영 지점 중 예보 지점(has_forecast)이 아닌 곳 (작물 공통 12토성만, 결정 D1·D2)."""
    return [k for k in observation_stations(d) if not d.stations[k].get("has_forecast")]


def has_calc_inputs(st: Mapping[str, Any]) -> bool:
    """ETo·물 계산에 필요한 지점 값(위경도·고도·풍속계 높이)이 모두 있나"""
    return all(st.get(c) is not None for c in ("lat", "lon", "elev_m", "wind_height_m"))


def default_soil_code(soils: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]]) -> str | None:
    """감시가 "어제 줄이 있나"를 볼 때 쓰는 토성 하나: 기본(is_default) 토성, 없으면 정렬 첫
    토성."""
    rows = list(soils.values() if isinstance(soils, Mapping) else soils)
    if not rows:
        return None
    rows.sort(key=lambda x: (x.get("sort_order", 0), x["soil_code"]))
    pick = next((x for x in rows if _is_true(x.get("is_default"))), rows[0])
    return pick["soil_code"]


def load_soil_prior(
    base: str, key: str, stations: Sequence[str], day: dt.date, http=rest_get
) -> dict[tuple[str, str], dict]:
    """창 재계산의 시작점: 그 날짜(창 시작 전날)의 soil_water_daily 줄 → {(지점, 토성): {dr_mm, dp_mm,
    filled}}.
    지점 × 토성 줄이 1,000줄을 넘어(97 × 12) 쪽으로 나눠 읽는다. 읽기만 한다."""
    if not stations:
        return {}
    query = (
        f"select=stn_id,soil_code,dr_mm,dp_mm,filled&date=eq.{day}"
        f"&stn_id=in.({','.join(str(s) for s in stations)})&order=stn_id,soil_code"
    )
    out: dict[tuple[str, str], dict] = {}
    for r in read_pages(base, key, "soil_water_daily", http, query):
        num = lambda v: None if v is None else float(v)  # noqa: E731
        out[(str(r["stn_id"]), r["soil_code"])] = dict(
            dr_mm=num(r.get("dr_mm")), dp_mm=num(r.get("dp_mm")), filled=bool(r.get("filled"))
        )
    return out


def rs_source_state(base: str, key: str, http=rest_get) -> tuple[bool, str]:
    """DB weather_daily 에 rs_source 칸이 있나(C7, CropMon 반영 여부). (있음?, 빼고 올릴 때 보고 문구).
    있다고 확인될 때만 올린다. 없거나 확인하지 못하면 그 칸만 빼고 올려 weather_daily 쓰기 전체가
    실패하지 않게 하되
    문구를 요약에 남긴다(조용히 넘어가지 않음)."""
    try:
        if column_exists(base, key, "weather_daily", "rs_source", http):
            return True, ""
        return False, "weather_daily.rs_source 칸이 DB에 없어 그 칸만 빼고 올림 (CropMon C7 반영 대기)"
    except Exception as e:
        return False, f"weather_daily.rs_source 칸이 있는지 확인하지 못해({str(e)[:120]}) 그 칸만 빼고 올림"


@dataclass(frozen=True)
class DispatchOptions:
    stations: list[str] | None  # None = 기본 대상
    soil_backfill: bool
    write: bool
    skip_if_done: bool


def dispatch_options(
    event: str, stations: str = "", backfill: str = "", dry_run: str = ""
) -> DispatchOptions:
    """실행 방식(collect.yml)에서 넘어온 값 → 실제 옵션. **새 입력(지점·소급·시험 실행)은
    workflow_dispatch 에서만 쓴다.**
    예약(schedule) 실행은 입력이 어떻게 와도 지금과 같다(기본 대상, 쓰기, 끝난 회차는 건너뜀).
    지점이나 소급이나 시험 실행을 적은 수동 실행은 끝난 회차를 건너뛰지 않는다(그래야 소급·점검이 돈다)."""
    if event != "workflow_dispatch":
        return DispatchOptions(None, False, True, True)
    ids = [t.strip() for t in str(stations or "").split(",") if t.strip()]
    for t in ids:
        if not re.fullmatch(r"[0-9]{1,5}", t):
            raise SystemExit(f"지점 번호는 숫자만 쉼표로 적습니다: {t!r}")
    bf = str(backfill or "").strip().lower() == "true"
    dry = str(dry_run or "").strip().lower() == "true"
    return DispatchOptions(ids or None, bf, not dry, not (ids or bf or dry))


def check_backfill_scope(
    d: Dictionaries, stations: Sequence[str] | None, soil_backfill: bool, obs_all: bool
) -> None:
    """소급은 관측만 하는 지점 20곳까지(사용자 확정). 지점을 안 적으면 97곳 전부라 멈춘다. 스위치가 꺼져
    있거나
    소급이 아니면 검사하지 않는다."""
    if not (soil_backfill and obs_all):
        return
    only = observation_only_stations(d)
    targets = only if not stations else [s for s in stations if s in set(only)]
    if len(targets) > OBS_BACKFILL_MAX:
        raise SystemExit(
            f"소급은 관측만 하는 지점을 한 번에 {OBS_BACKFILL_MAX}곳까지만 합니다(지금 {len(targets)}곳). "
            f"지점 번호(stations)를 {OBS_BACKFILL_MAX}곳 이하로 적어 주세요"
        )


def _grid_warning(stn_id: str, st: dict, result: SlotResult) -> None:
    """예보 지점의 격자 점검(결정 0025). DB 격자가 비어 있으면 그 지점 예보만 건너뛰고(관측은 계속),
    DB 격자가 계산값과 다르면 DB 값으로 예보를 계속하되 경고한다."""
    c = check_station_grid(st)
    label = f"{stn_id} {st.get('name_ko') or ''}".strip()
    if c.status == "missing":
        calc = f" 계산값은 {c.calc[0]},{c.calc[1]}" if c.calc else ""
        result.warnings.append(
            f"{label}: 예보 격자가 비어 있어 예보를 건너뜀(관측은 계속).{calc} CropMon 격자 시드 필요"
        )
    elif c.status == "differs":
        result.warnings.append(
            f"{label}: DB 격자 {c.db[0]},{c.db[1]}가 계산값 {c.calc[0]},{c.calc[1]}과 다름 "
            "— DB 값으로 예보 계속"
            "(경계 근처 좌표 차이일 수 있음)"
        )


def run_obs_station(
    stn_id: str,
    issue: pd.Timestamp,
    d: Dictionaries,
    key: str,
    http: kma.Http,
    clock: Clock,
    result: SlotResult,
    fetched_at: str | None,
    soil_backfill: bool = False,
    soil_prior: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> tuple[dict, bool]:
    """관측만 하는 지점 하나(결정 0026): ASOS 1/1~어제 한 번 → 일 기상값·ETo → 토성별 작물 공통 물 상태.
    예보·계산 조건(사과·배추)·forecast_runs 는 만들지 않는다(D1·D2).
    평소 회차는 weather_daily·soil_water_daily 를 최근 14일만 올린다. 계산은 1/1부터 받은 기상값 전체로
    하고(월 단위
    일사 규칙·ETo 채우기를 소급 때와 같게), 토양은 창 시작 전날의 DB 줄(soil_prior)에서 이어 계산한다.
    시작점이
    없는 토성이 있으면 그 지점은 전체 재계산(올리는 건 창만)하고 반환값 둘째(시작점 없음)가 True 다.
    반환: (지점 요약, 시작점 없음)"""
    st = d.stations[stn_id]
    prev = (issue.normalize() - pd.Timedelta(days=1)).date()
    win_start = prev - dt.timedelta(days=SOIL_UPLOAD_DAYS - 1)
    start = dt.date(prev.year, 1, 1)
    cutoff = None if soil_backfill else win_start
    summary = dict(
        stn_id=stn_id, name=st.get("name_ko"), n_weather=0, scenarios=[], error="", obs_only=True, n_soil=0
    )
    weather, res = fetch_weather(
        key, stn_id, st, start, prev, http, clock, result.log, retry_s=OBS_ASOS_RETRY_S
    )
    for w in weather:
        if cutoff is not None and w["date"] < cutoff:
            continue
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
    prior_missing = False
    if weather and d.soils:
        needs_prior = (not soil_backfill) and window_needs_prior(win_start, prev)
        soils_sorted = sorted(d.soils.values(), key=lambda x: (x.get("sort_order", 0), x["soil_code"]))
        for soil_row in soils_sorted:
            if soil_row.get("fc") is None or soil_row.get("wp") is None:
                continue
            fc, wp = float(soil_row["fc"]), float(soil_row["wp"])
            s_rows = None
            if needs_prior:
                pr = (soil_prior or {}).get((stn_id, soil_row["soil_code"]))
                if pr is not None:
                    try:
                        s_rows = soil_water_rows(weather, fc, wp, window_start=win_start, prior=pr)
                    except MissingPrior:
                        s_rows = None
                if s_rows is None:
                    prior_missing = True
            if s_rows is None:  # 시작점이 필요 없거나(3/1 시작·겨울·소급) 없어서 1/1부터 전체 재계산
                s_rows = [
                    r for r in soil_water_rows(weather, fc, wp) if cutoff is None or r["date"] >= cutoff
                ]
            for r in s_rows:
                result.soil_water_daily.append(
                    _jsonable(
                        dict(
                            stn_id=stn_id,
                            soil_code=soil_row["soil_code"],
                            date=r["date"],
                            **{k: r[k] for k in SOIL_WATER_COLS},
                        )
                    )
                )
                summary["n_soil"] += 1
    summary.update(
        n_weather=len(weather),
        asos_ok=res.get("status") == "ok",
        asos_last=str(weather[-1]["date"]) if weather else None,
        asos_status=res.get("status"),
    )
    return summary, prior_missing


def _obs_warnings(
    d: Dictionaries, result: SlotResult, skipped: dict[str, list[str]], prior_day: dt.date
) -> None:
    """관측만 하는 지점 단계의 경고를 종류별로 한 줄씩(지점이 많아도 줄 수가 늘지 않게)."""
    names = {k: str(st.get("name_ko") or "") for k, st in d.stations.items()}
    if skipped["no_input"]:
        result.warnings.append(
            f"관측 전용 {len(skipped['no_input'])}곳은 계산 입력(위경도·고도·풍속계 높이)이 비어 건너뜀 — "
            f"{named(skipped['no_input'], names)}: stations 표 확인 필요"
        )
    if skipped["cap"]:
        result.warnings.append(
            f"관측 전용 단계가 {OBS_STAGE_MAX_S // 60}분 상한을 넘어 {len(skipped['cap'])}곳을 건너뜀 — "
            f"{named(skipped['cap'], names)}: 다음 회차가 다시 받음"
        )
    failed = [s["stn_id"] for s in result.stations if s.get("obs_only") and not s.get("asos_ok", True)]
    if failed:
        result.warnings.append(
            f"관측 전용 ASOS 실패 {len(failed)}곳 — {named(failed, names)}: 회차 실패로 세지 않음"
            "(감시가 표 최신성으로 알림)"
        )
    if skipped["prior"]:
        result.warnings.append(
            f"관측 전용 {len(skipped['prior'])}곳: 창 시작 전날({prior_day})의 Dr 이 DB에 없어 "
            f"1/1부터 전체 재계산함(올리는 건 최근 {SOIL_UPLOAD_DAYS}일만) — "
            f"{named(skipped['prior'], names)}: 소급이 필요합니다"
        )


def run_slot(
    name: str,
    issue: Any,
    d: Dictionaries,
    key: str,
    http: kma.Http = kma.http_get,
    clock: Clock | None = None,
    stations: list[str] | None = None,
    wait: bool = True,
    soil_backfill: bool = False,
    obs_all: bool = False,
    soil_prior: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> SlotResult:
    """한 회차 전체. stations: 돌릴 지점(없으면 수집 지점 has_forecast. 감시·slot_done 과 같은 기준).
    obs_all: 켜면(OBS_ALL_STATIONS=true) 운영 지점 중 예보 지점이 아닌 곳을 "관측만 하는 지점"으로 뒤에
    더하고,
    stations 에 적힌 그런 지점도 관측만 하는 규칙으로 돈다(결정 0026). 꺼져 있으면 예전과 똑같다.
    soil_prior: 관측만 하는 지점의 창 시작 전날 DB 줄 {(지점, 토성): {dr_mm, dp_mm, filled}} (평소 회차)."""
    clock = clock or Clock()
    issue = pd.Timestamp(issue)
    result = SlotResult(run_name=name, issue=issue)
    D = issue.normalize()
    prev = (D - pd.Timedelta(days=1)).date()
    fetched_at = _iso(clock.now())
    err_all = load_error_rows()
    obs_only = observation_only_stations(d) if obs_all else []
    obs_set = set(obs_only)
    ids = stations or (collect_stations(d) + obs_only)
    if not ids:
        result.problems.append("수집 지점이 0곳 — 지점 사전 has_forecast 확인")
    skipped: dict[str, list[str]] = dict(no_input=[], cap=[], prior=[])
    obs_t0 = None
    for stn_id in ids:
        st = d.stations[stn_id]
        if stn_id in obs_set:
            if not has_calc_inputs(st):
                skipped["no_input"].append(stn_id)
                continue
            obs_t0 = obs_t0 or clock.now()
            if (clock.now() - obs_t0).total_seconds() > OBS_STAGE_MAX_S:
                skipped["cap"].append(stn_id)
                continue
            summary, prior_missing = run_obs_station(
                stn_id, issue, d, key, http, clock, result, fetched_at, soil_backfill, soil_prior
            )
            if prior_missing:
                skipped["prior"].append(stn_id)
            result.stations.append(summary)
            continue
        scen = d.scenarios_for(stn_id, D.year)
        collector, col = None, None
        if st.get("has_forecast"):
            _grid_warning(stn_id, st, result)
        if forecast_ready(st):
            started = _iso(clock.now())
            collector = ForecastCollector(key, st["grid_nx"], st["grid_ny"], http, clock, result.log)
            col = collector.collect_service(issue, wait=wait)
            col["started_at"] = started
        starts = [dt.date.fromisoformat(str(s["season_start"])[:10]) for s in scen]
        if st.get("has_forecast") and d.soils:
            starts.append(_soil_season_start(prev, backfill=soil_backfill))
        start = min(starts, default=prev)
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
        summary = run_station(
            name, issue, stn_id, d, weather, col, collector, err_all, result, soil_backfill=soil_backfill
        )
        if collector is not None:
            result.raw_files.update(collector.raw_files())
        summary["asos_ok"] = res.get("status") == "ok" if res else True
        summary["asos_last"] = str(weather[-1]["date"]) if weather else None
        summary["asos_status"] = res.get("status") if res else "조회 안 함"
        result.stations.append(summary)
    if obs_set:
        _obs_warnings(d, result, skipped, prev - dt.timedelta(days=SOIL_UPLOAD_DAYS))
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
        if (
            not s.get("asos_ok", True) or s.get("result") == "실패"
        ):  # 실패한 조회의 응답을 한 줄씩 (원인 찾기용)
            for kind in ("asos", "fcst"):
                e = next(
                    (e for e in reversed(r.log.entries) if e["kind"] == kind and e["status"] != "ok"), None
                )
                if e is not None and (
                    kind == "asos" and e["target"].startswith(s["stn_id"]) or kind == "fcst"
                ):
                    lines.append(
                        f"  ! {kind} {e['target']} 시도 {e['attempt']}: {e['status']} "
                        f"HTTP {e['http']} 코드 {e['code']} {e['msg'] or ''} {e['note'] or ''}".rstrip()
                    )
        for c in s["scenarios"]:
            lines.append(
                f"  - {c['scenario_id']}: {c.get('last_date')} Dr {c.get('dr_mm')} mm {c.get('status')}"
                + (f", 전망 출발 {c['start_dr_mm']} mm, 관수 필요 예상 {c['need']}" if "need" in c else "")
                + f" (Kc 중기 {c['kc_mid']}, 후기 {c['kc_end']})"
            )
    if r.problems:
        lines += ["", "## 문제"] + [f"- {x}" for x in r.problems]
    if r.warnings:
        lines += ["", "## 경고"] + [f"- {x}" for x in r.warnings]
    lines += [
        "",
        f"DB 올리기: {'완료' if r.uploaded else '안 함'}",
        f"줄 수: forecast_runs {len(r.forecast_runs)}, weather_daily {len(r.weather_daily)}, "
        f"water_balance_daily {len(r.water_balance_daily)}, soil_water_daily {len(r.soil_water_daily)}, "
        f"water_outlook {len(r.water_outlook)}, water_outlook_summary {len(r.water_outlook_summary)}",
        growth_text(r),
    ]
    return "\n".join(lines) + "\n"


def growth_text(r: SlotResult) -> str:
    """관측 물 상태 줄이 DB를 얼마나 늘리나(전부 새 줄일 때의 상한, 줄당 실측 크기 × 줄 수). 소급
    dry-run 보고용.
    이미 있는 줄은 덮어쓰므로 실제 증가는 이보다 작거나 같다."""
    n_soil, n_wx = len(r.soil_water_daily), len(r.weather_daily)
    mb = (n_soil * ROW_BYTES["soil_water_daily"] + n_wx * ROW_BYTES["weather_daily"]) / 1024 / 1024
    return (
        f"예상 DB 증가(전부 새 줄일 때의 상한): {mb:.1f}MB "
        f"(soil_water_daily {n_soil}줄, weather_daily {n_wx}줄)"
    )


CROP_KO = {"apple": "사과", "kimchi_cabbage": "배추"}


def brief_text(r: SlotResult) -> str:
    """짧은 요약: 지점마다 한 줄, 기본 조건(작물마다 기본 재배 방식)만. 실행 화면 알림(annotation)은
    약 4KB 에서 잘리므로, 지점이 10곳이어도 들어가게 한다. 전체는 summary.md 에 있다."""
    lines = [f"# 물 수집 {r.run_name} {r.issue:%Y-%m-%d %H:%M} (계산 {CALC_VERSION})"]
    if r.problems:
        lines += [f"! {x}"[:300] for x in r.problems[:5]]
    if r.warnings:
        lines += [f"? {x}"[:300] for x in r.warnings[:5]]
    obs = [s for s in r.stations if s.get("obs_only")]
    for s in r.stations:
        if s.get("obs_only"):  # 97곳이 되면 지점마다 한 줄은 알림 칸(약 4KB)에 안 들어간다 → 한 줄로 모음
            continue
        last = str(s.get("asos_last") or "")[5:] or "없음"
        parts = [s.get("result", "관측만"), f"ASOS ~{last}"]
        for c in s.get("scenarios", []):
            if not c.get("is_default"):
                continue
            crop = CROP_KO.get(c.get("crop_id"), c.get("crop_id") or "")
            p = f"{crop} {c.get('status', '?')}"
            if c.get("dr_mm") is not None and c.get("status") != "dormant":
                p += f" {c['dr_mm']:.0f}mm"
            need = (c.get("need") or {}).get("center")
            if need is not None:
                p += f" 예상일 {need}"
            parts.append(p)
        head = f"- {s['stn_id']} {s.get('name') or ''}: " + " · ".join(parts)
        if s.get("error"):
            head += f" · 오류: {s['error']}"[:200]
        lines.append(head)
    if obs:
        bad = sum(1 for s in obs if not s.get("asos_ok", True))
        lines.append(
            f"- 관측 전용 {len(obs)}곳: ASOS 성공 {len(obs) - bad} · 실패 {bad} · "
            f"토양 {sum(s.get('n_soil', 0) for s in obs)}줄"
        )
    lines.append(
        f"DB 올리기: {'완료' if r.uploaded else '안 함'} · 전망 {len(r.water_outlook)}줄 · "
        f"요약 {len(r.water_outlook_summary)}줄 · 물수지 {len(r.water_balance_daily)}줄"
    )
    return "\n".join(lines) + "\n"


def write_outputs(r: SlotResult, out_dir: Path) -> Path:
    d = out_dir / f"{r.issue:%Y%m%d_%H%M}"
    d.mkdir(parents=True, exist_ok=True)
    for t in (
        "forecast_runs",
        "weather_daily",
        "water_balance_daily",
        "soil_water_daily",
        "water_outlook",
        "water_outlook_summary",
    ):
        (d / f"{t}.json").write_text(
            json.dumps(getattr(r, t), ensure_ascii=False, indent=1), encoding="utf-8"
        )
    (d / "collect_log.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in r.log.entries), encoding="utf-8"
    )
    (d / "summary.md").write_text(summary_text(r), encoding="utf-8")
    (d / "brief.md").write_text(brief_text(r), encoding="utf-8")
    raw = d / "raw"
    raw.mkdir(exist_ok=True)
    for name, body in r.log.raw:
        with gzip.open(raw / f"{name}.json.gz", "wb") as f:
            f.write(body if isinstance(body, bytes | bytearray) else str(body).encode("utf-8"))
    return d


# ── DB에 올리기 (물3) ───────────────────────────────────────────────────
RUN_KEY = ("run_at", "stn_id", "collector")  # water_outlook 줄에 run_id 대신 붙여 둔 회차 열쇠


def check_contract(
    r: SlotResult, taw_by_scenario: dict[str, float], rs_source_column: bool = True
) -> list[str]:
    """여섯 표를 계약 v1.5로 검사. 어긋난 곳 설명 목록.
    rs_source_column: DB에 weather_daily.rs_source 칸이 있나. 없으면 그 칸은 검사에서 뺀다(올리기 전에
    빼므로)."""
    return (
        check_rows("forecast_runs", r.forecast_runs)
        + check_rows("weather_daily", r.weather_daily, ignore=() if rs_source_column else ("rs_source",))
        + check_rows("water_balance_daily", r.water_balance_daily, taw_by_scenario)
        + check_rows("soil_water_daily", r.soil_water_daily)
        + check_rows("water_outlook", r.water_outlook, taw_by_scenario, ignore=("run_id",), extra=RUN_KEY)
        + check_rows(
            "water_outlook_summary",
            r.water_outlook_summary,
            taw_by_scenario,
            ignore=("run_id",),
            extra=RUN_KEY,
        )
    )


def _run_key(row: dict) -> tuple:
    return (pd.Timestamp(row["run_at"]), str(row["stn_id"]), row["collector"])


def publish(
    r: SlotResult,
    writer: Any,
    taw_by_scenario: dict[str, float],
    rs_source_column: bool = True,
    rs_source_note: str = "",
) -> SlotResult:
    """계약 검사 → 원자료(Storage) → weather_daily → water_balance_daily → soil_water_daily
    → forecast_runs → water_outlook(·요약).
    rs_source_column=False(DB에 칸이 없거나 있는지 모름)이면 weather_daily 에서 그 칸만 빼고 올리고
    경고를 남긴다
    (칸이 없는 채로 올리면 PostgREST 가 알 수 없는 칸이라며 거절해 weather_daily 쓰기 전체가 실패한다).
    계약 검사에 걸리면 계산 결과는 올리지 않고, 회차 기록만 '실패'와 이유를 담아 올린다.
    원자료 올리기가 실패하면 기록에 남기고 raw_path를 비운 채 계속한다(계산 결과가 더 중요)."""
    problems = check_contract(r, taw_by_scenario, rs_source_column)
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
    weather = r.weather_daily
    if not rs_source_column:
        weather = [{k: v for k, v in w.items() if k != "rs_source"} for w in r.weather_daily]
        r.warnings.append(
            rs_source_note or "weather_daily.rs_source 칸을 빼고 올림 (DB에 칸이 없다고 알려 줌)"
        )
    writer.upsert("weather_daily", weather, SPECS["weather_daily"].key)
    writer.upsert("water_balance_daily", r.water_balance_daily, SPECS["water_balance_daily"].key)
    if r.soil_water_daily:
        writer.upsert("soil_water_daily", r.soil_water_daily, SPECS["soil_water_daily"].key)
    saved = writer.upsert("forecast_runs", r.forecast_runs, SPECS["forecast_runs"].key, returning=True)
    run_id = {_run_key(x): x["run_id"] for x in saved}
    for table in ("water_outlook", "water_outlook_summary"):
        rows = []
        for o in getattr(r, table):
            k = _run_key(o)
            if k not in run_id:
                raise RuntimeError(f"{table} 의 회차 {k} 가 forecast_runs 에 없습니다")
            rows.append({"run_id": run_id[k], **{c: v for c, v in o.items() if c not in RUN_KEY}})
        writer.upsert(table, rows, SPECS[table].key)
    r.uploaded = True
    return r


def taw_map(d: Dictionaries) -> dict[str, float]:
    return {s["scenario_id"]: float(s["taw_mm"]) for s in d.scenarios}


def slot_done(base: str, key: str, d: Dictionaries, issue: Any, http=rest_get) -> bool:
    """이 회차가 이미 '정시 성공'으로 끝났나 (예보 지점 모두).
    예약을 여러 번 걸어 둔 실행이 같은 일을 다시 하지 않게 한다.
    정시 성공이 아니면(지연·백업·실패) 다시 돈다 — 늦게라도 서비스 발표를 받으면 결과가 나아진다.
    **관측만 하는 지점(OBS_ALL_STATIONS)은 보지 않는다**(사용자 확정 2026-11-08, 결정 0026): 그 지점의 ASOS가
    계속 안 돼도 회차를 되풀이하지 않고, 감시의 "N일째" 경고(`obs`)로만 알린다."""
    # 예보를 할 수 있는 지점(격자 있음)만 기다린다. 격자가 비어 있는 지점은 예보를 건너뛰므로 그 지점 때문에
    # 같은 회차가 반복되면 안 된다(#28 문제 ④, 결정 0025).
    stations = [s for s in collect_stations(d) if forecast_ready(d.stations[s])]
    if not stations:
        return False
    run_at = urllib.parse.quote(_iso(issue), safe="")
    rows = read_table(
        base, key, "forecast_runs", http, f"select=stn_id,result&run_at=eq.{run_at}&collector=eq.{COLLECTOR}"
    )
    ok = {str(r["stn_id"]) for r in rows if r["result"] == "정시 성공"}
    return set(stations) <= ok


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"환경변수 {name}가 없습니다 (GitHub Secrets 또는 .env)")
    return v


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="물 수집기 한 회차 (결과는 파일로, DB 쓰기는 물3)")
    p.add_argument("--slot", help="발표시각 '2026-10-24 02' (없으면 지금 회차)")
    p.add_argument("--out", default="out", help="결과 폴더")
    p.add_argument("--stations", help="지점 번호 쉼표 목록 (없으면 수집 지점 has_forecast)")
    p.add_argument("--no-wait", action="store_true", help="재시도 없이 지금 한 번만 받기 (시험 실행)")
    p.add_argument("--write", action="store_true", help="계약 검사를 통과하면 Supabase에 쓰기")
    p.add_argument(
        "--soil-backfill",
        action="store_true",
        help="작물 공통 토양 물 상태(soil_water_daily)를 그해 3/1부터 전체 올리기",
    )
    p.add_argument(
        "--skip-if-done", action="store_true", help="이 회차가 이미 '정시 성공'이면 아무것도 하지 않고 끝내기"
    )
    p.add_argument(
        "--event",
        help="워크플로 실행 방식(github.event_name). 주면 --stations·--soil-backfill·--write·"
        "--skip-if-done 대신 아래 --input-* 값을 dispatch_options 로 판단한다(schedule 이면 입력은 무시)",
    )
    p.add_argument(
        "--input-stations", default="", help="(--event 와 함께) 수동 실행 입력: 지점 번호 쉼표 목록"
    )
    p.add_argument("--input-backfill", default="", help="(--event 와 함께) 수동 실행 입력: 소급(true)")
    p.add_argument(
        "--input-dry-run", default="", help="(--event 와 함께) 수동 실행 입력: 시험 실행(true면 DB 안 씀)"
    )
    a = p.parse_args(argv)
    if a.event is not None:
        o = dispatch_options(a.event, a.input_stations, a.input_backfill, a.input_dry_run)
        stations, soil_backfill, write, skip_if_done = o.stations, o.soil_backfill, o.write, o.skip_if_done
    else:
        stations = [s.strip() for s in a.stations.split(",")] if a.stations else None
        soil_backfill, write, skip_if_done = a.soil_backfill, a.write, a.skip_if_done
    clock = Clock()
    name, issue = parse_slot(a.slot) if a.slot else current_slot(clock.now())
    base, svc = _env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY")
    d = load_dictionaries(base, svc)
    obs_all = obs_all_enabled()
    check_backfill_scope(d, stations, soil_backfill, obs_all)
    if skip_if_done and slot_done(base, svc, d, issue):
        print(f"{name} {issue:%Y-%m-%d %H:%M} 회차는 이미 정시 성공으로 끝났습니다. 다시 하지 않습니다.")
        return 0
    soil_prior = None
    if (
        obs_all and not soil_backfill
    ):  # 평소 회차: 관측만 하는 지점의 창 시작 전날 Dr 을 DB 줄에서 한 번에 읽는다
        ids = stations or (collect_stations(d) + observation_only_stations(d))
        only = set(observation_only_stations(d))
        obs_ids = [s for s in ids if s in only and has_calc_inputs(d.stations[s])]
        prev = (issue.normalize() - pd.Timedelta(days=1)).date()
        win_start = prev - dt.timedelta(days=SOIL_UPLOAD_DAYS - 1)
        if obs_ids and d.soils and window_needs_prior(win_start, prev):
            soil_prior = load_soil_prior(base, svc, obs_ids, win_start - dt.timedelta(days=1))
    r = run_slot(
        name,
        issue,
        d,
        _env("DATA_GO_KR"),
        clock=clock,
        stations=stations,
        wait=not a.no_wait,
        soil_backfill=soil_backfill,
        obs_all=obs_all,
        soil_prior=soil_prior,
    )
    if write:
        from croplib_dp.io.supabase_write import SupabaseWriter

        rs_ok, rs_note = rs_source_state(base, svc)
        publish(
            r,
            SupabaseWriter(base, svc),
            taw_map(d),
            rs_source_column=rs_ok,
            rs_source_note=rs_note,
        )
    out = write_outputs(r, Path(a.out))
    print(summary_text(r))
    print(f"결과 폴더: {out}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
