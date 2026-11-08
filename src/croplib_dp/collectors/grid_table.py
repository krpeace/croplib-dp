"""운영 지점의 예보 격자 표(CSV)와 확인 보고서를 만든다. 이슈 #72(#28 S1, 결정 0025).

**DB에 쓰지 않는다.** 서버 키가 아니라 읽기 전용 키(`SUPABASE_READ_KEY`)로 `stations` 표 하나만 읽거나(`--source db`),
저장소의 지점 목록 CSV(`docs/data/stations_kma_*.csv`)에서 만든다(`--source csv`, 기본). 사람이 손으로 한 번 돌린다.
`stations`의 격자 값은 CropMon이 주인이라 이 표로 CropMon이 시드를 넣는다(이 저장소는 넣지 않는다).

  python -m croplib_dp.collectors.grid_table --source csv --csv docs/data/stations_kma_20261028.csv --out docs/data
  python -m croplib_dp.collectors.grid_table --source db --out out        # SUPABASE_URL, SUPABASE_READ_KEY 필요

결과: `stations_grid_<날짜>.csv`(열: stn_id, name_ko, lat, lon, grid_nx, grid_ny, method, near_boundary)와 같은 이름의 `.md`
(겹침 확인, 경계 근접 지점, db 모드에서는 DB 격자와의 비교). 격자가 서로 겹치면 종료 코드 1.
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Any

from croplib_dp.core import grid as G
from croplib_dp.io.dictionaries import read_table

DEFAULT_CSV = "docs/data/stations_kma_20261028.csv"
DB_QUERY = "select=stn_id,name_ko,lat,lon,grid_nx,grid_ny,is_active&order=stn_id"


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"환경변수 {name}가 없습니다 (읽기 전용 키는 GitHub Secrets 또는 .env)")
    return v


def read_csv_stations(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def compare_with_db(stations: list[dict]) -> dict[str, list[dict]]:
    """운영 지점별 DB 격자와 계산값 비교. 결과: ok / differs / missing 목록."""
    out: dict[str, list[dict]] = {"ok": [], "differs": [], "missing": [], "unchecked": []}
    for s in stations:
        if str(s.get("is_active")).strip().lower() != "true":
            continue
        c = G.check_station_grid(s)
        out[c.status].append(
            dict(stn_id=str(s["stn_id"]), name_ko=s.get("name_ko") or "", db=c.db, calc=c.calc)
        )
    return out


def report_markdown(date: str, rows: list[dict], source: str, db_cmp: dict[str, list[dict]] | None) -> str:
    dup = G.overlaps(rows)
    names = {r["stn_id"]: r["name_ko"] for r in rows}
    near = [r for r in rows if r["near_boundary"]]
    L = [
        f"# 운영 지점 예보 격자 ({date[:4]}-{date[4:6]}-{date[6:]})",
        "",
        f"- 계산 방식: `{G.GRID_METHOD}`(Lambert 정각원추, 지구 반지름 {G.EARTH_RADIUS_KM}km, 격자 {G.GRID_KM:g}km, "
        f"표준위도 {G.STD_LAT1_DEG:g}°·{G.STD_LAT2_DEG:g}°, 기준점 {G.ORIGIN_LON_DEG:g}°E·{G.ORIGIN_LAT_DEG:g}°N → ({G.ORIGIN_X}, {G.ORIGIN_Y}), 반올림 `floor(값+0.5)`)",
        f"- 지점 출처: {source}",
        f"- 지점: 운영 {len(rows)}곳. 파일: `stations_grid_{date}.csv`",
        "",
        "## 겹침 확인",
        "",
    ]
    if dup:
        L.append(f"**겹침 {len(dup)}곳** — 같은 격자를 쓰는 지점이 있습니다(예보 호출이 중복됩니다).")
        L += [
            f"- 격자 ({nx},{ny}): " + ", ".join(f"{s} {names.get(s, '')}".strip() for s in ids)
            for (nx, ny), ids in sorted(dup.items())
        ]
    else:
        distinct = len({(r["grid_nx"], r["grid_ny"]) for r in rows})
        L.append(f"겹침 0 — {len(rows)}곳의 격자가 서로 다릅니다(서로 다른 격자 {distinct}개).")
    L += ["", "## 경계 근접 지점", ""]
    L.append(
        f"경계 근접 {len(near)}곳 — 반올림 경계까지 {G.NEAR_BOUNDARY_CELLS:g}칸 이내라 위경도가 조금만 달라도 한 칸(5km) 옆으로 갈 수 있습니다. "
        "CropMon이 시드를 넣기 전에 계산값을 한 번 더 확인하세요."
    )
    L += ["", "| stn_id | 이름 | 위도 | 경도 | 격자 | 경계까지(칸) |", "|---|---|---:|---:|---|---:|"]
    L += [
        f"| {r['stn_id']} | {r['name_ko']} | {r['lat']} | {r['lon']} | {r['grid_nx']},{r['grid_ny']} | {G.boundary_margin(r['lat'], r['lon']):.3f} |"
        for r in near
    ]
    L += ["", "## DB 격자 비교", ""]
    if db_cmp is None:
        L.append("하지 않음(csv 모드). DB 값과 비교하려면 `--source db`로 돌립니다.")
    else:
        L.append(
            f"일치 {len(db_cmp['ok'])}곳 / 다름 {len(db_cmp['differs'])}곳 / 비어 있음 {len(db_cmp['missing'])}곳"
            + (f" / 비교 못 함 {len(db_cmp['unchecked'])}곳(좌표 없음)" if db_cmp["unchecked"] else "")
            + ". DB에 격자가 있으면 DB 값이 기준입니다(결정 0025)."
        )
        for key, title in (("differs", "DB와 계산값이 다른 지점"), ("missing", "DB 격자가 비어 있는 지점")):
            if db_cmp[key]:
                L += ["", f"### {title}", ""]
                L += [
                    f"- {x['stn_id']} {x['name_ko']}: DB {x['db'] and ','.join(map(str, x['db']))} / 계산 {x['calc'] and ','.join(map(str, x['calc']))}"
                    for x in db_cmp[key]
                ]
    return "\n".join(L) + "\n"


def write_outputs(
    out: Path, date: str, rows: list[dict], source: str, db_cmp: dict | None
) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"stations_grid_{date}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(G.GRID_CSV_COLUMNS)
        for r in rows:
            w.writerow([str(r[c]).lower() if isinstance(r[c], bool) else r[c] for c in G.GRID_CSV_COLUMNS])
    md_path = out / f"stations_grid_{date}.md"
    md_path.write_text(report_markdown(date, rows, source, db_cmp), encoding="utf-8")
    return csv_path, md_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="운영 지점 예보 격자 표(CSV)와 겹침·경계 보고서 (DB 쓰기 없음)")
    p.add_argument("--source", choices=["csv", "db"], default="csv", help="지점 목록 출처(기본 csv)")
    p.add_argument("--csv", default=DEFAULT_CSV, help="csv 모드: 지점 목록 CSV")
    p.add_argument("--out", default="docs/data", help="결과 폴더")
    p.add_argument("--date", help="파일 이름의 날짜 YYYYMMDD (없으면 오늘 한국 날짜)")
    a = p.parse_args(argv)
    if a.date:
        date = a.date
    else:
        from croplib_dp.collectors.water import kst_now

        date = f"{kst_now():%Y%m%d}"
    stations: list[dict[str, Any]]
    if a.source == "db":
        url, read_key = _env("SUPABASE_URL"), _env("SUPABASE_READ_KEY")
        stations = read_table(url, read_key, "stations", query=DB_QUERY)
        source, db_cmp = "DB `stations` 표(읽기 전용 키)", compare_with_db(stations)
    else:
        stations = read_csv_stations(Path(a.csv))
        source, db_cmp = f"`{a.csv}`", None
    rows = G.grid_rows(stations)
    csv_path, md_path = write_outputs(Path(a.out), date, rows, source, db_cmp)
    dup = G.overlaps(rows)
    print(f"운영 {len(rows)}곳, 겹침 {len(dup)}곳, 경계 근접 {sum(r['near_boundary'] for r in rows)}곳")
    print(f"{csv_path}\n{md_path}")
    return 1 if dup else 0


if __name__ == "__main__":
    sys.exit(main())
