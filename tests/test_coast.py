"""해안선 거리·섬 판정 (core/coast.py, io/coast_data.py). 이슈 #48 PR-B. 해안선은 모두 가짜 좌표다.

규칙: 지점이 땅 다각형 안이면 정상. 땅 밖이어도 가장 가까운 땅까지 3 km 이내면 해상도 보정(거리 0 km, 표시).
가장 큰 땅 다각형이 육지이고, 다른 다각형 안은 섬이다(다리로 이어진 섬 포함).
"""

import hashlib
import json
import math

import pytest

from croplib_dp.core.coast import (
    RESOLUTION_TOLERANCE_KM,
    classify_station,
    distance_to_lines_km,
    mainland_index,
    point_in_polygon,
)
from croplib_dp.io import coast_data

KM_PER_DEG = 111.195


def _sq(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


# 육지: 126~130 × 34~39. 섬: 131~131.5 × 36~36.5
MAIN = [_sq(126, 34, 130, 39)]
ISLAND = [_sq(131, 36, 131.5, 36.5)]
POLYS = [ISLAND, MAIN]  # 순서가 달라도 가장 큰 것이 육지
LINES = [_sq(126, 34, 130, 39), _sq(131, 36, 131.5, 36.5)]


def test_distance_to_a_north_south_line_in_km():
    line = [[(127.0, 36.0), (127.0, 38.0)]]
    d = distance_to_lines_km(37.0, 127.1, line)
    assert d == pytest.approx(0.1 * KM_PER_DEG * math.cos(math.radians(37.0)), rel=0.005)


def test_distance_beyond_the_end_of_a_line_uses_the_end_point():
    line = [[(127.0, 36.0), (127.0, 38.0)]]
    d = distance_to_lines_km(39.0, 127.0, line)
    assert d == pytest.approx(1.0 * KM_PER_DEG, rel=0.005)


def test_distance_picks_the_nearest_of_several_lines_and_none_for_no_lines():
    lines = [[(127.0, 36.0), (127.0, 38.0)], [(128.0, 36.0), (128.0, 38.0)]]
    assert distance_to_lines_km(37.0, 127.9, lines) < distance_to_lines_km(37.0, 127.5, lines)
    assert distance_to_lines_km(37.0, 127.9, lines) == pytest.approx(
        0.1 * KM_PER_DEG * math.cos(math.radians(37.0)), rel=0.01
    )
    assert distance_to_lines_km(37.0, 127.9, []) is None


def test_point_in_polygon_with_a_hole():
    poly = [_sq(126, 36, 128, 38), _sq(126.9, 36.9, 127.1, 37.1)]
    assert point_in_polygon(126.5, 36.5, poly) is True
    assert point_in_polygon(129.0, 37.0, poly) is False
    assert point_in_polygon(127.0, 37.0, poly) is False  # 구멍 안


def test_mainland_is_the_largest_polygon_whatever_the_order():
    assert mainland_index(POLYS) == 1 and mainland_index(list(reversed(POLYS))) == 0


def test_station_on_the_mainland_is_not_an_island_and_has_a_real_coast_distance():
    r = classify_station(36.0, 128.0, LINES, POLYS)
    assert r["status"] == "inside" and r["island"] is False and r["corrected"] is False
    assert r["coast_km"] == pytest.approx(2.0 * KM_PER_DEG * math.cos(math.radians(36.0)), rel=0.01)


def test_station_on_an_island_is_an_island_even_if_a_bridge_could_connect_it():
    r = classify_station(36.25, 131.25, LINES, POLYS)
    assert r["status"] == "inside" and r["island"] is True
    assert 0 < r["coast_km"] < 30


def test_station_just_outside_land_is_corrected_to_zero_km_and_flagged():
    """해상도 한계로 땅 밖에 찍힌 지점(고산·군산·목포형): 가장 가까운 땅 3 km 이내 → 거리 0, 해상도 보정"""
    r = classify_station(36.2, 130.995, LINES, POLYS)  # 섬 서쪽 약 0.45 km 밖
    assert r["status"] == "corrected" and r["corrected"] is True
    assert r["coast_km"] == 0.0 and r["island"] is True  # 가장 가까운 땅 = 섬
    assert 0 < r["polygon_dist_km"] <= RESOLUTION_TOLERANCE_KM


def test_corrected_station_next_to_the_mainland_stays_mainland():
    r = classify_station(36.0, 130.01, LINES, POLYS)  # 육지 동쪽 해안 바깥 약 0.9 km
    assert r["status"] == "corrected" and r["island"] is False and r["coast_km"] == 0.0


def test_tolerance_is_3_km_and_a_farther_point_is_unresolved_not_guessed():
    assert RESOLUTION_TOLERANCE_KM == 3.0
    r = classify_station(36.0, 135.0, LINES, POLYS)  # 어느 땅에서도 멀다
    assert r["status"] == "unresolved" and r["island"] is None and r["corrected"] is False
    assert r["coast_km"] > 100  # 보정하지 않고 실제 거리를 그대로 보인다


# ── 해안선 자료 읽기·내려받기 (io/coast_data.py) ───────────────────────────
def _geojson(features):
    return json.dumps({"type": "FeatureCollection", "features": features}).encode()


def _line_feature(coords):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}}


def _multi_line_feature(lines):
    return {"type": "Feature", "geometry": {"type": "MultiLineString", "coordinates": lines}}


def _poly_feature(rings):
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": rings}}


def _multi_poly_feature(polys):
    return {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": polys}}


def test_load_lines_keeps_only_lines_in_the_bounding_box_and_reads_multilines(tmp_path):
    p = tmp_path / "coast.geojson"
    p.write_bytes(
        _geojson(
            [
                _line_feature([[127.0, 36.0], [127.5, 37.0]]),
                _line_feature([[10.0, 10.0], [11.0, 11.0]]),  # 한국에서 먼 곳
                _multi_line_feature([[[128.0, 35.0], [128.5, 35.5]], [[0.0, 0.0], [1.0, 1.0]]]),
            ]
        )
    )
    lines = coast_data.load_lines(p, bbox=(122, 32, 133, 40))
    assert len(lines) == 2 and [round(x) for x, _ in lines[0]] == [127, 128]


def test_load_polygons_reads_polygon_and_multipolygon_inside_the_bbox(tmp_path):
    p = tmp_path / "land.geojson"
    p.write_bytes(
        _geojson(
            [
                _poly_feature([_sq(126, 34, 130, 39)]),
                _multi_poly_feature([[_sq(131, 36, 131.5, 36.5)], [_sq(10, 10, 11, 11)]]),
            ]
        )
    )
    polys = coast_data.load_polygons(p, bbox=(122, 32, 133, 40))
    assert len(polys) == 2 and all(isinstance(rings[0][0], tuple) for rings in polys)


def test_ensure_file_downloads_once_and_checks_sha256(tmp_path):
    data = b'{"type":"FeatureCollection","features":[]}'
    spec = coast_data.Source(
        "x.geojson", "https://example.invalid/x.geojson", hashlib.sha256(data).hexdigest()
    )
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        return 200, data

    path = coast_data.ensure_file(tmp_path, spec, http=fake_get)
    assert path.read_bytes() == data and len(calls) == 1
    coast_data.ensure_file(tmp_path, spec, http=fake_get)  # 이미 있고 해시가 맞으면 다시 받지 않는다
    assert len(calls) == 1


def test_ensure_file_refuses_a_download_with_the_wrong_hash(tmp_path):
    spec = coast_data.Source("x.geojson", "https://example.invalid/x.geojson", "0" * 64)
    with pytest.raises(RuntimeError, match="sha256"):
        coast_data.ensure_file(tmp_path, spec, http=lambda url, timeout: (200, b"tampered"))
    assert not (tmp_path / "x.geojson").exists()  # 어긋난 파일은 남기지 않는다


def test_ensure_file_redownloads_when_the_cached_file_is_broken(tmp_path):
    data = b"[]"
    spec = coast_data.Source(
        "x.geojson", "https://example.invalid/x.geojson", hashlib.sha256(data).hexdigest()
    )
    (tmp_path / "x.geojson").write_bytes(b"broken")
    path = coast_data.ensure_file(tmp_path, spec, http=lambda url, timeout: (200, data))
    assert path.read_bytes() == data


def test_ensure_file_fails_loudly_on_http_error(tmp_path):
    spec = coast_data.Source("x.geojson", "https://example.invalid/x.geojson", "0" * 64)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        coast_data.ensure_file(tmp_path, spec, http=lambda url, timeout: (404, b"not found"))


def test_sources_are_pinned_to_a_commit_and_a_full_sha256():
    for s in coast_data.SOURCES.values():
        assert "/natural-earth-vector/" in s.url and "master" not in s.url
        assert len(s.sha256) == 64 and set(s.sha256) <= set("0123456789abcdef")
