"""농장 계산 작업 (회원 농장 물수지 · 3일 전망 · 관수 기록, 이슈 #131, 결정 0040).

회원이 저장하면 그 농장만, 정기 회차(02시 · 17시) 뒤에는 모든 농장을 다시 계산해 `farm_water_*` 표에 쓴다.
공공 API 는 부르지 않는다: 관측은 DB `weather_daily`, 예보는 같은 회차의 공개 전망 줄(`water_outlook`)에서 읽는다.

한 번 도는 일 (모드 `auto`):
  ① (정기 실행만 `--sync`) `sync_all_farm_seasons()` 로 모든 농장의 작기 줄을 고친다
  ② 계산할 농장: 대기 중(requested_at > done_at 또는 done_at 없음) ∪ 오래된 농장(done_at 이 그 지점 최신 회차 끝난
     시각보다 앞섬, 회차 기록이 없으면 그 지점 weather_daily 최신 fetched_at 기준). 작물 공통은 대기 줄만 계산 없이 닫는다.
  ③ 농장마다 계산 · 쓰기 · done_at 기록. 한 농장이 실패해도 나머지는 계속, 실패 농장은 result = failed · 짧은 error.
`done_at` 은 대기 줄이면 그 농장에서 읽은 requested_at 값(같은 DB 시계, 계산 도중 들어온 요청은 requested_at > done_at 로
다음 차례에 잡힘), 아니면 이번 실행이 농장 표를 읽기 시작한 시각이다.

보안: 농장 번호 · 주소 · 좌표 · 관수량은 로그 · 요약 · 결과 파일 · error 에 쓰지 않는다(숫자 세기만).
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import argparse
import datetime as dt
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from croplib_dp.core import CALC_VERSION
from croplib_dp.core.farm_water import (
    current_season,
    daily_logs,
    farm_outlook,
    first_record,
    num,
    rebuild_ft,
    seasons_to_calc,
    stale_dates,
    to_date,
    water_rows,
)
from croplib_dp.core.kc import adjust_crop, stage_climate
from croplib_dp.core.outlook import fill_from_forecast, pooled_errors
from croplib_dp.core.water_balance import Soil
from croplib_dp.io.coefficients import load_error_rows
from croplib_dp.io.contract import check_rows
from croplib_dp.io.dictionaries import Dictionaries, crop_height, crop_params, read_table
from croplib_dp.io.farm_db import FarmDb

RUN_LOOKBACK_DAYS = 3  # 회차 기록을 읽는 기간 (예보표 되살리기 · 오래된 농장 기준)
FT_LOOKBACK_H = 48  # 예보표에 넣는 회차 범위: 최신 회차 앞 이 시간 안(지연일 채움용)
DAILY_COLS = (
    "date",
    "farm_season_id",
    "kc",
    "eto_mm",
    "eto_src",
    "etc_mm",
    "rain_mm",
    "irr_l",
    "irr_net_mm",
    "dr_prev_mm",
    "ks",
    "etc_adj_mm",
    "dp_mm",
    "dr_mm",
    "taw_mm",
    "raw_mm",
    "status",
    "filled",
    "calc_version",
)
KO_TZ = "Asia/Seoul"


class FarmError(Exception):
    """한 농장의 계산 실패. 메시지는 그대로 farm_calc_status.error 에 쓰는 짧은 말이다(농장 정보 · 값 없음)."""


@dataclass
class FarmCalcResult:
    mode: str = "auto"
    dry_run: bool = False
    farms_total: int = 0
    n_pending: int = 0
    n_stale: int = 0
    n_ok: int = 0
    n_failed: int = 0
    n_common: int = 0
    n_outlook_warn: int = 0
    n_status_fail: int = 0
    daily_rows: int = 0
    deleted_rows: int = 0
    synced: int | None = None
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)  # 사람이 알아야 할 일(농장 정보 없음)

    @property
    def selected(self) -> int:
        return self.n_ok + self.n_failed + self.n_common

    @property
    def failed(self) -> bool:
        return self.n_failed > 0 or self.n_status_fail > 0


# ── 사전 · 시각 ──────────────────────────────────────────────────────────────
def load_dictionaries(base: str, key: str, http=None) -> Dictionaries:
    """농장 계산에 필요한 사전만 읽는다(지점 · 작물 · 작형 · 재배 방식 · 토성). 공개 계산 조건(scenarios)은 읽지 않는다."""
    kw = {} if http is None else dict(http=http)
    rows = {
        t: read_table(base, key, t, **kw)
        for t in ("stations", "crops", "crop_seasons", "crop_kc_sets", "soil_types")
    }
    return Dictionaries.from_rows({**rows, "scenarios": []})


def _ts(v: Any) -> pd.Timestamp | None:
    if v is None:
        return None
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def is_pending(st: dict | None) -> bool:
    """requested_at 이 있고 done_at 이 없거나 requested_at > done_at"""
    if not st or st.get("requested_at") is None:
        return False
    done = _ts(st.get("done_at"))
    return done is None or _ts(st["requested_at"]) > done


def _clean(v: Any) -> Any:
    """DB 로 보낼 값: 날짜 → 'YYYY-MM-DD', NaN → None, numpy 숫자 → 파이썬 숫자"""
    if isinstance(v, pd.Timestamp | dt.date):
        return v.isoformat()[:10]
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "item") and not isinstance(v, str | bytes):
        return _clean(v.item())
    return v


def _season_scenario(d: Dictionaries, s: dict) -> dict:
    """농장 작기 줄 → crop_params 가 읽는 조건 모양. kc_set_id 가 비면 그 작물의 기본 재배 방식."""
    kc = s.get("kc_set_id")
    if kc is None:
        kc = next(
            (
                k
                for k, row in d.kc_sets.items()
                if row.get("crop_id") == s["crop_id"] and row.get("is_default")
            ),
            None,
        )
    if kc is None or s["season_type_id"] not in d.seasons or s["crop_id"] not in d.crops:
        raise FarmError("작형 · 재배 방식 값을 찾지 못함")
    return dict(
        crop_id=s["crop_id"],
        season_type_id=s["season_type_id"],
        kc_set_id=kc,
        season_start=to_date(s["start_date"]).isoformat(),
    )


def _protected(d: Dictionaries, season_type_id: str) -> bool:
    """is_protected 를 못 읽으면(열 없음 · null) 노지로 가정하지 않고 그 농장을 계산하지 않는다(#128과 같은 규칙)"""
    v = (d.seasons.get(season_type_id) or {}).get("is_protected")
    if v is None:
        raise FarmError("작형의 is_protected 를 읽지 못함")
    return v is True or str(v).strip().lower() == "true"


def _soil(d: Dictionaries, farm: dict, crop_id: str) -> Soil:
    s, c = d.soils.get(farm["soil_code"]) or {}, d.crops.get(crop_id) or {}
    if s.get("fc") is None or s.get("wp") is None or c.get("zr_m") is None or c.get("p") is None:
        raise FarmError("토성 · 작물 값이 비어 있음")
    return Soil.from_soil(float(s["fc"]), float(s["wp"]), float(c["zr_m"]), float(c["p"]))


# ── 입력 모으기 (지점마다 한 번) ──────────────────────────────────────────────
class Inputs:
    """지점별 기상 · 회차 · 공개 전망 줄. 한 번 읽고 농장마다 나눠 쓴다."""

    def __init__(self, db: FarmDb, now: pd.Timestamp):
        self.db = db
        self.runs_all = db.runs((now - pd.Timedelta(days=RUN_LOOKBACK_DAYS)).isoformat())
        self.runs_by_stn: dict[str, list[dict]] = defaultdict(list)
        for r in self.runs_all:
            self.runs_by_stn[str(r["stn_id"])].append(r)
        self._weather: dict[str, list[dict]] = {}
        self._src: dict[Any, list[dict]] = {}
        self._err_all = None

    def ref_time(self, stn: str) -> pd.Timestamp | None:
        """그 지점의 '새 자료' 시각: 최신 회차 끝난 시각, 없으면 weather_daily 최신 fetched_at"""
        fin = [_ts(r["finished_at"]) for r in self.runs_by_stn.get(stn, []) if r.get("finished_at")]
        if fin:
            return max(fin)
        return _ts(self.db.latest_fetched(stn))

    def weather(self, stn: str, start: dt.date) -> list[dict]:
        if stn not in self._weather:
            rows = self.db.weather(stn, start.isoformat())
            self._weather[stn] = [
                dict(
                    date=to_date(r["date"]),
                    eto_mm=num(r.get("eto_mm")),
                    rain_mm=num(r.get("rain_mm")),
                    filled=bool(r.get("filled")),
                    u2_ms=num(r.get("u2_ms")),
                    rh_min_pct=num(r.get("rh_min_pct")),
                )
                for r in rows
            ]
        return self._weather[stn]

    def prefetch_outlook(self, stations: set[str]) -> None:
        """필요한 지점들의 최근 회차 공개 전망 줄을 한꺼번에 읽는다"""
        ids = []
        for stn in stations:
            runs = sorted(self.runs_by_stn.get(stn, []), key=lambda r: _ts(r["run_at"]), reverse=True)
            if not runs:
                continue
            newest = _ts(runs[0]["run_at"])
            ids += [
                r["run_id"] for r in runs if newest - _ts(r["run_at"]) <= pd.Timedelta(hours=FT_LOOKBACK_H)
            ]
        for row in self.db.outlook_src(sorted(set(ids))) if ids else []:
            self._src.setdefault(row["run_id"], []).append(row)

    def ft(self, stn: str, kc_of) -> tuple[pd.DataFrame, dict | None]:
        """(예보표, 쓴 최신 회차 줄). 공개 전망 줄이 있는 최신 회차 기준, 앞선 48시간 회차까지 되살린다."""
        runs = [r for r in self.runs_by_stn.get(stn, []) if r["run_id"] in self._src]
        if not runs:
            return pd.DataFrame(), None
        runs.sort(key=lambda r: _ts(r["run_at"]))
        newest = runs[-1]
        keep = [
            r for r in runs if _ts(newest["run_at"]) - _ts(r["run_at"]) <= pd.Timedelta(hours=FT_LOOKBACK_H)
        ]
        src = [o for r in keep for o in self._src[r["run_id"]]]
        return rebuild_ft(keep, src, kc_of), newest

    def err(self, stn: str) -> pd.DataFrame:
        if self._err_all is None:
            self._err_all = load_error_rows()
        return pooled_errors(self._err_all, stn)


# ── 한 농장 계산 ─────────────────────────────────────────────────────────────
@dataclass
class FarmOut:
    daily: list[dict] = field(default_factory=list)
    outlook: list[dict] = field(default_factory=list)
    summary: dict | None = None
    run_id: Any = None
    warn: str = ""
    cur_span: tuple[dt.date, dt.date] | None = None


def calc_farm(
    farm: dict, seasons: list[dict], logs: list[dict], d: Dictionaries, inp: Inputs, today: dt.date
) -> FarmOut:
    """농장 한 곳의 계산 결과(DB 에 쓰기 전). 실패하면 FarmError(짧은 말)."""
    out = FarmOut()
    stn = str(farm["stn_id"])
    st = d.stations.get(stn)
    if st is None:
        raise FarmError("지점 정보를 찾지 못함")
    prev = today - dt.timedelta(days=1)
    todo = seasons_to_calc(seasons, today)
    cur = current_season(seasons, today)
    if cur is not None:
        out.cur_span = (to_date(cur["start_date"]), to_date(cur["end_date"]))
    if not todo:
        return out
    start0 = min(to_date(s["start_date"]) for s in todo)
    weather = inp.weather(stn, start0)
    area = num(farm["irrigated_area_m2"])
    if not area or area <= 0:
        raise FarmError("관수 면적 값이 올바르지 않음")
    for s in todo:
        scen = _season_scenario(d, s)
        crop0 = crop_params(d, scen)
        crop = adjust_crop(
            crop0,
            stage_climate([w for w in weather if w["date"] >= crop0.season_start], crop0),
            crop_height(d, scen),
        )
        sd, ed = to_date(s["start_date"]), to_date(s["end_date"])
        prot = _protected(d, s["season_type_id"])
        soil = _soil(d, farm, s["crop_id"])
        # 예보표의 ETc 는 이 작기의 Kc 로 만든다(지난 작기 마감에는 채움에 쓰는 ETo 만 필요하다)
        ft, newest = inp.ft(stn, crop.kc)
        fill = fill_from_forecast(ft) if len(ft) else {}
        logs_season = daily_logs(logs, sd, ed)
        rows = water_rows(
            season=s,
            crop=crop,
            soil=soil,
            area_m2=area,
            weather=[w for w in weather if w["date"] >= sd],
            fill=fill,
            through=min(prev, ed),
            logs_l=logs_season,
            protected=prot,
        )
        for r in rows:
            out.daily.append(dict(farm_id=farm["farm_id"], **{k: _clean(r[k]) for k in DAILY_COLS}))
        if cur is not None and s is cur:
            _outlook(out, farm, d, inp, stn, today, s, crop, soil, prot, rows, logs_season, area, ft, newest)
    return out


def _outlook(out, farm, d, inp, stn, today, s, crop, soil, prot, rows, logs_season, area, ft, newest) -> None:
    sd = to_date(s["start_date"])
    if sd > today:
        return  # 작기 시작 전: 전망 · 요약 줄 없음(옛 줄은 지운다)
    if not d.stations[stn].get("has_forecast"):
        out.warn = "예보 지점이 아니라 3일 전망을 만들지 않음"
        return
    if not len(ft):
        out.warn = "공개 전망 줄이 없어 3일 전망을 만들지 못함"
        return
    run_ts = ft["run"].max()
    run_name = newest["run_name"]
    logs_cur = {k: v for k, v in logs_season.items() if k <= today}
    try:
        orows, summ = farm_outlook(
            ft=ft,
            rows=rows,
            first=first_record(logs_cur) if prot else None,
            logs_l=logs_cur,
            area_m2=area,
            soil=soil,
            crop=crop,
            err=inp.err(stn),
            run_name=run_name,
            run=run_ts,
            protected=prot,
        )
    except ValueError:
        out.warn = "예보가 모자라 3일 전망을 만들지 못함"
        return
    out.run_id = newest["run_id"]
    out.outlook = [
        dict(farm_id=farm["farm_id"], run_id=out.run_id, **{k: _clean(v) for k, v in o.items()})
        for o in orows
    ]
    out.summary = dict(farm_id=farm["farm_id"], run_id=out.run_id, **{k: _clean(v) for k, v in summ.items()})


def check_out(out: FarmOut) -> None:
    probs = check_rows("farm_water_daily", out.daily) + check_rows("farm_water_outlook", out.outlook)
    if out.summary is not None:
        probs += check_rows("farm_water_outlook_summary", [out.summary])
    if probs:
        raise FarmError("계약 검사 실패")


# ── 쓰기 ─────────────────────────────────────────────────────────────────────
def clean_up(
    db: FarmDb, farm: dict, seasons: list[dict], out: FarmOut, existing: list[dict], res: FarmCalcResult
) -> None:
    """줄 정리: 어떤 작기 줄에도 안 드는 날 · 지금 작기 안인데 이번에 안 만든 날의 물수지 줄, 최신 회차가 아닌 전망 줄."""
    fid = farm["farm_id"]
    new = {to_date(r["date"]) for r in out.daily}
    cs, ce = out.cur_span if out.cur_span else (None, None)
    old = [to_date(r["date"]) for r in existing]
    stale = stale_dates(old, seasons, new, cs, ce)
    if stale:
        res.deleted_rows += db.delete_daily(fid, [x.isoformat() for x in stale])
    if out.summary is not None:
        db.upsert("farm_water_outlook", out.outlook)
        db.upsert("farm_water_outlook_summary", [out.summary])
        res.deleted_rows += db.delete_outlook(fid, keep_run_id=out.run_id)
    else:  # 작기 시작 전 · 공개 전망 줄 없음 · 작기 없음: 옛 전망은 틀린 정보가 되므로 지운다
        res.deleted_rows += db.delete_outlook(fid) + db.delete_summary(fid)


# ── 한 번 도는 일 ────────────────────────────────────────────────────────────
def select_farms(
    farms: list[dict], status: dict[str, dict], ref: dict[str, pd.Timestamp | None], mode: str
) -> list[tuple[dict, bool]]:
    """(농장, 대기 중인가). auto: 대기 ∪ 오래된 농장, pending: 대기만, all: 대기 ∪ 작물 공통이 아닌 모든 농장"""
    out = []
    for f in farms:
        st = status.get(f["farm_id"])
        pend = is_pending(st)
        common = f["crop_id"] == "common"
        if pend:
            out.append((f, True))
        elif common or mode == "pending":
            continue
        elif mode == "all":
            out.append((f, False))
        else:
            done = _ts(st.get("done_at")) if st else None
            r = ref.get(str(f["stn_id"]))
            if done is None or (r is not None and done < r):
                out.append((f, False))
    return out


def run(
    db: FarmDb,
    d: Dictionaries,
    *,
    now: pd.Timestamp | None = None,
    mode: str = "auto",
    sync: bool = False,
    write: bool = True,
) -> FarmCalcResult:
    t0 = time.perf_counter()
    now = pd.Timestamp.now(tz="UTC") if now is None else _ts(now)
    res = FarmCalcResult(mode=mode, dry_run=not write)
    if sync and write:
        res.synced = db.sync_all()
    read_at = now.isoformat()
    today = now.tz_convert(KO_TZ).date()
    farms = db.farms()
    res.farms_total = len(farms)
    status = {r["farm_id"]: r for r in db.status()}
    inp = Inputs(db, now)
    stations = {str(f["stn_id"]) for f in farms if f["crop_id"] != "common"}
    ref = {s: inp.ref_time(s) for s in stations}
    chosen = select_farms(farms, status, ref, mode)
    res.n_pending = sum(1 for _, p in chosen if p)
    res.n_stale = len(chosen) - res.n_pending
    if not chosen:
        res.seconds = time.perf_counter() - t0
        return res
    ids = {f["farm_id"] for f, _ in chosen}
    seasons_by: dict[str, list[dict]] = defaultdict(list)
    for s in db.seasons():
        if s["farm_id"] in ids:
            seasons_by[s["farm_id"]].append(s)
    logs_by: dict[str, list[dict]] = defaultdict(list)
    for r in db.logs():
        if r["farm_id"] in ids:
            logs_by[r["farm_id"]].append(r)
    inp.prefetch_outlook({str(f["stn_id"]) for f, _ in chosen if f["crop_id"] != "common"})
    existing_by: dict[str, list[dict]] = defaultdict(list)
    for r in db.existing_daily(sorted(ids)):
        existing_by[r["farm_id"]].append(r)
    for farm, pend in chosen:
        fid = farm["farm_id"]
        done_at = status[fid]["requested_at"] if pend else read_at
        seasons = seasons_by.get(fid, [])
        try:
            if farm["crop_id"] == "common":
                out = FarmOut(
                    cur_span=None
                )  # 계산 없음: 어떤 작기 줄에도 안 드는 옛 줄 · 전망 · 요약만 지운다
                res.n_common += 1
            else:
                out = calc_farm(farm, seasons, logs_by.get(fid, []), d, inp, today)
                check_out(out)
                if out.warn:
                    res.n_outlook_warn += 1
            if write:
                _write_farm(db, farm, seasons, out, existing_by.get(fid, []), res)
                _put_status(db, fid, done_at, "ok", None, res)
            else:
                res.daily_rows += len(out.daily)
            if farm["crop_id"] != "common":
                res.n_ok += 1
        except FarmError as e:
            res.n_failed += 1
            if write:
                _put_status(db, fid, done_at, "failed", str(e), res)
        except (
            Exception
        ) as e:  # 예상 못 한 오류도 그 농장만 실패로 남긴다(조용히 넘기지 않음). 문구에는 종류만
            res.n_failed += 1
            if write:
                _put_status(db, fid, done_at, "failed", f"계산 오류({type(e).__name__})", res)
    res.seconds = time.perf_counter() - t0
    return res


def _put_status(
    db: FarmDb, farm_id: str, done_at: str, result: str, error: str | None, res: FarmCalcResult
) -> None:
    """계산 상태를 쓴다. 쓰지 못해도 다른 농장은 계속하고, 그 일은 요약에 남긴다(그 농장은 다음 실행이 다시 잡는다)."""
    try:
        db.put_status(farm_id, done_at, result, error, CALC_VERSION)
    except Exception:
        res.n_status_fail += 1


def _write_farm(
    db: FarmDb, farm: dict, seasons: list[dict], out: FarmOut, existing: list[dict], res: FarmCalcResult
) -> None:
    try:
        db.upsert("farm_water_daily", out.daily)
        res.daily_rows += len(out.daily)
        clean_up(db, farm, seasons, out, existing, res)
    except FarmError:
        raise
    except Exception as e:
        raise FarmError("DB 쓰기 실패") from e


# ── 요약 (농장 정보 없음) ────────────────────────────────────────────────────
def brief_text(r: FarmCalcResult) -> str:
    head = f"# 농장 계산 (계산 {CALC_VERSION}) 모드 {r.mode}" + (
        " · 시험 실행(쓰지 않음)" if r.dry_run else ""
    )
    lines = [
        head,
        f"대상 {r.selected}곳 (대기 {r.n_pending} · 오래됨 {r.n_stale}) / 전체 농장 {r.farms_total}곳"
        f" — 성공 {r.n_ok} · 실패 {r.n_failed} · 작물 공통 닫음 {r.n_common}",
        f"물수지 {r.daily_rows}줄 {'계산' if r.dry_run else '올림'} · 지운 줄 {r.deleted_rows}"
        f" · 전망 경고 {r.n_outlook_warn}곳"
        + (f" · 계산 상태를 못 씀 {r.n_status_fail}곳" if r.n_status_fail else "")
        + (f" · 작기 줄 정리 {r.synced}곳" if r.synced is not None else "")
        + f" · {r.seconds:.1f}초",
    ]
    lines += [f"* {n}" for n in r.notes]
    return "\n".join(lines) + "\n"


def write_outputs(r: FarmCalcResult, out_dir: Path) -> Path:
    d = out_dir / "farm_calc"
    d.mkdir(parents=True, exist_ok=True)
    text = brief_text(r)
    (d / "brief.md").write_text(text, encoding="utf-8")
    (d / "summary.md").write_text(text, encoding="utf-8")
    return d


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"환경변수 {name}가 없습니다 (GitHub Secrets 또는 .env)")
    return v


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="농장 계산 작업 (회원 농장 물수지 · 3일 전망)")
    p.add_argument("--out", default="out", help="결과 폴더")
    p.add_argument("--mode", choices=("auto", "pending", "all"), default="auto", help="계산할 농장 고르기")
    p.add_argument(
        "--sync",
        action="store_true",
        help="먼저 sync_all_farm_seasons() 로 모든 농장의 작기 줄을 고친다(정기 실행)",
    )
    p.add_argument("--dry-run", action="store_true", help="DB 에 쓰지 않고 계산만")
    a = p.parse_args(argv)
    base, key = _env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY")
    db = FarmDb(base, key)
    d = load_dictionaries(base, key)
    r = run(db, d, mode=a.mode, sync=a.sync, write=not a.dry_run)
    out = write_outputs(r, Path(a.out))
    print(brief_text(r))
    print(f"결과 폴더: {out}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
