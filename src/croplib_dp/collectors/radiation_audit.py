"""일사 점검 수집기 (이슈 #48, #28 S0-b PR-A). **DB에 쓰지 않는다.** 결과는 파일로만 남긴다.

운영 지점(stations.is_active)마다 지난 365일 ASOS 일자료를 받아(해가 바뀌면 해마다 한 번씩 = 지점당 2번),
core/radiation_audit 으로 분류·실측 kRs·ETo 오차·의심 일조를 구한다. 지점 목록은 DB 에서 읽기만 한다.
- 사람이 Actions 에서 눌러 실행한다(`radiation-audit.yml`, 예약 없음).
- 한 지점이 끝내 받지 못하면 그 지점만 빼고 실패로 남긴다(조용히 넘어가지 않음).
- 해안선 거리·섬 여부는 여기서 구하지 않는다(PR-B, 원본 해안선 자료는 저장소에 넣지 않음).
"""

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from croplib_dp.collectors.heat_sun import active_stations, fetch_year
from croplib_dp.collectors.water import Clock, RunLog, _env
from croplib_dp.core.daily_weather import StationMeta
from croplib_dp.core.radiation_audit import CLASSES, KRS_HIGH, KRS_LOW, audit_station
from croplib_dp.io import kma
from croplib_dp.io.dictionaries import Dictionaries, load_dictionaries

DEFAULT_DAYS = 365
OUT_NAME = "radiation_audit"


@dataclass
class AuditResult:
    start: dt.date
    end: dt.date
    days: int
    stations: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    log: RunLog = field(default_factory=RunLog)

    @property
    def failed(self) -> bool:
        return bool(self.problems)

    @property
    def calls(self) -> int:
        """ASOS 조회 시도 수(재시도 포함, 호출 생략은 뺌)"""
        return sum(1 for e in self.log.entries if e["kind"] == "asos" and e["status"] != "skipped")


def _pieces(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """기간을 달력 해별로 자른다(fetch_year 는 한 해 안의 기간을 받는다)"""
    return [
        (max(start, dt.date(y, 1, 1)), min(end, dt.date(y, 12, 31))) for y in range(start.year, end.year + 1)
    ]


def run(
    d: Dictionaries,
    key: str,
    http=kma.http_get,
    clock=None,
    stations: list[str] | None = None,
    days: int = DEFAULT_DAYS,
) -> AuditResult:
    clock = clock or Clock()
    end = clock.now().date() - dt.timedelta(days=1)
    r = AuditResult(start=end - dt.timedelta(days=days - 1), end=end, days=days)
    targets = stations or active_stations(d)
    if not targets:
        r.problems.append("대상 지점이 0곳 — 지점 사전 is_active 확인")
    for stn_id in targets:
        st = d.stations.get(stn_id, {})
        name = st.get("name_ko", "")
        if st.get("lat") is None or st.get("elev_m") is None:  # 천문일사·ETo 를 못 구한다
            r.problems.append(f"{stn_id} {name}: 지점 사전에 위도·고도가 없어 이 지점을 뺌")
            r.stations.append(dict(stn_id=stn_id, name=name, error="위도·고도 없음"))
            continue
        items: list[dict] = []
        ok = True
        for s, e in _pieces(r.start, r.end):
            got = fetch_year(key, stn_id, s, e, http, clock, r.log)
            if got is None:
                ok = False
                r.problems.append(f"{stn_id} {name}: ASOS {s}~{e} 를 받지 못해 이 지점을 뺌")
                break
            items += got
        if not ok:
            r.stations.append(dict(stn_id=stn_id, name=name, error="ASOS 실패"))
            continue
        meta = StationMeta(
            lat=float(st["lat"]),
            elev_m=float(st["elev_m"]),
            wind_height_m=float(st.get("wind_height_m") or 10),
        )
        r.stations.append(
            dict(
                stn_id=stn_id,
                name=name,
                lat=float(st["lat"]),
                lon=float(st["lon"]) if st.get("lon") is not None else None,
                elev_m=float(st["elev_m"]),
                db_measures_sunlight=st.get("measures_sunlight"),
                **audit_station(items, meta),
            )
        )
    return r


def _round(o: Any, n: int = 6) -> Any:
    if isinstance(o, float):
        return round(o, n)
    if isinstance(o, dict):
        return {k: _round(v, n) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v, n) for v in o]
    return o


def _f(v, n=3):
    return "-" if v is None else f"{v:.{n}f}"


def brief_text(r: AuditResult) -> str:
    """짧은 요약(실행 화면 알림용, 4KB 안): 분류별 지점 수와 문제"""
    ok = [s for s in r.stations if not s.get("error")]
    lines = [f"# 일사 점검 {r.start} ~ {r.end} ({r.days}일, 호출 {r.calls}번, DB 쓰기 안 함)"]
    lines += [f"! {p}"[:300] for p in r.problems[:5]]
    lines.append(f"지점 {len(r.stations)}곳, 받은 곳 {len(ok)}곳")
    for c in CLASSES:
        names = [s["name"] or s["stn_id"] for s in ok if s["klass"] == c]
        shown = ", ".join(names[:8]) + (f" 외 {len(names) - 8}곳" if len(names) > 8 else "")
        lines.append(f"- {c}: {len(names)}곳" + (f" ({shown})" if names else ""))
    sparse = [s for s in ok if s["rs_sparse"]]
    lines.append(f"- 일사 값이 1~99일뿐인 지점: {len(sparse)}곳")
    lines.append(f"- 의심 일조 합계: {sum(s['suspect_sunshine_days'] for s in ok)}일 (규칙 없음, 개수만)")
    return "\n".join(lines) + "\n"


def summary_text(r: AuditResult) -> str:
    """전체 요약: 지점마다 한 줄 (사람이 읽는 표, 실행 화면 요약 칸용)"""
    rows = [
        "| 지점 | 분류 | 유효 | 0.0 | 하한 미만 | 월 무효 | 일조 | 의심 일조 | kRs | MAE 0.16 | MAE 0.19 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in r.stations:
        if s.get("error"):
            rows.append(f"| {s['stn_id']} {s['name']} | {s['error']} |  |  |  |  |  |  |  |  |  |")
            continue
        e = s["eto"]
        rows.append(
            f"| {s['stn_id']} {s['name']} | {s['klass']} | {s['rs_valid']} | {s['rs_zero']} | "
            f"{s['rs_below_floor']} | {s['rs_month_invalid']} | {s['ss_days']} | "
            f"{s['suspect_sunshine_days']} | "
            f"{_f(s['krs_median'], 4)} | {_f(e and e['mae_016'])} | {_f(e and e['mae_019'])} |"
        )
    return (
        brief_text(r) + f"\n(kRs 후보 {KRS_LOW}·{KRS_HIGH}, ETo 오차 단위 mm/d)\n\n" + "\n".join(rows) + "\n"
    )


def write_outputs(r: AuditResult, out_dir: Path) -> Path:
    d = out_dir / OUT_NAME
    d.mkdir(parents=True, exist_ok=True)
    (d / "stations.json").write_text(
        json.dumps(_round(r.stations), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (d / "problems.json").write_text(json.dumps(r.problems, ensure_ascii=False, indent=1), encoding="utf-8")
    meta = dict(start=str(r.start), end=str(r.end), days=r.days, calls=r.calls, stations=len(r.stations))
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "collect_log.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in r.log.entries), encoding="utf-8"
    )
    (d / "brief.md").write_text(brief_text(r), encoding="utf-8")
    (d / "summary.md").write_text(summary_text(r), encoding="utf-8")
    return d


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="일사 점검 수집기 (사람이 눌러 실행, DB 쓰기 없음)")
    p.add_argument("--out", default="out", help="결과 폴더")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help="조회 일수(어제까지, 기본 365)")
    p.add_argument("--stations", help="지점 번호 쉼표 목록 (없으면 운영 지점 전부)")
    a = p.parse_args(argv)
    d = load_dictionaries(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))
    stations = [s.strip() for s in a.stations.split(",")] if a.stations else None
    r = run(d, _env("DATA_GO_KR"), stations=stations, days=a.days)
    out = write_outputs(r, Path(a.out))
    print(brief_text(r))
    print(f"결과 폴더: {out}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
