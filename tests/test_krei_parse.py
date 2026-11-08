"""KREI 농업관측 수집기 2단계 파싱·검사 시험 (core/krei_parse.py, core/krei_check.py).

고정 표본(tests/fixtures/krei/):
- #8778 (2026-11 과일)
- #8779 (2026-11 과채)
- #8743 (2026-09 과일)
"""

import copy
import datetime as dt
import gzip
import json
from pathlib import Path

import pytest

from croplib_dp.collectors.krei import KreiResult, parse_one_report
from croplib_dp.core.krei import parse_report_items
from croplib_dp.core.krei_check import (
    check_yoy_recalc,
    interval_for_num,
)
from croplib_dp.core.krei_parse import (
    parse_report_pages,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "krei"
ITEMS_XML = (FIXTURES_DIR / "reportItem.xml").read_bytes()
ITEMS = parse_report_items(ITEMS_XML)


@pytest.fixture(scope="module")
def pages_8778() -> list[dict]:
    p = FIXTURES_DIR / "8778.pages.json.gz"
    return json.load(gzip.open(p, "rt", encoding="utf-8"))


@pytest.fixture(scope="module")
def pages_8779() -> list[dict]:
    p = FIXTURES_DIR / "8779.pages.json.gz"
    return json.load(gzip.open(p, "rt", encoding="utf-8"))


@pytest.fixture(scope="module")
def pages_8743() -> list[dict]:
    p = FIXTURES_DIR / "8743.pages.json.gz"
    return json.load(gzip.open(p, "rt", encoding="utf-8"))


def test_yoy_recalc_rounding_bounds():
    """증감률 반올림: 4.7 / 7.6 -> 표기 -38.0 통과 (계산 -38.16이어도 반올림 구간 안)."""
    ok, msg = check_yoy_recalc("4.7", "7.6", "-38.0")
    assert ok, f"4.7/7.6 -> -38.0 통과 실패: {msg}"

    # 반올림 구간 벗어난 경우 실패 확인
    bad_ok, _ = check_yoy_recalc("4.7", "7.6", "-30.0")
    assert not bad_ok


def test_yoy_recalc_integer_inputs_and_unicode_minus():
    """표기 자릿수 기준 불확실성 구간 계산:
    - 정수(12, 15): ±0.5 구간([11.5, 12.5], [14.5, 15.5])
    - 소수 1자리(4.7): ±0.05 구간([4.65, 4.75])
    - 소수 2자리(1.25): ±0.005 구간([1.245, 1.255])
    정수 12 / 15 -> 표기 -20.0 및 유니코드 마이너스(−20.0) 통과 확인.
    """
    assert interval_for_num("12") == (12.0, 11.5, 12.5)
    assert interval_for_num("4.7") == (4.7, 4.65, 4.75)
    assert interval_for_num("1.25") == (1.25, 1.245, 1.255)

    # 1. 12 / 15, 표기 -20.0 (ASCII 마이너스)
    ok, msg = check_yoy_recalc("12", "15", "-20.0")
    assert ok, f"12/15 -> -20.0 통과 실패: {msg}"

    # 2. 12 / 15, 표기 −20.0 (유니코드 마이너스 U+2212)
    ok_uni, msg_uni = check_yoy_recalc("12", "15", "−20.0")
    assert ok_uni, f"12/15 -> −20.0 (유니코드) 통과 실패: {msg_uni}"

    # 3. int 타입 입력 (12, 15, -20.0)
    ok_num, msg_num = check_yoy_recalc(12, 15, -20.0)
    assert ok_num, f"12, 15 -> -20.0 (숫자형) 통과 실패: {msg_num}"

    # 4. 정수 구간([-25.8%, -13.8%])을 벗어난 표기(-30.0)는 실패
    bad_ok, _ = check_yoy_recalc("12", "15", "-30.0")
    assert not bad_ok


def test_persimmon_shipment_rounding_interval(pages_8743):
    """#8743 단감 출하량 전망(1.9 / 2.1, 표기 -12.3) -> 통과 (반올림 구간 안)."""
    ok, msg = check_yoy_recalc("1.9", "2.1", "-12.3")
    assert ok, f"단감 1.9/2.1 -> -12.3 통과 실패: {msg}"

    parsed = parse_report_pages(
        file_id=8743,
        yyyy=2026,
        mm=9,
        ovr_item_code="OVR0000000013",
        pages=pages_8743,
        items=ITEMS,
        text_status="ok",
    )
    persimmon_tables = [t for t in parsed.raw_tables if t.title == "단감 출하량 전망"]
    assert len(persimmon_tables) == 1
    t = persimmon_tables[0]
    assert t.check_status == "pass"

    obs = [
        o
        for o in parsed.observations
        if o.item_label == "단감" and o.metric == "shipment" and o.period_label == "9월"
    ]
    cur = next(o for o in obs if o.basis == "current")
    prev = next(o for o in obs if o.basis == "prev")
    yoy = next(o for o in obs if o.basis == "yoy_pct")

    assert cur.value == 1.9
    assert prev.value == 2.1
    assert yoy.value == -12.3


def test_8778_apple_production_outlook(pages_8778):
    """#8778 사과 생산량 전망 검사 통과 및 정형 값 일치 확인."""
    parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=pages_8778,
        items=ITEMS,
        text_status="ok",
    )
    t = next(t for t in parsed.raw_tables if "사과 생산량 전망" in t.title)
    assert t.check_status == "pass"
    assert t.check_log is None

    # 면적, 단수, 생산량
    area_obs = {
        o.basis: o.value for o in parsed.observations if o.item_label == "사과" and o.metric == "area"
    }
    yield_obs = {
        o.basis: o.value for o in parsed.observations if o.item_label == "사과" and o.metric == "yield"
    }
    prod_obs = {
        o.basis: o.value for o in parsed.observations if o.item_label == "사과" and o.metric == "production"
    }

    assert area_obs["current"] == 34901.0
    assert yield_obs["current"] == 1422.0
    assert prod_obs["current"] == 496.1

    assert area_obs["prev"] == 33212.0
    assert yield_obs["prev"] == 1349.0
    assert prod_obs["prev"] == 448.0

    assert area_obs["normal"] == 33820.0
    assert yield_obs["normal"] == 1410.0
    assert prod_obs["normal"] == 474.6

    assert area_obs["yoy_pct"] == 5.1
    assert yield_obs["yoy_pct"] == 5.4
    assert prod_obs["yoy_pct"] == 10.8

    assert area_obs["vs_normal_pct"] == 3.2
    assert yield_obs["vs_normal_pct"] == 0.8
    assert prod_obs["vs_normal_pct"] == 4.5


def test_8778_apple_shipment_outlook(pages_8778):
    """#8778 사과 출하량 전망 통과 및 11월/12월 이후 기간 파싱 확인."""
    parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=pages_8778,
        items=ITEMS,
        text_status="ok",
    )
    t = next(t for t in parsed.raw_tables if t.title == "사과 출하량 전망")
    assert t.check_status == "pass"

    ship_obs = [o for o in parsed.observations if o.item_label == "사과" and o.metric == "shipment"]
    # 11월
    m11_cur = next(o for o in ship_obs if o.period_label == "11월" and o.basis == "current")
    m11_prev = next(o for o in ship_obs if o.period_label == "11월" and o.basis == "prev")
    m11_norm = next(o for o in ship_obs if o.period_label == "11월" and o.basis == "normal")
    m11_yoy = next(o for o in ship_obs if o.period_label == "11월" and o.basis == "yoy_pct")
    m11_vsn = next(o for o in ship_obs if o.period_label == "11월" and o.basis == "vs_normal_pct")

    assert m11_cur.value == 67.0
    assert m11_prev.value == 58.9
    assert m11_norm.value == 61.1
    assert m11_yoy.value == 13.8
    assert m11_vsn.value == 9.8
    assert m11_cur.period_start == dt.date(2026, 11, 1)
    assert m11_cur.period_end == dt.date(2026, 11, 30)

    # 12월 이후
    m12_cur = next(o for o in ship_obs if o.period_label == "12월 이후" and o.basis == "current")
    m12_prev = next(o for o in ship_obs if o.period_label == "12월 이후" and o.basis == "prev")
    m12_norm = next(o for o in ship_obs if o.period_label == "12월 이후" and o.basis == "normal")
    m12_yoy = next(o for o in ship_obs if o.period_label == "12월 이후" and o.basis == "yoy_pct")
    m12_vsn = next(o for o in ship_obs if o.period_label == "12월 이후" and o.basis == "vs_normal_pct")

    assert m12_cur.value == 251.0
    assert m12_prev.value == 241.0
    assert m12_norm.value == 246.4
    assert m12_yoy.value == 4.2
    assert m12_vsn.value == 1.9
    assert m12_cur.period_start == dt.date(2026, 12, 1)
    assert m12_cur.period_end is None


def test_8778_pear_production_outlook(pages_8778):
    """#8778 배 생산량 전망 통과 확인."""
    parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=pages_8778,
        items=ITEMS,
        text_status="ok",
    )
    t = next(t for t in parsed.raw_tables if "배 생산량 전망" in t.title)
    assert t.check_status == "pass"

    area_cur = next(
        o for o in parsed.observations if o.item_label == "배" and o.metric == "area" and o.basis == "current"
    )
    yield_cur = next(
        o
        for o in parsed.observations
        if o.item_label == "배" and o.metric == "yield" and o.basis == "current"
    )
    prod_cur = next(
        o
        for o in parsed.observations
        if o.item_label == "배" and o.metric == "production" and o.basis == "current"
    )

    assert area_cur.value == 9261.0
    assert yield_cur.value == 1974.0
    assert prod_cur.value == 182.8

    area_yoy = next(
        o for o in parsed.observations if o.item_label == "배" and o.metric == "area" and o.basis == "yoy_pct"
    )
    yield_yoy = next(
        o
        for o in parsed.observations
        if o.item_label == "배" and o.metric == "yield" and o.basis == "yoy_pct"
    )
    prod_yoy = next(
        o
        for o in parsed.observations
        if o.item_label == "배" and o.metric == "production" and o.basis == "yoy_pct"
    )

    assert area_yoy.value == -0.6
    assert yield_yoy.value == -6.9
    assert prod_yoy.value == -7.4


def test_8778_apple_growth_survey(pages_8778):
    """#8778 사과 생육상황 분포 합계 100±0.3 검사 및 값 확인."""
    parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=pages_8778,
        items=ITEMS,
        text_status="ok",
    )
    t = next(t for t in parsed.raw_tables if t.title == "사과 생육상황")
    assert t.check_status == "pass"

    dists = {d.aspect: (d.good, d.same, d.bad) for d in parsed.survey_dists if d.item_label == "사과"}
    assert dists["전반적인 생육상황"] == (58.3, 31.4, 10.2)
    assert dists["열매 성장(과 비대) 정도"] == (27.5, 36.2, 36.3)
    assert dists["착색 정도"] == (60.1, 26.5, 13.4)


def test_8778_apple_monthly_price(pages_8778):
    """#8778 사과 월별 도매가격: 머리글 첫 달 7월 -> 2026년산 1월=2027-01, 품종 연결."""
    parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=pages_8778,
        items=ITEMS,
        text_status="ok",
    )
    t = next(t for t in parsed.raw_tables if t.title == "사과 월별 도매가격(상품)")
    assert t.check_status == "pass"

    apple_prices = [
        o for o in parsed.observations if o.item_label == "사과" and o.metric == "wholesale_price"
    ]
    # 2025년산 1월 관측치: 2025년산 기준 1월 = 2026-01
    p2025_jan = next(o for o in apple_prices if o.basis == "prev" and o.period_label == "1월")
    assert p2025_jan.period_start == dt.date(2026, 1, 1)
    assert p2025_jan.period_end == dt.date(2026, 1, 31)
    assert p2025_jan.variety == "후지"
    assert p2025_jan.value == 78273.0

    # 2026년산 7월(쓰가루), 8월(쓰가루, 홍로), 9월(홍로), 10월(후지)
    p2026 = [o for o in apple_prices if o.basis == "current"]
    assert len(p2026) == 5
    assert p2026[0].period_label == "7월" and p2026[0].variety == "쓰가루" and p2026[0].value == 48381.0
    assert p2026[1].period_label == "8월" and p2026[1].variety == "쓰가루" and p2026[1].value == 45543.0
    assert p2026[2].period_label == "8월" and p2026[2].variety == "홍로" and p2026[2].value == 71389.0
    assert p2026[3].period_label == "9월" and p2026[3].variety == "홍로" and p2026[3].value == 50060.0
    assert p2026[4].period_label == "10월" and p2026[4].variety == "후지" and p2026[4].value == 30024.0


def test_8778_headlines(pages_8778):
    """#8778 헤드라인 및 방향 추출 확인."""
    parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=pages_8778,
        items=ITEMS,
        text_status="ok",
    )
    s_apple_ship = next(s for s in parsed.section_texts if s.section == "출하전망" and s.item_label == "사과")
    assert s_apple_ship.headline == "11월 사과 출하량 전년 대비 증가"
    assert s_apple_ship.direction == "증가"
    assert len(s_apple_ship.bullets) >= 2

    s_pear_prod = next(s for s in parsed.section_texts if s.section == "생산전망" and s.item_label == "배")
    assert s_pear_prod.headline == "2026년 배 생산량 전년 대비 감소"
    assert s_pear_prod.direction == "감소"


def test_corrupted_production_fails(pages_8778):
    """2026년 생산량을 496.1 -> 520.0으로 바꾼 가짜 입력
    -> 증감률·면적×단수 모두 fail, observation 0줄, needs_review.
    """
    corrupted_pages = copy.deepcopy(pages_8778)
    for w in corrupted_pages[2]["words"]:
        if w[4] == "496.1":
            w[4] = "520.0"

    parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=corrupted_pages,
        items=ITEMS,
        text_status="ok",
    )
    assert parsed.parse_status == "needs_review"

    t = next(t for t in parsed.raw_tables if "사과 생산량 전망" in t.title)
    assert t.check_status == "fail"
    assert t.check_log is not None
    checks_failed = {err["check"] for err in t.check_log}
    assert "area_yield_prod" in checks_failed
    assert "yoy_recalc" in checks_failed

    apple_prod_obs = [o for o in parsed.observations if o.item_label == "사과" and o.metric == "production"]
    assert len(apple_prod_obs) == 0


def test_graphs_excluded(pages_8778):
    """그래프 제목(사과 도매가격(상품), 반입량)은 table_raw에 들어가지 않는다."""
    parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=pages_8778,
        items=ITEMS,
        text_status="ok",
    )
    for t in parsed.raw_tables:
        assert t.table_type != "graph"
        assert "반입량" not in t.title
        if "도매가격(상품)" in t.title:
            assert "월별" in t.title


def test_normal_and_vs_normal_distinction(pages_8778):
    """'평년' 줄과 '평년 대비' 줄이 섞이지 않는다."""
    parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=pages_8778,
        items=ITEMS,
        text_status="ok",
    )
    t = next(t for t in parsed.raw_tables if "사과 생산량 전망" in t.title)
    normal_rows = [r for r in t.rows if r[0] == "평년"]
    vs_normal_rows = [r for r in t.rows if r[0] == "평년 대비"]

    assert len(normal_rows) == 1
    assert len(vs_normal_rows) == 1
    assert normal_rows[0][3] == "474.6"
    assert vs_normal_rows[0][3] == "4.5"


def test_grain_prep_only(pages_8778):
    """쌀·콩·감자 등 준비 대상은 원형 표와 헤드라인만 추출되고 observation/survey_dist는 없음."""
    parsed = parse_report_pages(
        file_id=9999,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000070",  # 쌀
        pages=pages_8778,
        items=ITEMS,
        text_status="ok",
    )
    assert parsed.parse_status == "parsed"
    assert len(parsed.raw_tables) > 0
    assert len(parsed.section_texts) > 0
    assert len(parsed.observations) == 0
    assert len(parsed.survey_dists) == 0


def test_excluded_reports():
    """no_text 건이나 축산 등 제외 부류는 skipped."""
    parsed_no_text = parse_report_pages(
        file_id=8687,
        yyyy=2026,
        mm=5,
        ovr_item_code="OVR0000000008",
        pages=[],
        items=ITEMS,
        text_status="no_text",
    )
    assert parsed_no_text.parse_status == "skipped"

    parsed_livestock = parse_report_pages(
        file_id=8800,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000050",  # 축산
        pages=[],
        items=ITEMS,
        text_status="ok",
    )
    assert parsed_livestock.parse_status == "skipped"


class DummyWriter:
    def __init__(self):
        self.deleted = []
        self.upserted = {}
        self.next_id = 100

    def delete(self, table: str, query: str, select: str = "id", schema: str | None = None):
        self.deleted.append((table, query))
        return []

    def upsert(
        self, table: str, rows: list, on_conflict: tuple, returning: bool = False, schema: str | None = None
    ):
        saved = []
        for r in rows:
            r_copy = dict(r)
            if "id" not in r_copy or r_copy["id"] is None:
                r_copy["id"] = self.next_id
                self.next_id += 1
            saved.append(r_copy)
        self.upserted[table] = self.upserted.get(table, []) + saved
        return saved


def test_parse_one_report_idempotency(pages_8778):
    """같은 발행호를 두 번 파싱해도 통째로 바꾸기(연쇄 삭제 후 재삽입)로 줄 수가 같다."""
    writer = DummyWriter()
    rep = {
        "file_id": 8778,
        "yyyy": 2026,
        "mm": 11,
        "ovr_item_code": "OVR0000000013",
        "item_name": "과일",
        "report_type": "월보",
        "text_status": "ok",
    }
    gz_bytes = gzip.compress(json.dumps(pages_8778).encode("utf-8"))

    # 1회 실행
    r1 = KreiResult()
    p1 = parse_one_report(r1, rep, ITEMS, gz_bytes, writer=writer, write=True)
    assert p1.parse_status == "parsed"
    tables_count_1 = len(writer.upserted.get("table_raw", []))
    obs_count_1 = len(writer.upserted.get("observation", []))

    # 2회 실행 (재실행)
    writer.upserted.clear()
    r2 = KreiResult()
    p2 = parse_one_report(r2, rep, ITEMS, gz_bytes, writer=writer, write=True)
    assert p2.parse_status == "parsed"
    tables_count_2 = len(writer.upserted.get("table_raw", []))
    obs_count_2 = len(writer.upserted.get("observation", []))

    assert tables_count_1 == tables_count_2
    assert obs_count_1 == obs_count_2


def test_stored_fruit_shipment_outlook_prior_crop_year():
    """봄/초여름 월보에서 저장 사과·배 출하량 전망(2025년산/2024년산) 및 다단 머리글 기간 라벨 파싱 확인."""
    from croplib_dp.core.krei_parse import RawTable, parse_shipment_outlook

    table = RawTable(
        page=3,
        seq=1,
        item_code="OVR0000000005",
        item_label="사과",
        title="사과 출하량 전망",
        unit="천 톤, %",
        header=[
            [
                {"text": "구분"},
                {"text": "7월~익년 3월"},
                {"text": "4월"},
                {"text": "5월"},
            ]
        ],
        rows=[
            ["2025년산", "368.8", "41.4", "26.0"],
            ["2024년산", "378.4", "39.5", "25.3"],
            ["평년", "388.1", "34.8", "23.8"],
            ["전년 대비", "-2.6", "4.8", "3.0"],
            ["평년 대비", "-5.0", "19.1", "9.5"],
        ],
        note=None,
        source=None,
        table_type="shipment_outlook",
    )

    obs, errors = parse_shipment_outlook(table, file_id=8677, yyyy=2026, mm=4)
    assert errors == []
    assert len(obs) > 0

    # 당해년산(2025)과 전년산(2024)이 올바른 crop_year로 기록되는지 확인
    cur_obs = [o for o in obs if o.basis == "current"]
    prev_obs = [o for o in obs if o.basis == "prev"]
    assert all(o.crop_year == 2025 for o in cur_obs)
    assert all(o.crop_year == 2024 for o in prev_obs)

    # 기간 날짜 파싱 확인: 7월~익년 3월은 4월호 기준 전년도 7월(2025-07) ~ 당해년도 3월(2026-03)
    obs_prev_period = next(o for o in obs if o.period_label == "7월~익년 3월" and o.basis == "current")
    assert obs_prev_period.period_start == dt.date(2025, 7, 1)
    assert obs_prev_period.period_end == dt.date(2026, 3, 31)

    # 4월, 5월
    obs_m4 = next(o for o in obs if o.period_label == "4월" and o.basis == "current")
    assert obs_m4.period_start == dt.date(2026, 4, 1)
    assert obs_m4.period_end == dt.date(2026, 4, 30)


def test_multilevel_header_shipment_outlook():
    """2단 머리글(9월, 추석 성수기, 10월)에서 빈 셀이 상위 행 텍스트로 합성되어
    기간 라벨이 올바르게 보존되는지 확인."""
    from croplib_dp.core.krei_parse import RawTable, parse_shipment_outlook

    table = RawTable(
        page=3,
        seq=4,
        item_code="OVR0000000005",
        item_label="사과",
        title="사과 출하량 전망",
        unit="천 톤, %",
        header=[
            [{"text": "구분"}, {"text": "9월"}, {"text": ""}, {"text": "10월"}],
            [{"text": ""}, {"text": ""}, {"text": "추석 성수기"}, {"text": ""}],
        ],
        rows=[
            ["2026년", "71.5", "47.7", "39.2"],
            ["2025년", "68.6", "40.0", "37.8"],
            ["평년", "63.5", "56.2", "50.6"],
            ["전년 대비", "4.3", "19.3", "3.7"],
            ["평년 대비", "12.7", "-15.1", "-22.5"],
        ],
        note=None,
        source=None,
        table_type="shipment_outlook",
    )

    obs, errors = parse_shipment_outlook(table, file_id=8743, yyyy=2026, mm=9)
    assert errors == []
    labels = sorted({o.period_label for o in obs})
    assert labels == ["10월", "9월", "추석 성수기"]


def test_parse_period_dates_crop_year():
    """'N월 ~ 익년 M월'의 시작 연도를 발행월이 아니라 그 행의 산(crop_year) 기준으로 계산.
    - 2026-11 발행, 2026년산, '12월 ~ 익년 3월' -> 2026-12-01 ~ 2027-03-31
    - 2026-04 발행, 2025년산, '7월~익년 3월' -> 2025-07-01 ~ 2026-03-31
    """
    from croplib_dp.core.krei_parse import parse_period_dates

    # 1. 2026-11 발행, 2026년산, 12월 ~ 익년 3월
    s1, e1 = parse_period_dates("12월 ~ 익년 3월", issue_y=2026, issue_m=11, crop_year=2026)
    assert s1 == dt.date(2026, 12, 1)
    assert e1 == dt.date(2027, 3, 31)

    # 2. 2026-04 발행, 2025년산, 7월~익년 3월
    s2, e2 = parse_period_dates("7월~익년 3월", issue_y=2026, issue_m=4, crop_year=2025)
    assert s2 == dt.date(2025, 7, 1)
    assert e2 == dt.date(2026, 3, 31)


def test_manual_approval_overrides_check_and_loads_obs(pages_8778):
    """원문 자체가 안 맞는 표를 승인 목록에 등록하면 check_status='pass'로 보고 숫자를 적재함."""
    from croplib_dp.core.krei_parse import find_table_approval, load_approved_tables

    # 1. 8723 기본 승인 목록 로드 확인
    approved = load_approved_tables()
    assert any(a.get("file_id") == 8723 or str(a.get("file_id")) == "8723" for a in approved)
    app_8723 = find_table_approval(8723, "2026년 복숭아 생산량 전망", approved)
    assert app_8723 is not None
    assert "2025년 값이 고쳐진 통계" in app_8723.get("reason", "")

    # 2. 8778 의도적 오류 주입 표본을 custom approval로 승인하여 관측치 적재 및 parsed 확인
    corrupted_pages = copy.deepcopy(pages_8778)
    for w in corrupted_pages[2]["words"]:
        if w[4] == "496.1":
            w[4] = "520.0"

    # 승인 목록 없음 -> needs_review, observation 미적재
    unapproved_parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=corrupted_pages,
        items=ITEMS,
        text_status="ok",
        approved_tables=[],
    )
    assert unapproved_parsed.parse_status == "needs_review"
    unapp_t = next(t for t in unapproved_parsed.raw_tables if "사과 생산량 전망" in t.title)
    assert unapp_t.check_status == "fail"
    assert not any(
        o.item_label == "사과" and o.metric == "production" for o in unapproved_parsed.observations
    )

    # 승인 목록 있음 -> pass, observation 적재, parsed
    custom_approvals = [
        {
            "file_id": 8778,
            "title": "2026년 사과 생산량 전망",
            "reason": "테스트 수동 승인: 오차 허용",
            "approved_at": "2026-11-09",
        }
    ]
    approved_parsed = parse_report_pages(
        file_id=8778,
        yyyy=2026,
        mm=11,
        ovr_item_code="OVR0000000013",
        pages=corrupted_pages,
        items=ITEMS,
        text_status="ok",
        approved_tables=custom_approvals,
    )
    assert approved_parsed.parse_status == "parsed"
    app_t = next(t for t in approved_parsed.raw_tables if "사과 생산량 전망" in t.title)
    assert app_t.check_status == "pass"
    assert app_t.check_log is not None
    assert "수동 승인: 테스트 수동 승인: 오차 허용" in app_t.check_log[0]["message"]
    # 관측치가 정상 적재되었는지 확인
    prod_obs = [
        o for o in approved_parsed.observations if o.item_label == "사과" and o.metric == "production"
    ]
    assert len(prod_obs) > 0
