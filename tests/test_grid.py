"""예보 격자 (core/grid.py, collectors/grid_table.py). 이슈 #72(#28 S1).

규칙(2026-11-08 확정): 위경도 → 기상청 단기예보 격자(Lambert 정각원추). **DB에 격자가 있으면 DB 값이 기준**이고
계산값과 다르면 경고만(예보 계속). 격자가 비어 있는 has_forecast 지점만 예보를 건너뛴다.
대표 10곳 DB 격자(2026-11-08 사용자 SQL, 공개 값)와 계산값이 10/10 일치해야 한다. 입력은 모두 공개 값 또는 가짜 값이다.
"""

# ruff: noqa: E501

import csv
import json
import math
from pathlib import Path

import pytest

from croplib_dp.collectors import grid_table as GT
from croplib_dp.core import grid as G

ROOT = Path(__file__).resolve().parent.parent
KMA_CSV = ROOT / "docs" / "data" / "stations_kma_20261028.csv"
GRID_CSV = ROOT / "docs" / "data" / "stations_grid_20261108.csv"

# stations 표의 대표 10곳 (stn_id, 이름, lat, lon, grid_nx, grid_ny) — 2026-11-08 18:10 사용자 실행 SQL 결과
DB_TEN = [
    ("101", "춘천", 37.90262, 127.7357, 73, 134),
    ("119", "수원", 37.25746, 126.983, 60, 120),
    ("129", "서산", 36.77658, 126.4939, 52, 109),
    ("131", "청주", 36.63924, 127.44066, 68, 107),
    ("136", "안동", 36.57293, 128.70733, 90, 106),
    ("146", "전주", 35.84092, 127.11718, 63, 89),
    ("156", "광주", 35.17294, 126.89156, 59, 75),
    ("189", "서귀포", 33.24616, 126.5653, 53, 32),
    ("192", "진주", 35.16378, 128.04004, 79, 75),
    ("216", "태백", 37.17038, 128.98929, 95, 119),
]
NEAR_12 = {"105", "121", "129", "138", "174", "226", "232", "252", "253", "255", "257", "296"}


def _kma_rows():
    return list(csv.DictReader(KMA_CSV.open(encoding="utf-8-sig")))


def _active():
    return [r for r in _kma_rows() if r["is_active"].lower() == "true"]


# ── 변환식 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("stn,name,lat,lon,nx,ny", DB_TEN)
def test_latlon_to_grid_matches_each_of_the_ten_db_grids(stn, name, lat, lon, nx, ny):
    assert G.latlon_to_grid(lat, lon) == (nx, ny), name


def test_ten_of_ten_match_the_db():
    ok = sum(G.latlon_to_grid(la, lo) == (nx, ny) for _, _, la, lo, nx, ny in DB_TEN)
    assert ok == 10


def test_known_official_grids():
    assert G.latlon_to_grid(37.5665, 126.9780) == (60, 127)  # 서울 중구(시청) 공식 격자
    assert G.latlon_to_grid(38.0, 126.0) == (43, 136)  # 기준점


def test_corners_of_the_peninsula_and_islands_are_inside_the_grid():
    for lat, lon in [
        (37.4812, 130.9040),
        (37.2397, 131.8686),
        (33.2461, 126.5653),
        (37.9, 124.7),
    ]:  # 울릉·독도·서귀포·백령 근처
        nx, ny = G.latlon_to_grid(lat, lon)
        assert 1 <= nx <= 149 and 1 <= ny <= 253


@pytest.mark.parametrize(
    "lat,lon",
    [(math.nan, 127.0), (37.0, math.inf), (95.0, 127.0), (37.0, 400.0), (None, 127.0), ("x", 127.0)],
)
def test_bad_coordinates_raise_value_error(lat, lon):
    with pytest.raises(ValueError):
        G.latlon_to_grid(lat, lon)


def test_boundary_margin_is_the_distance_to_the_rounding_edge_in_cells():
    # 서산 129: 원값 y 109.987 → 반올림 경계까지 0.013칸
    m = G.boundary_margin(36.77658, 126.4939)
    assert 0.0 <= m < 0.02 and G.near_boundary(36.77658, 126.4939)
    # 춘천 101: 원값 (73.171, 134.712) → 경계에서 0.171 이상
    assert G.boundary_margin(37.90262, 127.7357) > 0.1 and not G.near_boundary(37.90262, 127.7357)


# ── 지점 한 곳의 판단 ────────────────────────────────────────────────────
def _st(**kw):
    base = dict(stn_id="101", lat=37.90262, lon=127.7357, grid_nx=73, grid_ny=134, has_forecast=True)
    return {**base, **kw}


def test_check_ok_when_db_equals_calc():
    c = G.check_station_grid(_st())
    assert (c.status, c.db, c.calc) == ("ok", (73, 134), (73, 134))


def test_check_missing_when_db_grid_is_empty():
    for kw in (dict(grid_nx=None), dict(grid_ny=None), dict(grid_nx=None, grid_ny=None)):
        c = G.check_station_grid(_st(**kw))
        assert c.status == "missing" and c.db is None and c.calc == (73, 134)


def test_check_missing_even_without_coordinates():
    c = G.check_station_grid(_st(grid_nx=None, grid_ny=None, lat=None, lon=None))
    assert c.status == "missing" and c.calc is None


def test_check_differs_when_db_is_a_different_cell_and_db_stays_the_reference():
    c = G.check_station_grid(_st(grid_nx=74))
    assert c.status == "differs" and c.db == (74, 134) and c.calc == (73, 134)


def test_check_unchecked_when_db_has_a_grid_but_no_coordinates():
    c = G.check_station_grid(_st(lat=None, lon=None))
    assert c.status == "unchecked" and c.db == (73, 134) and c.calc is None


def test_forecast_ready_needs_has_forecast_and_a_db_grid_not_a_matching_one():
    assert G.forecast_ready(_st())
    assert G.forecast_ready(_st(grid_nx=74))  # 다르다고 건너뛰지 않는다(DB 값이 기준)
    assert G.forecast_ready(_st(lat=None, lon=None))
    assert not G.forecast_ready(_st(grid_nx=None))
    assert not G.forecast_ready(_st(grid_ny=None))
    assert not G.forecast_ready(_st(has_forecast=False))
    assert not G.forecast_ready(_st(has_forecast=None))


def test_db_values_may_arrive_as_strings_or_decimals():
    assert (
        G.check_station_grid(_st(grid_nx="73", grid_ny="134.0", lat="37.90262", lon="127.7357")).status
        == "ok"
    )


# ── 운영 97곳 ───────────────────────────────────────────────────────────
def test_the_97_active_stations_have_97_different_cells():
    act = _active()
    assert len(act) == 97
    cells = [G.latlon_to_grid(float(r["lat"]), float(r["lon"])) for r in act]
    assert len(set(cells)) == 97
    assert G.overlaps(G.grid_rows(act)) == {}


def test_overlaps_reports_stations_sharing_a_cell():
    rows = [
        dict(stn_id="1", grid_nx=10, grid_ny=20),
        dict(stn_id="2", grid_nx=10, grid_ny=20),
        dict(stn_id="3", grid_nx=11, grid_ny=20),
    ]
    assert G.overlaps(rows) == {(10, 20): ["1", "2"]}


def test_exactly_twelve_stations_are_near_a_rounding_boundary():
    rows = G.grid_rows(_active())
    assert {r["stn_id"] for r in rows if r["near_boundary"]} == NEAR_12


def test_grid_rows_only_active_sorted_by_number_with_the_method_name():
    rows = G.grid_rows(_kma_rows())
    assert len(rows) == 97 and [int(r["stn_id"]) for r in rows] == sorted(int(r["stn_id"]) for r in rows)
    assert rows[0].keys() == {
        "stn_id",
        "name_ko",
        "lat",
        "lon",
        "grid_nx",
        "grid_ny",
        "method",
        "near_boundary",
    }
    assert all(r["method"] == G.GRID_METHOD for r in rows)
    assert not ({"116", "164", "175", "176", "187", "214", "256", "265"} & {r["stn_id"] for r in rows})


def test_the_ten_representative_stations_in_the_table_equal_the_db_grids():
    rows = {r["stn_id"]: r for r in G.grid_rows(_active())}
    for stn, _, _, _, nx, ny in DB_TEN:
        assert (rows[stn]["grid_nx"], rows[stn]["grid_ny"]) == (nx, ny), stn


# ── 올려 둔 CSV ─────────────────────────────────────────────────────────
def test_committed_csv_is_what_the_tool_makes_from_the_station_list():
    expect = G.grid_rows(_active())
    got = list(csv.DictReader(GRID_CSV.open(encoding="utf-8-sig")))
    assert list(got[0].keys()) == list(G.GRID_CSV_COLUMNS)
    assert len(got) == 97
    for e, g in zip(expect, got, strict=True):
        assert g["stn_id"] == e["stn_id"] and g["name_ko"] == e["name_ko"]
        assert (int(g["grid_nx"]), int(g["grid_ny"])) == (e["grid_nx"], e["grid_ny"])
        assert g["method"] == G.GRID_METHOD and (g["near_boundary"] == "true") == e["near_boundary"]


def test_committed_csv_has_no_secret_or_farm_data():
    text = GRID_CSV.read_text(encoding="utf-8")
    assert "service_role" not in text and "eyJ" not in text


# ── 표 만드는 도구 (collectors/grid_table.py) ───────────────────────────────
def test_tool_from_csv_writes_the_csv_and_a_report_with_overlap_and_boundary_results(tmp_path):
    code = GT.main(["--source", "csv", "--csv", str(KMA_CSV), "--out", str(tmp_path), "--date", "20261108"])
    assert code == 0
    out = tmp_path / "stations_grid_20261108.csv"
    md = (tmp_path / "stations_grid_20261108.md").read_text(encoding="utf-8")
    assert out.read_text(encoding="utf-8-sig").splitlines()[0] == ",".join(G.GRID_CSV_COLUMNS)
    assert "97곳" in md and "겹침 0" in md and "경계 근접 12곳" in md
    for stn in NEAR_12:
        assert stn in md


def test_tool_report_says_overlap_when_two_stations_share_a_cell(tmp_path):
    src = tmp_path / "s.csv"
    src.write_text(
        "stn_id,name_ko,sido,lat,lon,elev_m,wind_height_m,measures_sunlight,is_active,source_note\n"
        "1,가짜가,서울,37.5665,126.978,1,10,,true,x\n2,가짜나,서울,37.5666,126.9781,1,10,,true,x\n",
        encoding="utf-8",
    )
    code = GT.main(["--source", "csv", "--csv", str(src), "--out", str(tmp_path), "--date", "20260101"])
    md = (tmp_path / "stations_grid_20260101.md").read_text(encoding="utf-8")
    assert code == 1 and "겹침 1곳" in md and "가짜가" in md and "가짜나" in md


def test_tool_from_db_compares_with_the_db_grid_and_reports_differences(tmp_path, monkeypatch):
    rows = [
        dict(
            stn_id="101", name_ko="춘천", lat=37.90262, lon=127.7357, grid_nx=73, grid_ny=134, is_active=True
        ),
        dict(
            stn_id="119", name_ko="수원", lat=37.25746, lon=126.983, grid_nx=61, grid_ny=120, is_active=True
        ),  # 일부러 다르게
        dict(stn_id="300", name_ko="새곳", lat=36.0, lon=128.0, grid_nx=None, grid_ny=None, is_active=True),
    ]
    seen = {}

    def fake_read(base, key, table, http=None, query=""):
        seen.update(base=base, key=key, table=table)
        return rows

    monkeypatch.setattr(GT, "read_table", fake_read)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_READ_KEY", "read-key-fake")
    code = GT.main(["--source", "db", "--out", str(tmp_path), "--date", "20260102"])
    md = (tmp_path / "stations_grid_20260102.md").read_text(encoding="utf-8")
    assert (
        code == 0 and seen["table"] == "stations" and seen["key"] == "read-key-fake"
    )  # 읽기 키로 stations 만
    assert (
        "DB 격자 비교" in md
        and "일치 1곳" in md
        and "다름 1곳" in md
        and "비어 있음 1곳" in md
        and "수원" in md
    )


def test_tool_from_db_needs_the_read_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_READ_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    with pytest.raises(SystemExit):
        GT.main(["--source", "db", "--out", str(tmp_path), "--date", "20260102"])


def test_tool_never_writes_to_the_db():
    src = (ROOT / "src" / "croplib_dp" / "collectors" / "grid_table.py").read_text(encoding="utf-8")
    assert "SERVICE_ROLE" not in src and "supabase_write" not in src and "SupabaseWriter" not in src


def test_json_dump_of_a_check_is_plain(tmp_path):
    c = G.check_station_grid(_st(grid_nx=74))
    json.dumps(dict(status=c.status, db=c.db, calc=c.calc))
