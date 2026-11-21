"""적산온도·적산일사 수집기 (CropMon 계약 v1.5 heat_daily·sun_daily, 온1·작물 공통).

하루 한 번: 지점마다 ASOS 일자료를 평년 기간(지난 10년)과 올해(1월 1일 ~ 어제) 받아,
core/heat_sun 으로 한 해 표를 만들고, 계약 검사를 통과하면 Supabase 에 덮어쓴다.
- 새해 1월 1~3일(PREV_YEAR_DAYS)에는 작년도 12/31 까지 다시 계산해 마감한다. 1/1 의 '어제'는 작년 12/31 이라
  올해 구간이 거꾸로(시작 > 끝)라서 올해는 평년선만 만들고, 작년 12/31 값을 채운다 (이슈 #40, 결정 0018).
- 기준온도는 작물 사전 crops.t_base_c (표준값 층). 비어 있으면 그 작물은 계산하지 않고 문제로 남긴다.
- 일사를 재지 않는 지점(sumGsr 가 하나도 없음)은 sun_daily 줄을 만들지 않는다.
- sun_daily 는 관측 일사만 쓴다. 0.01×Ra 보다 작은 값과 한 달 대부분이 무효인 달의 값은 비우고,
  일조시간·기온차 추정값은 섞지 않는다(결정 0020). 값은 있었는데 모두 걸러졌거나 평년선의 유효 일수가
  100일 미만인 지점(일사 미측정 지점)은, DB 에 이미 올라간 옛 줄을 지우지 않고 같은 키로 값을 비운 줄을
  올려 덮어쓴다(삭제 없음).
- 한 지점이 끝내 받지 못하면 그 지점만 빼고, 회차를 실패로 남긴다(조용히 넘어가지 않음).
- 대상 지점은 운영 중인 지점(stations.is_active = true) 전부다. 폐지·휴지 지점은 올해 관측이 없어
  돌지 않고 실패로도 남기지 않는다. `--stations` 를 주면 그 지점만 돈다.
- `--cache-dir` 를 주면 지난 해(올해 이전) ASOS 일자료는 파일 캐시에서 읽고, 없는 해만 받는다.
  올해는 늘 새로 받는다. 결과 계산은 같다 (결정 0017).
- 적산 계열 2단계 (이슈 #127, 결정 0038): 옛 표(heat_daily·sun_daily)는 지금 그대로 쓰고,
  뷰 `crop_accum_series` 에서 읽은 계열(기준온도 × 누적 시작 월-일)마다
  새 표(heat_series_daily·sun_series_daily)에도 쓴다.
  같은 기준온도의 일값·평년은 한 번만 계산하고 누적만 계열마다 따로 낸다. 쓰기 순서는 옛 표 → 새 표.
  새 표를 쓰지 못하거나 뷰를 읽지 못해도 옛 표는 평소처럼 쓰고, 그 일은 문제 목록에 남긴다(회차 실패).
"""

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from croplib_dp.collectors.water import ASOS_RETRY_S, Clock, RunLog, _env
from croplib_dp.core import CALC_VERSION
from croplib_dp.core.daily_weather import num
from croplib_dp.core.heat_sun import (
    VIEW,
    calc_gdd,
    group_heat_series,
    group_sun_series,
    is_blank_row,
    normals_by_day,
    parse_mmdd,
    pending_stations,
    required_dates,
    t_base_key,
    year_table,
)
from croplib_dp.core.radiation import clean_observed_rs, has_enough_normals
from croplib_dp.io import kma
from croplib_dp.io.asos_cache import AsosCache, is_complete
from croplib_dp.io.contract import SPECS, check_rows
from croplib_dp.io.dictionaries import Dictionaries, load_dictionaries, read_table

NORMAL_YEARS = 10
BLANK_REPORT_DAYS = 7  # 요약에 알리는 끝 날 빈 줄의 최근 일수
PREV_YEAR_DAYS = 3  # 1월 1~3일은 작년도 다시 계산한다 (12/31 관측이 늦게 올라올 때를 위해)
HEAT_KEY = SPECS["heat_daily"].key
SUN_KEY = SPECS["sun_daily"].key
HEAT_SERIES_KEY = SPECS["heat_series_daily"].key
SUN_SERIES_KEY = SPECS["sun_series_daily"].key
SERIES_COLUMNS = "crop_id,season_type_id,heat_t_base_c,heat_cum_start_mmdd,sun_cum_start_mmdd"


@dataclass
class AccumSeries:
    """뷰 crop_accum_series 에서 읽은 적산 계열 목록 (코드에 목록을 적지 않는다)"""

    heat: list[tuple[Any, str]]  # (기준온도 — 뷰에서 받은 값 그대로, 누적 시작 'MM-DD')
    sun: list[str]  # 적산일사 누적 시작 'MM-DD'


def build_series(rows: list[dict]) -> tuple[AccumSeries | None, list[str]]:
    """뷰 줄 → (계열 목록, 문제 목록). 비었거나 쓸 수 있는 계열이 없으면 (None, 문제) 새 표를 만들지 않음."""
    if not rows:
        return None, [f"뷰 {VIEW} 가 비어 있어 새 표(heat_series_daily·sun_series_daily)를 만들지 않음"]
    heat, p_heat = group_heat_series(rows)
    sun, p_sun = group_sun_series(rows)
    if not heat and not sun:
        return None, [*p_heat, *p_sun, f"뷰 {VIEW} 에 쓸 수 있는 계열이 없어 새 표를 만들지 않음"]
    return AccumSeries(heat, sun), [*p_heat, *p_sun]


def read_series_rows(url: str, key: str, read=read_table) -> list[dict]:
    """뷰를 읽는다 (필요한 열만, 한도 200줄 — 지금 5줄). 실패하면 오류를 낸다."""
    return read(url, key, VIEW, query=f"select={SERIES_COLUMNS}&limit=200")


@dataclass
class StationRows:
    heat: list[dict]
    sun: list[dict]
    summary: dict
    heat_series: list[dict]
    sun_series: list[dict]


@dataclass
class YearBlock:
    """한 해 묶음: 그해 표를 만들 때 쓴 평년 기간과, 지점별 요약 (사람이 읽는 보고)"""

    year: int
    normal_from: int
    normal_to: int
    stations: list[dict] = field(default_factory=list)


@dataclass
class HeatSunResult:
    year: int  # 올해. 아래 normal_from·normal_to 와 stations 도 올해 묶음 기준이다
    normal_from: int
    normal_to: int
    heat_daily: list[dict] = field(default_factory=list)  # 계산한 모든 해(1/1~3 에는 작년+올해)
    sun_daily: list[dict] = field(default_factory=list)
    heat_series_daily: list[dict] = field(default_factory=list)  # 적산 계열 새 표 (이슈 #127)
    sun_series_daily: list[dict] = field(default_factory=list)
    blocks: list[YearBlock] = field(default_factory=list)  # 해마다 한 묶음 (오래된 해 → 올해)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # 실패는 아니지만 사람이 알아야 할 일 (건너뜀·빈 줄 등)
    log: RunLog = field(default_factory=RunLog)
    uploaded: bool = False  # 옛 표를 올렸나
    series_used: bool = False  # 뷰에서 계열 목록을 받아 새 표 줄을 만들었나
    series_uploaded: bool = False  # 새 표를 올렸나
    cache_used: bool = False  # --cache-dir 를 줬나
    cache_hit_years: int = 0  # 캐시에서 읽은 (지점, 해) 수
    cache_fetched_years: int = 0  # 캐시를 쓰는데 못 찾아 새로 받은 지난 해 수

    @property
    def stations(self) -> list[dict]:
        """올해 묶음의 지점별 요약"""
        return self.blocks[-1].stations if self.blocks else []

    @property
    def failed(self) -> bool:
        return bool(self.problems)


def _date(it: dict) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(it.get("tm", ""))[:10])
    except ValueError:
        return None


def fetch_year(key, stn_id, start, end, http, clock, log) -> list[dict] | None:
    """ASOS start~end (한 해 안). 일시 오류는 세 번 더 시도한다. 끝내 못 받으면 None.
    시작이 끝보다 늦으면(1/1 의 올해 구간) 묻지 않고 빈 목록을 돌려준다 (실패가 아니다)."""
    if start > end:
        log.add(clock, "asos", f"{stn_id} {start}~{end}", 0, {}, "skipped", note="역전 구간 — 호출 생략")
        return []
    for attempt in range(1, len(ASOS_RETRY_S) + 2):
        res = kma.fetch_asos_days(key, stn_id, start, end, http)
        log.add(clock, "asos", f"{stn_id} {start}~{end}", attempt, res, res["status"])
        if res["status"] in ("ok", "nodata"):
            return res["items"]
        if res["status"] != "retry" or attempt > len(ASOS_RETRY_S):
            return None
        clock.sleep(ASOS_RETRY_S[attempt - 1])
    return None


def _r(v, n=2):
    return None if v is None else round(float(v), n)


def _empty_sun_rows(
    stn_id: str, year: int, nf: int, nt: int, start: tuple[int, int] = (1, 1), **key
) -> list[dict]:
    """값을 모두 비운 sun 줄(그해 누적 시작 ~ 12/31, 기본 1/1). 옛 줄을 지우지 않고 덮어쓰는 데 쓴다.
    key: 새 표의 cum_start_mmdd"""
    first = dt.date(year, *start)
    return [
        dict(
            stn_id=stn_id,
            **key,
            date=(first + dt.timedelta(days=k)).isoformat(),
            rs_day_mj=None,
            rs_cum_mj=None,
            rs_normal_day_mj=None,
            rs_normal_cum_mj=None,
            normal_from=nf,
            normal_to=nt,
            filled=True,
            calc_version=CALC_VERSION,
        )
        for k in range((dt.date(year, 12, 31) - first).days + 1)
    ]


def _heat_row(x: dict, stn_id: str, t_base_c, nf: int, nt: int, **key) -> dict:
    """year_table 한 줄 → heat 줄. key: 옛 표는 crop_id, 새 표는 cum_start_mmdd"""
    return dict(
        stn_id=stn_id,
        **key,
        date=x["date"].isoformat(),
        t_base_c=t_base_c,
        gdd_day=_r(x["day"]),
        gdd_cum=_r(x["cum"], 1),
        gdd_normal_day=_r(x["normal_day"]),
        gdd_normal_cum=_r(x["normal_cum"], 1),
        lead_days=x["lead"],
        normal_from=nf,
        normal_to=nt,
        filled=bool(x["filled"]),
        calc_version=CALC_VERSION,
    )


def _sun_row(x: dict, stn_id: str, nf: int, nt: int, **key) -> dict:
    """year_table 한 줄 → sun 줄. key: 새 표의 cum_start_mmdd"""
    return dict(
        stn_id=stn_id,
        **key,
        date=x["date"].isoformat(),
        rs_day_mj=_r(x["day"]),
        rs_cum_mj=_r(x["cum"], 1),
        rs_normal_day_mj=_r(x["normal_day"]),
        rs_normal_cum_mj=_r(x["normal_cum"], 1),
        normal_from=nf,
        normal_to=nt,
        filled=bool(x["filled"]),
        calc_version=CALC_VERSION,
    )


def build_station(
    stn_id: str,
    items: list[dict],
    crops: dict[str, float],
    year: int,
    nf: int,
    nt: int,
    lat: float,
    series: AccumSeries | None = None,
) -> StationRows:
    """한 지점의 ASOS 일자료 → 옛 표 줄(heat·sun) + 새 표 줄(series 가 있을 때) + 요약.
    lat 는 일사 하한(0.01×Ra)을 구하는 데 쓴다. 같은 기준온도의 일값·평년은 한 번만 계산하고,
    계열(누적 시작)마다 누적만 따로 낸다."""
    by_day = {d: it for it in items if (d := _date(it)) is not None}
    temp_days = [
        d for d, it in by_day.items() if num(it.get("maxTa")) is not None and num(it.get("minTa")) is not None
    ]
    this_year = [d for d in temp_days if d.year == year]
    last_obs = max(this_year) if this_year else None
    # 기온(최고·최저)이 없는 '빈 줄' 날 중 마지막 관측일 뒤(끝 날)는 그 날 줄을 올리지 않는다.
    # 올리면 이미 올라간 옛 값을 비운 값으로 덮는다(이슈 #104). 마지막 관측일 앞에 낀 빈 날은
    # 평년 채움 줄(filled = true)로 쓴다 — 건너뛰면 DB 에 남은 null 줄이 영영 안 바뀐다(이슈 #109).
    blank = {
        d.isoformat()
        for d, it in by_day.items()
        if d.year == year and is_blank_row(it) and (last_obs is None or d > last_obs)
    }
    heat, summary = [], dict(stn_id=stn_id, last_obs=str(last_obs) if last_obs else None, crops={})
    summary["blank"] = sorted(blank)

    # 기준온도마다 일값·평년을 한 번만 계산한다(inputs).
    # 같은 기준온도·같은 시작이면 한 해 표도 한 번만 만든다(tables).
    inputs: dict = {}
    tables: dict = {}

    def heat_table(t_base, start=(1, 1)) -> list[dict]:
        k = t_base_key(t_base)
        if k not in inputs:
            g = {
                d: calc_gdd(num(it.get("avgTa")), num(it.get("maxTa")), num(it.get("minTa")), float(t_base))
                for d, it in by_day.items()
            }
            inputs[k] = ({d: v for d, v in g.items() if d.year == year}, normals_by_day(g, nf, nt))
        if (k, start) not in tables:
            obs, normal = inputs[k]
            tables[(k, start)] = year_table(year, obs, normal, last_obs, start=start)
        return tables[(k, start)]

    for crop_id, t_base in crops.items():
        rows = heat_table(t_base)
        heat += [
            _heat_row(x, stn_id, t_base, nf, nt, crop_id=crop_id)
            for x in rows
            if x["date"].isoformat() not in blank
        ]
        last = next((x for x in reversed(rows) if x["cum"] is not None), None)
        summary["crops"][crop_id] = (
            dict(cum=_r(last["cum"], 0), normal=_r(last["normal_cum"], 0), lead=last["lead"])
            if last
            else None
        )
    heat_series = []
    for t_base, mmdd in series.heat if series else ():
        heat_series += [
            _heat_row(x, stn_id, t_base, nf, nt, cum_start_mmdd=mmdd)
            for x in heat_table(t_base, parse_mmdd(mmdd))
            if x["date"].isoformat() not in blank
        ]

    raw_rs = {d: num(it.get("sumGsr")) for d, it in by_day.items()}
    rs = clean_observed_rs(raw_rs, lat)  # 0.0·무효 달은 None (추정값을 섞지 않는다)
    normal_rs = normals_by_day(rs, nf, nt)
    enough = has_enough_normals(normal_rs)
    had_raw = any(v is not None for v in raw_rs.values())

    def sun_rows(start=(1, 1), **key) -> list[dict]:
        if not enough and had_raw:
            # 값은 있었는데 모두 걸러졌거나 평년선이 100일 미만(영주형) → 일사 미측정 지점.
            # DB 의 옛 줄은 지우지 않고 빈 값 줄로 덮어쓴다
            return _empty_sun_rows(stn_id, year, nf, nt, start, **key)
        if not enough:
            return []
        obs_rs = {d: v for d, v in rs.items() if d.year == year}
        return [
            _sun_row(x, stn_id, nf, nt, **key)
            for x in year_table(year, obs_rs, normal_rs, last_obs, start=start)
            if x["date"].isoformat() not in blank
        ]

    sun = sun_rows()
    if not enough:
        summary["sun"] = "자료 없음"
    else:
        last = next((x for x in reversed(sun) if x["rs_cum_mj"] is not None), None)
        summary["sun"] = (
            f"{last['rs_cum_mj'] / last['rs_normal_cum_mj'] * 100:.0f}%"
            if last and last["rs_normal_cum_mj"]
            else "자료 없음"
        )
    sun_series = []
    for mmdd in series.sun if series else ():
        sun_series += sun_rows(parse_mmdd(mmdd), cum_start_mmdd=mmdd)
    return StationRows(heat, sun, summary, heat_series, sun_series)


def active_stations(d: Dictionaries) -> list[str]:
    """적산온도를 내는 지점: 운영 중(is_active)인 지점 전부. 폐지·휴지 지점은 올해 관측이 없다."""
    return [k for k, st in d.stations.items() if st.get("is_active", True)]


def run(
    d: Dictionaries,
    key: str,
    http=kma.http_get,
    clock=None,
    stations: list[str] | None = None,
    cache: AsosCache | None = None,
    series: AccumSeries | None = None,
) -> HeatSunResult:
    """series: 뷰에서 읽은 적산 계열. 없으면(None) 새 표 줄은 만들지 않는다(옛 표만, 지금과 같다)."""
    clock = clock or Clock()
    today = clock.now().date()
    year, end = today.year, today - dt.timedelta(days=1)
    in_prev_window = today.month == 1 and today.day <= PREV_YEAR_DAYS
    years = [year - 1, year] if in_prev_window else [year]  # 작년은 항상 12/31 까지, 올해는 어제까지
    r = HeatSunResult(year=year, normal_from=year - NORMAL_YEARS, normal_to=year - 1)
    r.blocks = [YearBlock(y, y - NORMAL_YEARS, y - 1) for y in years]
    r.series_used = series is not None
    crops = {}
    for c in d.crops.values():
        if c.get("t_base_c") is None:
            r.problems.append(f"작물 {c['crop_id']}: 기준온도(crops.t_base_c)가 비어 있어 계산하지 않음")
        else:
            crops[c["crop_id"]] = float(c["t_base_c"])
    r.cache_used = cache is not None
    targets = stations or active_stations(d)
    if not targets:
        r.problems.append("대상 지점이 0곳 — 지점 사전 is_active 확인")
    for stn_id in targets:
        name = d.stations.get(stn_id, {}).get("name_ko", "")
        lat = d.stations.get(stn_id, {}).get("lat")
        if lat is None:  # 일사 하한을 못 구한다. 조용히 넘어가지 않고 이 지점을 뺀다
            r.problems.append(f"{stn_id} {name}: 지점 사전에 위도(lat)가 없어 이 지점을 뺌")
            for b in r.blocks:
                b.stations.append(dict(stn_id=stn_id, name=name, error="위도 없음"))
            continue
        lat = float(lat)
        items: list[dict] = []
        ok = True
        for y in range(years[0] - NORMAL_YEARS, year + 1):  # 가장 오래된 해의 평년 첫 해 ~ 올해
            s, e = dt.date(y, 1, 1), (dt.date(y, 12, 31) if y < year else end)
            past = cache is not None and y < year  # 지난 해는 바뀌지 않아 캐시한다. 올해는 늘 새로 받는다
            got = cache.get(stn_id, y) if past else None
            if got is not None:
                r.cache_hit_years += 1
            else:
                got = fetch_year(key, stn_id, s, e, http, clock, r.log)
                if got is not None and past and is_complete(got, y):  # 12/31 이 아직 없는 해는 남기지 않는다
                    cache.put(stn_id, y, got)
                    r.cache_fetched_years += 1
            if got is None:
                ok = False
                r.problems.append(f"{stn_id} {name}: ASOS {s}~{e} 를 받지 못해 이 지점을 뺌")
                break
            items += got
        if not ok:
            for b in r.blocks:
                b.stations.append(dict(stn_id=stn_id, name=name, error="ASOS 실패"))
            continue
        for b in r.blocks:  # 받은 자료 한 벌로 해마다 계산한다
            built = build_station(stn_id, items, crops, b.year, b.normal_from, b.normal_to, lat, series)
            summary = built.summary
            block_end = min(end, dt.date(b.year, 12, 31))  # 요약은 최근 7일 끝 날의 빈 줄만 알린다
            recent = (block_end - dt.timedelta(days=BLANK_REPORT_DAYS - 1)).isoformat()
            summary["blank"] = [d for d in summary["blank"] if d >= recent]
            r.heat_daily += built.heat
            r.sun_daily += built.sun
            r.heat_series_daily += built.heat_series
            r.sun_series_daily += built.sun_series
            b.stations.append(dict(summary, name=name))
    return r


def read_done(table: str, dates: list[dt.date]) -> set[tuple[str, dt.date]]:
    """DB 에 이미 올라간 관측 줄의 (지점, 날짜). 실패하면 오류를 낸다(부르는 쪽이 평소처럼 돌린다).
    heat_daily 는 작물 공통만 본다. 둘 다 관측 줄(filled = false)에 값이 있는 것만 센다."""
    url, key = _env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY")
    cond = "date=in.(" + ",".join(d.isoformat() for d in dates) + ")&filled=eq.false"
    if table == "heat_daily":
        q = f"select=stn_id,date&crop_id=eq.common&gdd_cum=not.is.null&{cond}"
    else:
        q = f"select=stn_id,date&rs_cum_mj=not.is.null&{cond}"
    rows = read_table(url, key, table, query=q + "&limit=5000")
    return {(x["stn_id"], dt.date.fromisoformat(x["date"])) for x in rows}


def skip_check(d: Dictionaries, today: dt.date, read=None) -> tuple[bool, str]:
    """(건너뛸까, 사람이 읽는 이유). 필요한 날 값을 운영 지점 전부가 이미 갖고 있을 때만 건너뛴다.
    한 곳이라도 비었거나 DB 를 못 읽으면 건너뛰지 않는다 — 아침에 못 채운 지점을 늦은 실행이 채워야 한다."""
    read = read or read_done
    targets = active_stations(d)
    if not targets:
        return False, ""
    req = required_dates(today)
    try:
        heat = read("heat_daily", req)
        sun = read("sun_daily", req + [x - dt.timedelta(days=1) for x in req])
    except Exception as e:
        return False, f"건너뛸지 확인 못 함({type(e).__name__}: {str(e)[:120]}) — 평소처럼 실행"
    left = pending_stations(targets, req, heat, sun)
    if left:
        return False, ""
    days = ", ".join(x.isoformat() for x in req)
    return True, f"건너뜀: {days} 값을 운영 지점 {len(targets)}곳 모두 이미 갖고 있음 — 받지도 쓰지도 않음"


def check_old(r: HeatSunResult) -> list[str]:
    return check_rows("heat_daily", r.heat_daily) + check_rows("sun_daily", r.sun_daily)


def check_series(r: HeatSunResult) -> list[str]:
    return check_rows("heat_series_daily", r.heat_series_daily) + check_rows(
        "sun_series_daily", r.sun_series_daily
    )


def check(r: HeatSunResult) -> list[str]:
    return check_old(r) + check_series(r)


def publish(r: HeatSunResult, writer) -> None:
    """계약 검사를 통과하면 옛 표 둘을 덮어쓰고, 이어서 새 표 둘(적산 계열)을 덮어쓴다 (옛 표 → 새 표).
    옛 표가 계약에 어긋나면 아무것도 올리지 않고 문제로 남긴다(지금과 같다).
    새 표가 계약에 어긋나거나 올리지 못하면 옛 표는 이미 올라간 채 두고, 새 표 문제를 남긴다(회차 실패)."""
    probs = check_old(r)
    if probs:
        r.problems += probs
        return
    new_probs = check_series(r)
    try:
        if r.heat_daily:
            writer.upsert("heat_daily", r.heat_daily, on_conflict=HEAT_KEY)
        if r.sun_daily:
            writer.upsert("sun_daily", r.sun_daily, on_conflict=SUN_KEY)
        r.uploaded = True
    except Exception as e:  # 올리기 실패도 문제로 남긴다
        r.problems.append(f"DB 올리기 실패: {type(e).__name__}: {str(e)[:200]}")
        return
    if new_probs:
        r.problems += new_probs
        return
    table = ""
    try:
        for table, rows, key in (
            ("heat_series_daily", r.heat_series_daily, HEAT_SERIES_KEY),
            ("sun_series_daily", r.sun_series_daily, SUN_SERIES_KEY),
        ):
            if rows:
                writer.upsert(table, rows, on_conflict=key)
        r.series_uploaded = bool(r.heat_series_daily or r.sun_series_daily)
    except Exception as e:  # 옛 표는 이미 올라갔다. 새 표 문제는 조용히 넘기지 않는다
        r.problems.append(f"새 표 {table} 올리기 실패(옛 표는 올라감): {type(e).__name__}: {str(e)[:200]}")


CROP_KO = {"common": "작물 공통", "apple": "사과", "kimchi_cabbage": "배추", "grape": "포도"}


def _new_year_line(r: HeatSunResult) -> str:
    """1/1~3 에만: 작년을 마감하는 실행에서 새해 줄이 몇 줄 만들어졌는지 한 줄로 (지점 줄은 늘리지 않는다)"""
    b = r.blocks[-1]
    heat = sum(1 for x in r.heat_daily if x["date"].startswith(f"{b.year}-"))
    sun = sum(1 for x in r.sun_daily if x["date"].startswith(f"{b.year}-"))
    seen = [s["last_obs"] for s in b.stations if s.get("last_obs")]
    state = f"관측 ~{max(seen)[5:]}까지" if seen else "평년선만 생성"
    return f"새해 {b.year}: {state} (heat {heat}줄 · sun {sun}줄)"


def blank_text(stations: list[dict]) -> str:
    """기상청이 값 없는 빈 줄로 준 날: 그 날 줄은 올리지 않았다고 한 줄로 알린다 (조용히 넘기지 않는다)"""
    by_day: dict[str, int] = {}
    for s in stations:
        for d in s.get("blank") or []:
            by_day[d] = by_day.get(d, 0) + 1
    if not by_day:
        return ""
    days = sorted(by_day)
    shown = ", ".join(f"{d}({by_day[d]}곳)" for d in days[:5]) + (" 외" if len(days) > 5 else "")
    n = len({s["stn_id"] for s in stations if s.get("blank")})
    return (
        f"빈 줄(기온 없음) 날 {n}곳: {shown} — 그 날 줄은 올리지 않음 "
        "(옛 값을 지우지 않는다, 값이 올라오면 다음 실행이 채움)"
    )


def brief_text(r: HeatSunResult) -> str:
    """짧은 요약 (실행 화면 알림용): 지점마다 한 줄.
    1/1~3 에는 지점 줄을 작년 마감 기준으로 보여 주고, 새해는 한 줄만 덧붙인다."""
    main = r.blocks[0] if r.blocks else YearBlock(r.year, r.normal_from, r.normal_to)
    lines = [
        f"# 적산온도·적산일사 {main.year} (평년 {main.normal_from}–{main.normal_to}, 계산 {CALC_VERSION})"
    ]
    lines += [f"! {p}"[:300] for p in r.problems[:5]]
    lines += [f"* {n}"[:300] for n in r.notes]
    blank = blank_text(main.stations)
    if blank:
        lines.append(blank)
    if r.cache_used:
        lines.append(f"ASOS 캐시: 읽음 {r.cache_hit_years}해 · 새로 받아 남김 {r.cache_fetched_years}해")
    for s in main.stations:
        if s.get("error"):
            lines.append(f"- {s['stn_id']} {s.get('name', '')}: {s['error']}")
            continue
        parts = [f"관측 ~{(s.get('last_obs') or '없음')[5:]}"]
        for crop, c in s.get("crops", {}).items():
            if c:
                lead = c["lead"]
                when = (
                    "평년 미도달"
                    if lead is None
                    else (f"평년보다 {abs(lead)}일 " + ("빠름" if lead >= 0 else "늦음"))
                )
                parts.append(f"{CROP_KO.get(crop, crop)} {c['cum']:.0f}/{c['normal']:.0f}℃·일 {when}")
        parts.append(f"햇빛 {s.get('sun')}")
        lines.append(f"- {s['stn_id']} {s.get('name', '')}: " + " · ".join(parts))
    if len(r.blocks) > 1:
        lines.append(_new_year_line(r))
    lines.append(
        f"DB 올리기: {'완료' if r.uploaded else '안 함'} · "
        f"heat {len(r.heat_daily)}줄 · sun {len(r.sun_daily)}줄"
        + (
            f" · 새 표 {'올림' if r.series_uploaded else '안 올림'}: "
            f"heat_series {len(r.heat_series_daily)}줄 · sun_series {len(r.sun_series_daily)}줄"
            if r.series_used
            else ""
        )
    )
    return "\n".join(lines) + "\n"


def write_outputs(r: HeatSunResult, out_dir: Path) -> Path:
    d = out_dir / f"heat_sun_{r.year}"
    d.mkdir(parents=True, exist_ok=True)
    for t in ("heat_daily", "sun_daily", "heat_series_daily", "sun_series_daily", "stations", "problems"):
        (d / f"{t}.json").write_text(
            json.dumps(getattr(r, t), ensure_ascii=False, indent=1), encoding="utf-8"
        )
    (d / "collect_log.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in r.log.entries), encoding="utf-8"
    )
    (d / "brief.md").write_text(brief_text(r), encoding="utf-8")
    (d / "summary.md").write_text(brief_text(r), encoding="utf-8")
    return d


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="적산온도·적산일사 수집기 (하루 한 번)")
    p.add_argument("--out", default="out", help="결과 폴더")
    p.add_argument("--stations", help="지점 번호 쉼표 목록 (없으면 운영 지점 전부)")
    p.add_argument("--cache-dir", help="지난 해 ASOS 일자료를 남겨 두고 다시 쓰는 폴더 (없으면 매번 다 받기)")
    p.add_argument("--write", action="store_true", help="계약 검사를 통과하면 Supabase에 쓰기")
    p.add_argument(
        "--skip-if-done",
        action="store_true",
        help="(--write 와 함께, 지점 지정 없이) 필요한 날 값을 운영 지점 전부가 이미 갖고 있으면 건너뛰기",
    )
    a = p.parse_args(argv)
    d = load_dictionaries(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))
    stations = [s.strip() for s in a.stations.split(",")] if a.stations else None
    cache = AsosCache(a.cache_dir) if a.cache_dir else None
    notes: list[str] = []
    if a.skip_if_done and a.write and not stations:
        today = Clock().now().date()
        skip, why = skip_check(d, today, read_done)
        if skip:
            ny = today.year
            r = HeatSunResult(year=ny, normal_from=ny - NORMAL_YEARS, normal_to=ny - 1)
            r.blocks = [YearBlock(today.year, r.normal_from, r.normal_to)]
            r.notes.append(why)
            out = write_outputs(r, Path(a.out))
            print(brief_text(r))
            print(f"결과 폴더: {out}")
            return 0
        if why:
            notes.append(why)
    series, series_problems = None, []
    # 적산 계열 목록은 뷰에서 읽는다. 못 읽어도 옛 표는 평소처럼 올리고,
    # 그 일은 문제로 남긴다(조용히 넘기지 않음).
    try:
        series, series_problems = build_series(
            read_series_rows(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))
        )
    except Exception as e:
        series_problems = [
            f"뷰 {VIEW} 읽기 실패: {type(e).__name__}: {str(e)[:200]} — 새 표 줄은 만들지 않고 옛 표만 올림"
        ]
    r = run(d, _env("DATA_GO_KR"), stations=stations, cache=cache, series=series)
    r.problems += series_problems
    r.notes += notes
    if a.write:
        from croplib_dp.io.supabase_write import SupabaseWriter

        publish(r, SupabaseWriter(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY")))
    else:
        r.problems += check(r)
    out = write_outputs(r, Path(a.out))
    print(brief_text(r))
    print(f"결과 폴더: {out}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
