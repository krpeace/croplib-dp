"""적산온도·적산일사 수집기 (CropMon 계약 v1.5 heat_daily·sun_daily, 온1·작물 공통).

하루 한 번: 지점마다 ASOS 일자료를 평년 기간(지난 10년)과 올해(1월 1일 ~ 어제) 받아,
core/heat_sun 으로 한 해 표를 만들고, 계약 검사를 통과하면 Supabase 에 덮어쓴다.
- 기준온도는 작물 사전 crops.t_base_c (표준값 층). 비어 있으면 그 작물은 계산하지 않고 문제로 남긴다.
- 일사를 재지 않는 지점(평년 기간에 sumGsr 가 하나도 없음)은 sun_daily 줄을 만들지 않는다.
- 한 지점이 끝내 받지 못하면 그 지점만 빼고, 회차를 실패로 남긴다(조용히 넘어가지 않음).
- 대상 지점은 운영 중인 지점(stations.is_active = true) 전부다. 폐지·휴지 지점은 올해 관측이 없어
  돌지 않고 실패로도 남기지 않는다. `--stations` 를 주면 그 지점만 돈다.
- `--cache-dir` 를 주면 지난 해(올해 이전) ASOS 일자료는 파일 캐시에서 읽고, 없는 해만 받는다.
  올해는 늘 새로 받는다. 결과 계산은 같다 (결정 0017).
"""

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from croplib_dp.collectors.water import ASOS_RETRY_S, Clock, RunLog, _env
from croplib_dp.core import CALC_VERSION
from croplib_dp.core.daily_weather import num
from croplib_dp.core.heat_sun import calc_gdd, normals_by_day, year_table
from croplib_dp.io import kma
from croplib_dp.io.asos_cache import AsosCache
from croplib_dp.io.contract import SPECS, check_rows
from croplib_dp.io.dictionaries import Dictionaries, load_dictionaries

NORMAL_YEARS = 10
HEAT_KEY = SPECS["heat_daily"].key
SUN_KEY = SPECS["sun_daily"].key


@dataclass
class HeatSunResult:
    year: int
    normal_from: int
    normal_to: int
    heat_daily: list[dict] = field(default_factory=list)
    sun_daily: list[dict] = field(default_factory=list)
    stations: list[dict] = field(default_factory=list)  # 지점별 요약 (사람이 읽는 보고)
    problems: list[str] = field(default_factory=list)
    log: RunLog = field(default_factory=RunLog)
    uploaded: bool = False
    cache_used: bool = False  # --cache-dir 를 줬나
    cache_hit_years: int = 0  # 캐시에서 읽은 (지점, 해) 수
    cache_fetched_years: int = 0  # 캐시를 쓰는데 못 찾아 새로 받은 지난 해 수

    @property
    def failed(self) -> bool:
        return bool(self.problems)


def _date(it: dict) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(it.get("tm", ""))[:10])
    except ValueError:
        return None


def fetch_year(key, stn_id, start, end, http, clock, log) -> list[dict] | None:
    """ASOS start~end (한 해 안). 일시 오류는 세 번 더 시도한다. 끝내 못 받으면 None."""
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


def build_station(stn_id: str, items: list[dict], crops: dict[str, float], year: int, nf: int, nt: int):
    """한 지점의 ASOS 일자료 → (heat 줄, sun 줄, 요약)"""
    by_day = {d: it for it in items if (d := _date(it)) is not None}
    temp_days = [
        d for d, it in by_day.items() if num(it.get("maxTa")) is not None and num(it.get("minTa")) is not None
    ]
    this_year = [d for d in temp_days if d.year == year]
    last_obs = max(this_year) if this_year else None
    heat, summary = [], dict(stn_id=stn_id, last_obs=str(last_obs) if last_obs else None, crops={})
    for crop_id, t_base in crops.items():
        g = {
            d: calc_gdd(num(it.get("avgTa")), num(it.get("maxTa")), num(it.get("minTa")), t_base)
            for d, it in by_day.items()
        }
        normal = normals_by_day(g, nf, nt)
        obs = {d: v for d, v in g.items() if d.year == year}
        rows = year_table(year, obs, normal, last_obs)
        for x in rows:
            heat.append(
                dict(
                    stn_id=stn_id,
                    crop_id=crop_id,
                    date=x["date"].isoformat(),
                    t_base_c=t_base,
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
            )
        last = next((x for x in reversed(rows) if x["cum"] is not None), None)
        summary["crops"][crop_id] = (
            dict(cum=_r(last["cum"], 0), normal=_r(last["normal_cum"], 0), lead=last["lead"])
            if last
            else None
        )
    rs = {d: num(it.get("sumGsr")) for d, it in by_day.items()}
    normal_rs = normals_by_day(rs, nf, nt)
    sun = []
    if normal_rs:
        for x in year_table(year, {d: v for d, v in rs.items() if d.year == year}, normal_rs, last_obs):
            sun.append(
                dict(
                    stn_id=stn_id,
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
            )
        last = next((x for x in reversed(sun) if x["rs_cum_mj"] is not None), None)
        summary["sun"] = (
            f"{last['rs_cum_mj'] / last['rs_normal_cum_mj'] * 100:.0f}%"
            if last and last["rs_normal_cum_mj"]
            else "자료 없음"
        )
    else:
        summary["sun"] = "자료 없음"
    return heat, sun, summary


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
) -> HeatSunResult:
    clock = clock or Clock()
    today = clock.now().date()
    year, end = today.year, today - dt.timedelta(days=1)
    r = HeatSunResult(year=year, normal_from=year - NORMAL_YEARS, normal_to=year - 1)
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
        items: list[dict] = []
        ok = True
        for y in range(r.normal_from, year + 1):
            s, e = dt.date(y, 1, 1), (dt.date(y, 12, 31) if y < year else end)
            past = cache is not None and y < year  # 지난 해는 바뀌지 않아 캐시한다. 올해는 늘 새로 받는다
            got = cache.get(stn_id, y) if past else None
            if got is not None:
                r.cache_hit_years += 1
            else:
                got = fetch_year(key, stn_id, s, e, http, clock, r.log)
                if got is not None and past:
                    cache.put(stn_id, y, got)
                    r.cache_fetched_years += 1
            if got is None:
                ok = False
                r.problems.append(f"{stn_id} {name}: ASOS {s}~{e} 를 받지 못해 이 지점을 뺌")
                break
            items += got
        if not ok:
            r.stations.append(dict(stn_id=stn_id, name=name, error="ASOS 실패"))
            continue
        heat, sun, summary = build_station(stn_id, items, crops, year, r.normal_from, r.normal_to)
        r.heat_daily += heat
        r.sun_daily += sun
        r.stations.append(dict(summary, name=name))
    return r


def check(r: HeatSunResult) -> list[str]:
    return check_rows("heat_daily", r.heat_daily) + check_rows("sun_daily", r.sun_daily)


def publish(r: HeatSunResult, writer) -> None:
    """계약 검사를 통과하면 두 표를 덮어쓴다. 어긋나면 올리지 않고 문제로 남긴다."""
    probs = check(r)
    if probs:
        r.problems += probs
        return
    try:
        if r.heat_daily:
            writer.upsert("heat_daily", r.heat_daily, on_conflict=HEAT_KEY)
        if r.sun_daily:
            writer.upsert("sun_daily", r.sun_daily, on_conflict=SUN_KEY)
        r.uploaded = True
    except Exception as e:  # 올리기 실패도 문제로 남긴다
        r.problems.append(f"DB 올리기 실패: {type(e).__name__}: {str(e)[:200]}")


CROP_KO = {"common": "작물 공통", "apple": "사과", "kimchi_cabbage": "배추"}


def brief_text(r: HeatSunResult) -> str:
    """짧은 요약 (실행 화면 알림용): 지점마다 한 줄"""
    lines = [f"# 적산온도·적산일사 {r.year} (평년 {r.normal_from}–{r.normal_to}, 계산 {CALC_VERSION})"]
    lines += [f"! {p}"[:300] for p in r.problems[:5]]
    if r.cache_used:
        lines.append(f"ASOS 캐시: 읽음 {r.cache_hit_years}해 · 새로 받아 남김 {r.cache_fetched_years}해")
    for s in r.stations:
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
    lines.append(
        f"DB 올리기: {'완료' if r.uploaded else '안 함'} · "
        f"heat {len(r.heat_daily)}줄 · sun {len(r.sun_daily)}줄"
    )
    return "\n".join(lines) + "\n"


def write_outputs(r: HeatSunResult, out_dir: Path) -> Path:
    d = out_dir / f"heat_sun_{r.year}"
    d.mkdir(parents=True, exist_ok=True)
    for t in ("heat_daily", "sun_daily", "stations", "problems"):
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
    a = p.parse_args(argv)
    d = load_dictionaries(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))
    stations = [s.strip() for s in a.stations.split(",")] if a.stations else None
    cache = AsosCache(a.cache_dir) if a.cache_dir else None
    r = run(d, _env("DATA_GO_KR"), stations=stations, cache=cache)
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
