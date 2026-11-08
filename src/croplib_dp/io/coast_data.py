"""해안선·땅 다각형 자료 읽기 (Natural Earth 10m, 퍼블릭 도메인, 이슈 #48 PR-B).

- 원본은 **저장소에 넣지 않는다.** 로컬 폴더(기본 `.cache/natural_earth`)에 내려받아 쓰고, 커밋 고정 주소와
  sha256 으로 같은 파일임을 확인한다. 계산 결과·방법·버전만 `docs/data/`에 둔다.
- 출처 표기는 필요 없지만 "Made with Natural Earth."를 문서에 적는다. 이용 조건: 전부 퍼블릭 도메인
  (https://github.com/nvkelso/natural-earth-vector/blob/master/LICENSE.md).
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from croplib_dp.core.coast import Point, Polygon

# 한국 주변 (서경, 남위, 동경, 북위): 이 상자 안에 걸리는 선·다각형만 쓴다
KOREA_BBOX = (122.0, 32.0, 133.0, 40.0)
_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
TIMEOUT_S = 180


@dataclass(frozen=True)
class Source:
    filename: str
    url: str
    sha256: str


SOURCES = {
    "coastline": Source(
        "ne_10m_coastline.geojson",
        f"{_BASE}/a726d63745ff99cf26a03ae1c7cfb0d4f12fa517/geojson/ne_10m_coastline.geojson",
        "6f75ae0e0de157b14946e2255eb1f5486d9a13819032e26d4610852d296788f6",
    ),
    "land": Source(
        "ne_10m_land.geojson",
        f"{_BASE}/ace5fed0eaf3c6c03c951e75b439ba8fffbc218e/geojson/ne_10m_land.geojson",
        "1ac90796408bc6ad6911d69448485d3c4dbf2190370080368a09976e1c9f7416",
    ),
}


def _http_get(url: str, timeout: float) -> tuple[int, bytes]:
    r = requests.get(url, timeout=timeout)
    return r.status_code, r.content


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def ensure_file(
    directory: Path, src: Source, http: Callable[[str, float], tuple[int, bytes]] = _http_get
) -> Path:
    """폴더에 파일이 있고 sha256 이 맞으면 그대로 쓰고, 아니면 받는다. 받은 파일의 sha256 이 어긋나면
    저장하지 않고 오류를 낸다(조용히 넘어가지 않음)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / src.filename
    if path.exists() and _sha(path.read_bytes()) == src.sha256:
        return path
    status, body = http(src.url, TIMEOUT_S)
    if status != 200:
        raise RuntimeError(f"{src.filename} 내려받기 실패: HTTP {status}")
    if _sha(body) != src.sha256:
        raise RuntimeError(
            f"{src.filename} sha256 이 기대값과 다름 (받은 {_sha(body)[:16]}…) — 저장하지 않음"
        )
    path.write_bytes(body)
    return path


def _in_bbox(pts, bbox) -> bool:
    x0, y0, x1, y1 = bbox
    return any(x0 <= x <= x1 and y0 <= y <= y1 for x, y in pts)


def _ring(coords) -> list[Point]:
    return [(float(x), float(y)) for x, y, *_ in coords]


def load_lines(path: Path, bbox=KOREA_BBOX) -> list[list[Point]]:
    """GeoJSON 의 LineString·MultiLineString → 선 목록(bbox 에 걸리는 것만)"""
    out: list[list[Point]] = []
    for f in json.loads(path.read_text(encoding="utf-8"))["features"]:
        g = f["geometry"]
        parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        out += [_ring(p) for p in parts if _in_bbox(p, bbox)]
    return out


def load_polygons(path: Path, bbox=KOREA_BBOX) -> list[Polygon]:
    """GeoJSON 의 Polygon·MultiPolygon → 다각형 목록(바깥 고리가 bbox 에 걸리는 것만)"""
    out: list[Polygon] = []
    for f in json.loads(path.read_text(encoding="utf-8"))["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        out += [[_ring(r) for r in p] for p in polys if _in_bbox(p[0], bbox)]
    return out
