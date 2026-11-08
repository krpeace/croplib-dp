"""일사 점검 결과 + 해안선 → 실태표·보고서·관계 그림 만들기 (이슈 #48 PR-B). 오프라인 계산이다.

`radiation-audit`(PR-A)가 만든 결과 폴더(stations.json·meta.json)를 읽고, Natural Earth 해안선·땅 다각형으로
지점별 해안선 거리·섬 여부를 구해 `docs/data/`에 실태표(csv)·보고서(md)·관계 그림(svg)을 쓴다.
- API 키·DB를 쓰지 않는다(점검 결과 파일과 공개 해안선 자료만 읽는다).
- 해안선 원본은 `--coast-dir`(기본 `.cache/natural_earth`)에 내려받아 쓰고 저장소에 넣지 않는다.
"""

# ruff: noqa: E501  (한글 문장·SVG 문자열이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from croplib_dp.core.coast import Point, Polygon, classify_station
from croplib_dp.core.radiation_audit import compare_boundaries
from croplib_dp.core.radiation_report import coastal_csv, markdown_report, merge_geo, svg_scatter, to_csv
from croplib_dp.io import coast_data


def load_audit(audit_dir: Path) -> tuple[list[dict], dict]:
    """점검 수집기 결과 폴더 → (지점 목록, 메타). 없으면 조용히 넘어가지 않고 멈춘다."""
    try:
        stations = json.loads((audit_dir / "stations.json").read_text(encoding="utf-8"))
        meta = json.loads((audit_dir / "meta.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise SystemExit(
            f"점검 결과가 없습니다: {e.filename} (radiation-audit 실행 결과 폴더를 --audit-dir 로)"
        ) from e
    return stations, meta


def build_report(
    audit: Sequence[dict],
    meta: dict,
    lines: Sequence[Sequence[Point]],
    polygons: Sequence[Polygon],
    out_dir: Path,
    stamp: str,
    sources: str = "",
    coastal_csv_path: Path | None = None,
) -> dict[str, Path]:
    """지점별 해안선 거리·섬 판정 → 경계 후보 비교 → csv·md·svg 쓰기. 쓴 파일 경로를 돌려준다."""
    geo = {
        str(s["stn_id"]): classify_station(s["lat"], s["lon"], lines, polygons)
        for s in audit
        if not s.get("error")
    }
    stations = merge_geo(audit, geo)
    # 땅 밖이라 판정하지 못한 지점(unresolved)은 섬이 아닌 것으로 두되 보고서에 거리를 그대로 남긴다
    for s in stations:
        if s.get("island") is None and "island" in s:
            s["island"] = False
    comparison = compare_boundaries(stations)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "csv": out_dir / f"station_radiation_{stamp}.csv",
        "md": out_dir / f"station_radiation_{stamp}.md",
        "svg": out_dir / f"radiation_kRs_vs_coast_{stamp}.svg",
    }
    paths["csv"].write_text(to_csv(stations), encoding="utf-8-sig")  # 엑셀에서 한글이 깨지지 않게 BOM
    paths["md"].write_text(markdown_report(stations, comparison, meta, stamp, sources), encoding="utf-8")
    paths["svg"].write_text(svg_scatter(stations), encoding="utf-8")
    if coastal_csv_path is not None:  # 승인된 경계(10 km)로 지점별 kRs 목록 만들기 → io/coastal.py 가 읽는다
        coastal_csv_path.parent.mkdir(parents=True, exist_ok=True)
        coastal_csv_path.write_text(coastal_csv(stations, stamp), encoding="utf-8")
        paths["coastal"] = coastal_csv_path
    return paths


def _sources_text() -> str:
    """사용한 해안선 자료의 파일·커밋·sha256 (같은 파일인지 확인할 수 있게 보고서에 적는다)"""
    parts = []
    for src in coast_data.SOURCES.values():
        commit = src.url.split("/natural-earth-vector/")[1].split("/")[0][:7]
        parts.append(f"{src.filename}(커밋 {commit}, sha256 {src.sha256[:16]}…)")
    return "사용 파일: " + ", ".join(parts) + "."


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="일사 점검 결과 + 해안선 → 실태표·보고서·그림 (오프라인)")
    p.add_argument("--audit-dir", default="out/radiation_audit", help="radiation-audit 결과 폴더")
    p.add_argument(
        "--coast-dir", default=".cache/natural_earth", help="해안선 자료를 내려받아 두는 폴더(저장소 밖 권장)"
    )
    p.add_argument("--out", default="docs/data", help="결과를 쓸 폴더")
    p.add_argument("--coastal-csv", help="승인된 경계(해안선 10 km 이하 0.19)로 지점별 kRs 목록을 쓸 경로")
    p.add_argument("--stamp", help="파일 이름의 날짜(YYYYMMDD). 기본은 점검 기간 마지막 날")
    a = p.parse_args(argv)
    audit, meta = load_audit(Path(a.audit_dir))
    stamp = a.stamp or meta["end"].replace("-", "")
    coast_dir = Path(a.coast_dir)
    lines = coast_data.load_lines(coast_data.ensure_file(coast_dir, coast_data.SOURCES["coastline"]))
    polygons = coast_data.load_polygons(coast_data.ensure_file(coast_dir, coast_data.SOURCES["land"]))
    coastal = Path(a.coastal_csv) if a.coastal_csv else None
    paths = build_report(audit, meta, lines, polygons, Path(a.out), stamp, _sources_text(), coastal)
    for k, v in paths.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
