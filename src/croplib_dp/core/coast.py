"""해안선까지 거리와 섬 판정 (이슈 #48 PR-B, 결정 0021). 순수 함수만 둔다: 파일·API를 모른다.

좌표는 (경도, 위도) 도(°). 짧은 거리라 지점 위도 기준 평면(등장방형)으로 근사한다.
- 해안선까지 거리: 해안선 선분들까지의 최단 직선거리(km).
- 섬: 땅 다각형 중 **가장 큰 것이 육지**이고, 지점이 다른 다각형 안에 있으면 섬이다(다리로 이어진 섬 포함).
- 해상도 보정: 해안선 자료(Natural Earth 10m, 축척 1:1천만)는 위치 오차가 수 km라, 해안 지점이 땅 다각형
  밖에 찍힐 수 있다(고산·군산·목포형). 가장 가까운 땅까지 3 km 이내이면 그 땅으로 보고 거리를 0 km로 두며
  "보정" 표시를 남긴다. 3 km보다 멀면 짐작하지 않고 `unresolved`로 남긴다.
"""

import math
from collections.abc import Sequence

Point = tuple[float, float]  # (경도, 위도)
Ring = Sequence[Point]
Polygon = Sequence[Ring]  # 첫 고리가 바깥, 나머지는 구멍

KM_PER_DEG = 111.195  # 위도 1°당 km
RESOLUTION_TOLERANCE_KM = 3.0


def _seg_distance(ax: float, ay: float, bx: float, by: float) -> float:
    """원점에서 선분 (a, b)까지 거리"""
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
    return math.hypot(ax + t * dx, ay + t * dy)


def distance_to_lines_km(lat: float, lon: float, lines: Sequence[Sequence[Point]]) -> float | None:
    """지점에서 선(꼭짓점 목록) 여러 개까지의 최단 거리(km). 선이 없으면 None."""
    k = math.cos(math.radians(lat)) * KM_PER_DEG
    best: float | None = None
    for ln in lines:
        pts = [((x - lon) * k, (y - lat) * KM_PER_DEG) for x, y in ln]
        for (ax, ay), (bx, by) in zip(pts, pts[1:], strict=False):
            if best is not None and (
                min(ax, bx) > best or max(ax, bx) < -best or min(ay, by) > best or max(ay, by) < -best
            ):
                continue  # 지금까지 찾은 거리보다 멀리 있는 선분은 건너뛴다
            d = _seg_distance(ax, ay, bx, by)
            if best is None or d < best:
                best = d
    return best


def _in_ring(x: float, y: float, ring: Ring) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def point_in_polygon(lon: float, lat: float, polygon: Polygon) -> bool:
    """다각형(바깥 고리 + 구멍들) 안이면 True. 구멍 안은 False."""
    return _in_ring(lon, lat, polygon[0]) and not any(_in_ring(lon, lat, h) for h in polygon[1:])


def _area(ring: Ring) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False))) / 2


def mainland_index(polygons: Sequence[Polygon]) -> int:
    """가장 넓은 다각형의 번호 = 육지(한반도가 속한 큰 땅)"""
    return max(range(len(polygons)), key=lambda i: _area(polygons[i][0]))


def classify_station(
    lat: float,
    lon: float,
    lines: Sequence[Sequence[Point]],
    polygons: Sequence[Polygon],
    tolerance_km: float = RESOLUTION_TOLERANCE_KM,
) -> dict:
    """한 지점의 해안선 거리·섬 여부.
    반환: status(inside / corrected / unresolved), coast_km, island(True·False·None), corrected,
    polygon_dist_km(보정할 때 가장 가까운 땅까지의 거리)."""
    main = mainland_index(polygons)
    for i, poly in enumerate(polygons):
        if point_in_polygon(lon, lat, poly):
            return dict(
                status="inside",
                coast_km=distance_to_lines_km(lat, lon, lines),
                island=i != main,
                corrected=False,
                polygon_dist_km=0.0,
            )
    # 땅 밖: 가장 가까운 땅 다각형(바깥 고리)까지 거리
    dists = [(distance_to_lines_km(lat, lon, [poly[0]]), i) for i, poly in enumerate(polygons)]
    d, i = min(dists, key=lambda t: (t[0] is None, t[0]))
    if d is not None and d <= tolerance_km:
        return dict(status="corrected", coast_km=0.0, island=i != main, corrected=True, polygon_dist_km=d)
    return dict(
        status="unresolved",
        coast_km=distance_to_lines_km(lat, lon, lines),
        island=None,
        corrected=False,
        polygon_dist_km=d,
    )
