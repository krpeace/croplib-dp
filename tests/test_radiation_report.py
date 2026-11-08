"""실태표·경계 후보 보고서 (core/radiation_report.py, collectors/radiation_report.py). 이슈 #48 PR-B.

입력은 모두 가짜 값이다(가짜 점검 결과 + 가짜 해안선).
"""

# ruff: noqa: E501  (한글 문장·SVG 문자열이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import csv
import io
import json
import xml.etree.ElementTree as ET

import pytest

from croplib_dp.collectors import radiation_report as RR
from croplib_dp.core.radiation_audit import compare_boundaries
from croplib_dp.core.radiation_report import (
    COASTAL_CSV_COLUMNS,
    CSV_COLUMNS,
    coastal_csv,
    krs_groups,
    markdown_report,
    merge_geo,
    svg_scatter,
    to_csv,
)


def _eto(mae16, mae19, n=300):
    h = dict(n=n // 2, mae_016=mae16, mae_019=mae19)
    return dict(
        n=n, bias_016=-mae16, mae_016=mae16, bias_019=mae19, mae_019=mae19,
        best_krs=0.19 if mae19 < mae16 else 0.16, h1=h, h2=h,
    )  # fmt: skip


def _aud(stn, name, klass="정상", krs=0.17, mae16=0.3, mae19=0.4, elev=50.0, **kw):
    normal = klass == "정상"
    d = dict(
        stn_id=stn, name=name, lat=36.0, lon=127.0, elev_m=elev, db_measures_sunlight=None, klass=klass,
        days_total=365, rs_values=365 if normal else 0, rs_zero=0, rs_below_floor=0, rs_month_invalid=0,
        rs_valid=365 if normal else 0, rs_sparse=False, ss_days=365, temp_days=365, suspect_sunshine_days=2,
        krs_n=365 if normal else 0, krs_median=krs if normal else None,
        krs_q1=krs - 0.01 if normal else None, krs_q3=krs + 0.01 if normal else None,
        krs_season={"3~5": krs, "6~8": krs, "9~11": krs, "12~2": None if normal else None},
        eto=_eto(mae16, mae19) if normal else None,
    )  # fmt: skip
    d.update(kw)
    return d


AUDIT = [
    _aud("1", "가", krs=0.20, mae16=0.5, mae19=0.2),  # 섬·해안
    _aud("2", "나", krs=0.19, mae16=0.4, mae19=0.3),  # 해안 8 km
    _aud("3", "다", krs=0.16, mae16=0.1, mae19=0.4),  # 내륙 60 km
    _aud("4", "라", krs=0.15, mae16=0.1, mae19=0.5),  # 내륙 90 km
    _aud("5", "마", klass="일사 없음·일조 있음"),
    dict(stn_id="6", name="바", error="ASOS 실패"),
]
GEO = {
    "1": dict(status="inside", coast_km=1.0, island=True, corrected=False, polygon_dist_km=0.0),
    "2": dict(status="inside", coast_km=8.0, island=False, corrected=False, polygon_dist_km=0.0),
    "3": dict(status="inside", coast_km=60.0, island=False, corrected=False, polygon_dist_km=0.0),
    "4": dict(status="inside", coast_km=90.0, island=False, corrected=False, polygon_dist_km=0.0),
    "5": dict(status="corrected", coast_km=0.0, island=False, corrected=True, polygon_dist_km=1.2),
}
META = dict(start="2025-11-08", end="2026-11-07", days=365, calls=194, stations=6)


def test_merge_geo_adds_distance_island_and_correction_flag_and_keeps_failed_stations():
    st = merge_geo(AUDIT, GEO)
    by = {s["stn_id"]: s for s in st}
    assert (by["1"]["coast_km"], by["1"]["island"], by["1"]["resolution_corrected"]) == (1.0, True, False)
    assert (by["5"]["coast_km"], by["5"]["resolution_corrected"]) == (0.0, True)
    assert "coast_km" not in by["6"] and by["6"]["error"] == "ASOS 실패"


def test_csv_has_the_planned_columns_one_row_per_station_and_blank_for_missing():
    text = to_csv(merge_geo(AUDIT, GEO))
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0].keys()) == list(CSV_COLUMNS) and len(rows) == 6
    for col in ("elev_m", "island", "coast_km", "resolution_corrected", "klass", "suspect_sunshine_days",
                "krs_median", "krs_3_5", "krs_6_8", "krs_9_11", "krs_12_2", "eto_mae_016", "eto_mae_019"):  # fmt: skip
        assert col in CSV_COLUMNS
    r1, r5, r6 = rows[0], rows[4], rows[5]
    assert r1["island"] == "true" and r1["coast_km"] == "1.0" and r1["krs_6_8"] == "0.2"
    assert r1["krs_12_2"] == ""  # 계절 유효일 부족 → 빈 칸
    assert (
        r5["eto_mae_016"] == ""
        and r5["klass"] == "일사 없음·일조 있음"
        and r5["resolution_corrected"] == "true"
    )
    assert r6["klass"] == "수신 실패" and r6["coast_km"] == ""


def test_krs_groups_by_distance_band_and_island_with_known_medians():
    g = krs_groups(merge_geo(AUDIT, GEO))
    bands = {b["label"]: b for b in g["bands"]}
    assert bands["0~10 km"]["n"] == 2 and bands["0~10 km"]["median"] == pytest.approx(0.195)
    assert bands["10~30 km"]["n"] == 0 and bands["10~30 km"]["median"] is None
    assert bands["30~60 km"]["n"] == 0 or bands["30~60 km"]["median"] == pytest.approx(0.16)
    assert bands["60 km 이상"]["n"] == 2 and bands["60 km 이상"]["median"] == pytest.approx(0.155)
    assert g["island"]["n"] == 1 and g["island"]["median"] == pytest.approx(0.20)
    assert g["inland"]["n"] == 3 and g["overall"]["n"] == 4


def test_scatter_svg_is_valid_xml_with_circles_for_inland_and_diamonds_for_islands():
    svg = svg_scatter(merge_geo(AUDIT, GEO))
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    assert root.tag == ns + "svg"
    circles = [e for e in root.iter(ns + "circle") if e.get("class") == "inland"]
    diamonds = [e for e in root.iter(ns + "polygon") if e.get("class") == "island"]
    assert len(circles) == 3 and len(diamonds) == 1  # 정상 지점 4곳만 그린다(일사 없는 곳·실패 지점은 없음)
    text = "".join(root.itertext())
    assert "0.16" in text and "0.19" in text and "내륙" in text and "섬" in text
    assert svg == svg_scatter(merge_geo(AUDIT, GEO))  # 같은 입력 → 같은 그림


def test_markdown_has_all_planned_sections_and_proposes_the_best_criterion():
    st = merge_geo(AUDIT, GEO)
    cmp = compare_boundaries(st)
    md = markdown_report(st, cmp, META, "20261108")
    for head in ("분류", "일조시간", "실측 kRs", "경계 후보", "의심 일조", "해상도", "해상도 보정", "섬"):
        assert head in md
    assert cmp["best"]["label"] in md and "지점별 최적" in md
    assert "10 km 미만" in md and "Natural Earth" in md
    assert "194" in md  # 호출 수
    assert md.count("\n|") > 15  # 표가 있다


def test_markdown_warns_when_the_best_criterion_is_the_smallest_candidate():
    st = merge_geo(AUDIT, GEO)
    cmp = compare_boundaries(st)
    assert cmp["best"]["label"] == "거리 ≤ 10 km"
    assert "가장 작은 후보" in markdown_report(st, cmp, META, "20261108")


def test_markdown_says_when_criteria_make_no_meaningful_difference():
    aud = [_aud(str(i), f"S{i}", mae16=0.300, mae19=0.304) for i in range(1, 7)]
    geo = {
        str(i): dict(status="inside", coast_km=10.0 * i, island=False, corrected=False, polygon_dist_km=0)
        for i in range(1, 7)
    }
    st = merge_geo(aud, geo)
    md = markdown_report(st, compare_boundaries(st), META, "20261108")
    assert "구분이 무의미" in md and "전부 0.16" in md


# ── 수집기(보고서 만들기) ────────────────────────────────────────────────
def _sq(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def test_build_report_writes_csv_md_svg_with_the_date_and_consistent_numbers(tmp_path):
    audit = [
        _aud("1", "육지", krs=0.17),
        _aud("2", "섬", krs=0.21, mae16=0.5, mae19=0.2),
        _aud("3", "없음", klass="모두 없음"),
    ]
    audit[0].update(lat=36.0, lon=128.0)
    audit[1].update(lat=36.25, lon=131.25)
    audit[2].update(lat=36.2, lon=130.995)  # 땅 밖(해상도 보정)
    lines = [_sq(126, 34, 130, 39), _sq(131, 36, 131.5, 36.5)]
    polys = [[_sq(126, 34, 130, 39)], [_sq(131, 36, 131.5, 36.5)]]
    paths = RR.build_report(audit, META, lines, polys, tmp_path, "20261108")
    assert {p.name for p in paths.values()} == {
        "station_radiation_20261108.csv",
        "station_radiation_20261108.md",
        "radiation_kRs_vs_coast_20261108.svg",
    }
    rows = list(csv.DictReader(io.StringIO(paths["csv"].read_text(encoding="utf-8-sig"))))
    by = {r["stn_id"]: r for r in rows}
    assert by["2"]["island"] == "true" and by["1"]["island"] == "false"
    assert by["3"]["resolution_corrected"] == "true" and by["3"]["coast_km"] == "0.0"
    assert float(by["1"]["coast_km"]) > 100
    assert "station_radiation_20261108.csv" in paths["md"].read_text(encoding="utf-8")


def test_main_stops_loudly_when_the_audit_result_is_missing(tmp_path):
    with pytest.raises(SystemExit):
        RR.main(["--audit-dir", str(tmp_path / "none"), "--coast-dir", str(tmp_path), "--out", str(tmp_path)])


def test_report_collector_reads_no_api_key_and_has_no_database_write_path():
    import inspect

    src = inspect.getsource(RR)
    for bad in ("SupabaseWriter", "supabase_write", "upsert", "DATA_GO_KR", "SERVICE_ROLE"):
        assert bad not in src


def test_audit_files_roundtrip_through_json(tmp_path):
    """점검 수집기가 쓴 stations.json 모양 그대로 읽을 수 있다"""
    d = tmp_path / "radiation_audit"
    d.mkdir()
    (d / "stations.json").write_text(json.dumps(AUDIT, ensure_ascii=False), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    stations, meta = RR.load_audit(d)
    assert len(stations) == 6 and meta["calls"] == 194


# ── 해안 목록(coastal_stations.csv) 만들기 ─────────────────────────────────
def test_coastal_csv_applies_the_approved_boundary_with_ten_km_inclusive():
    aud = [_aud(str(i), f"S{i}") for i in range(1, 6)]
    km = {"1": 0.0, "2": 9.99, "3": 10.0, "4": 10.01, "5": 80.0}
    geo = {
        k: dict(status="inside", coast_km=v, island=k == "2", corrected=k == "1", polygon_dist_km=0.0)
        for k, v in km.items()
    }
    text = coastal_csv(merge_geo(aud + [dict(stn_id="9", name="실패", error="ASOS 실패")], geo), "20261107")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0].keys()) == list(COASTAL_CSV_COLUMNS)
    assert [(r["stn_id"], r["krs"]) for r in rows] == [
        ("1", "0.19"),
        ("2", "0.19"),
        ("3", "0.19"),
        ("4", "0.16"),
        ("5", "0.16"),
    ]
    assert (
        rows[0]["resolution_corrected"] == "true"
        and rows[1]["island"] == "true"
        and rows[0]["basis"] == "20261107"
    )
    assert all(
        r["stn_id"] != "9" for r in rows
    )  # 수신 실패 지점은 목록에 넣지 않는다(목록에 없으면 내륙 0.16)


def test_build_report_can_also_write_the_coastal_list(tmp_path):
    audit = [_aud("1", "육지", krs=0.17), _aud("2", "섬", krs=0.21)]
    audit[0].update(lat=36.0, lon=128.0)
    audit[1].update(lat=36.25, lon=131.02)  # 섬 서쪽 해안 가까이(약 2 km)
    lines = [_sq(126, 34, 130, 39), _sq(131, 36, 131.5, 36.5)]
    polys = [[_sq(126, 34, 130, 39)], [_sq(131, 36, 131.5, 36.5)]]
    target = tmp_path / "coastal_stations.csv"
    paths = RR.build_report(audit, META, lines, polys, tmp_path, "20261108", coastal_csv_path=target)
    assert paths["coastal"] == target
    rows = {r["stn_id"]: r for r in csv.DictReader(io.StringIO(target.read_text(encoding="utf-8-sig")))}
    assert rows["1"]["krs"] == "0.16" and rows["2"]["krs"] == "0.19"
