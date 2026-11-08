"""KREI 농업관측 수집기 1단계 시험 (core/krei.py, io/krei.py, collectors/krei.py, core/monitor.py).

네트워크 없이 실제 응답 fixture(tests/fixtures/krei/)와 작은 시험용 PDF·가짜 Supabase로 검증한다.
"""

import datetime as dt
import gzip
import json
import urllib.parse
from pathlib import Path

import pandas as pd
import pytest

from croplib_dp.collectors import krei as K
from croplib_dp.collectors import monitor as M
from croplib_dp.core.krei import (
    UNREADABLE_ISSUE_TITLE,
    build_pages_payload,
    compress_pages_json,
    compute_page_text_stats,
    compute_text_stats,
    default_months,
    extract_file_id,
    extract_first_page_meta,
    months_between,
    needs_refetch,
    next_year_month,
    parse_kst_timestamp,
    parse_report_items,
    parse_reports,
    sync_items,
)
from croplib_dp.core.monitor import check_health, report
from croplib_dp.io.contract import SPECS, check_rows
from croplib_dp.io.github_issue import IssueAlerts
from croplib_dp.io.krei import USER_AGENT, extract_pdf_bytes
from croplib_dp.io.supabase_write import SupabaseWriter

FIXTURES = Path(__file__).parent / "fixtures" / "krei"
REPORT_ITEM_XML = (FIXTURES / "reportItem.xml").read_bytes()
MONTHLY_11_XML = (FIXTURES / "montly_2026-11.xml").read_bytes()
NEWS_11_XML = (FIXTURES / "news_2026-11.xml").read_bytes()
TS = pd.Timestamp


def make_test_pdf(words_on_page: list[str] | None = None) -> bytes:
    """외부 글꼴 없이 pdfplumber 가 쪽 크기·단어 좌표·한국어 글자를 뽑을 수 있는 시험용 PDF 생성."""
    if words_on_page is None:
        words_on_page = [
            "통권",
            "제312호",
            "2026년",
            "11월",
            "3일",
            "발행",
            "과일",
            "농업관측센터월보사과배단감감귤포도복숭아출하량전년대비증가전망생육양호" * 6,
        ]
    charset: list[str] = []
    for w in words_on_page:
        for ch in w:
            if ch not in charset:
                charset.append(ch)
    if not charset:
        charset = [" "]
    code_of = {ch: i + 33 for i, ch in enumerate(charset)}
    bfchar = [f"<{code:02X}> <{ch.encode('utf-16-be').hex().upper()}>" for ch, code in code_of.items()]
    cmap = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Custom def\n"
        "/CMapType 2 def\n"
        "1 begincodespacerange\n"
        "<00> <FF>\n"
        "endcodespacerange\n"
        f"{len(bfchar)} beginbfchar\n" + "\n".join(bfchar) + "\nendbfchar\n"
        "endcmap\n"
        "CMapName currentdict /CMap defineresource pop\n"
        "end\n"
        "end\n"
    ).encode("ascii")

    # 각 단어를 서로 떨어진 x 좌표에 배치해 pdfplumber extract_words() 가 단어별로 묶게 한다
    ops = ["BT", "/F1 12 Tf"]
    x = 50
    for w in words_on_page:
        hx = "".join(f"{code_of[ch]:02X}" for ch in w)
        ops.append(f"1 0 0 1 {x} 750 Tm <{hx}> Tj")
        x += 60
    ops.append("ET")
    content = "\n".join(ops).encode("ascii")

    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        (
            b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/FirstChar 32 /LastChar 255 /Widths ["
            + b" ".join([b"600"] * 224)
            + b"] /ToUnicode 6 0 R >>\nendobj\n"
        ),
        f"5 0 obj\n<< /Length {len(content)} >>\nstream\n".encode("ascii")
        + content
        + b"\nendstream\nendobj\n",
        f"6 0 obj\n<< /Length {len(cmap)} >>\nstream\n".encode("ascii") + cmap + b"\nendstream\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for o in objs:
        offsets.append(len(out))
        out.extend(o)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


class FakeClock:
    def __init__(self, start: str = "2026-11-07 20:40:00"):
        self.t = TS(start)
        self.sleeps: list[float] = []

    def now(self) -> pd.Timestamp:
        return self.t

    def sleep(self, sec: float) -> None:
        self.sleeps.append(sec)
        self.t = self.t + pd.Timedelta(seconds=sec)


class FakeKreiHttp:
    def __init__(self, pdf_bytes: bytes | None = None, fail_once_urls: set[str] | None = None):
        self.pdf_bytes = pdf_bytes if pdf_bytes is not None else make_test_pdf()
        self.fail_once: set[str] = set(fail_once_urls or ())
        self.calls: list[tuple[str, int, dict[str, str]]] = []
        self.monthly_xml: dict[str, bytes] = {
            "2026-11": MONTHLY_11_XML,
            "2026-12": b'<?xml version="1.0" encoding="UTF-8"?><channel></channel>',
        }
        self.news_xml: dict[str, bytes] = {
            "2026-11": NEWS_11_XML,
            "2026-12": b'<?xml version="1.0" encoding="UTF-8"?><channel></channel>',
        }
        self.item_xml: bytes = REPORT_ITEM_XML

    def __call__(
        self, url: str, timeout: int, headers: dict[str, str]
    ) -> tuple[int | None, bytes, str | None]:
        self.calls.append((url, timeout, headers))
        if url in self.fail_once:
            self.fail_once.remove(url)
            return 503, b"Service Unavailable", "HTTP 503"
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("/reportItem"):
            return 200, self.item_xml, None
        if u.path.endswith("/montlyReport"):
            ym = f"{q.get('yyyy')}-{q.get('mm')}"
            body = self.monthly_xml.get(ym, b'<?xml version="1.0" encoding="UTF-8"?><channel></channel>')
            return 200, body, None
        if u.path.endswith("/newsReport"):
            ym = f"{q.get('yyyy')}-{q.get('mm')}"
            body = self.news_xml.get(ym, b'<?xml version="1.0" encoding="UTF-8"?><channel></channel>')
            return 200, body, None
        if "/download/" in u.path:
            return 200, self.pdf_bytes, None
        return 404, b"Not Found", "HTTP 404"


# ── 1. XML 파싱 (실제 응답 fixture) ─────────────────────────────────────────
def test_parse_report_items_fixture_43_items_3_levels():
    items = parse_report_items(REPORT_ITEM_XML)
    assert len(items) == 43
    levels = {it["level"] for it in items}
    assert levels == {1, 2, 3}
    by_code = {it["ovr_item_code"]: it for it in items}
    # 최상위(level 1): 원예·축산·곡물, prt_ovr_item_code 는 None
    assert by_code["OVR0000000093"]["item_name"] == "원예"
    assert by_code["OVR0000000093"]["prt_ovr_item_code"] is None
    assert by_code["OVR0000000093"]["level"] == 1
    # 앞자리 0 이 있는 문자열 코드 유지 ('08', '05')
    assert by_code["OVR0000000008"]["krei_item_code"] == "08"
    assert by_code["OVR0000000005"]["krei_item_code"] == "05"
    assert by_code["OVR0000000026"]["prt_krei_item_code"] == "08"
    # 문서와 다른 실제 API 값 확인: 수입과일 OVR0000000019, 해외축산 OVR0000000094
    assert by_code["OVR0000000019"]["item_name"] == "수입과일"
    assert by_code["OVR0000000094"]["item_name"] == "해외축산"


def test_parse_monthly_fixture_12_items_and_layers_two_distinct_rows():
    reports, problems = parse_reports(MONTHLY_11_XML, "monthly")
    assert problems == []
    assert len(reports) == 12
    # 산란계(OVR0000000018) 2건: 순보(8776)와 월보(8784)가 각각 다른 file_id 행이 된다
    layers = [r for r in reports if r["ovr_item_code"] == "OVR0000000018"]
    assert len(layers) == 2
    by_type = {r["report_type"]: r for r in layers}
    assert set(by_type) == {"순보", "월보"}
    assert by_type["순보"]["file_id"] == 8776
    assert by_type["월보"]["file_id"] == 8784
    # 해외축산 분기보도 포함
    overseas = next(r for r in reports if r["ovr_item_code"] == "OVR0000000094")
    assert overseas["report_type"] == "분기보" and overseas["file_id"] == 8785
    # reg_date 가 KST timestamptz 로 해석됨
    fruit = next(r for r in reports if r["file_id"] == 8778)
    assert fruit["item_name"] == "과일" and fruit["reg_at"] == "2026-11-03T15:10:04+09:00"
    assert fruit["mod_at"] is None


def test_parse_news_fixture_empty_is_not_an_error():
    reports, problems = parse_reports(NEWS_11_XML, "news")
    assert reports == [] and problems == []


# ── 2. 파일번호 추출 및 부분 실패 처리 ───────────────────────────────────────
def test_extract_file_id_valid_and_invalid():
    url = "https://aglook.krei.re.kr/main/uObserveMonth/OVR0000000013/download/8778/ORGTEXT_FILE"
    assert extract_file_id(url) == 8778
    assert extract_file_id("/main/uObserveMonth/OVR0000000031/download/8779/ORGTEXT_FILE") == 8779
    assert extract_file_id("https://aglook.krei.re.kr/main/uObserveMonth/OVR0000000013/view") is None
    assert extract_file_id("") is None
    assert extract_file_id(None) is None


def test_parse_reports_skips_only_malformed_pdf_link_without_aborting():
    xml_utf8 = """<?xml version="1.0" encoding="UTF-8"?><channel>
      <item>
        <yyyy>2026</yyyy><mm>11</mm>
        <ovr_item_code>OVR0000000013</ovr_item_code><item_name>과일</item_name>
        <title>월보</title>
        <pdf_link>https://aglook.krei.re.kr/main/uObserveMonth/OVR0000000013/download/8778/ORGTEXT_FILE</pdf_link>
        <reg_date>20261103151004</reg_date><mod_date></mod_date>
      </item>
      <item>
        <yyyy>2026</yyyy><mm>11</mm>
        <ovr_item_code>OVR0000000031</ovr_item_code><item_name>과채</item_name>
        <title>월보</title>
        <pdf_link>https://aglook.krei.re.kr/broken_link_without_id</pdf_link>
        <reg_date>20261103155915</reg_date><mod_date></mod_date>
      </item>
    </channel>""".encode()
    reports, problems = parse_reports(xml_utf8, "monthly")
    assert len(reports) == 1 and reports[0]["file_id"] == 8778
    assert len(problems) == 1 and "broken_link_without_id" in problems[0]


# ── 3. 다음 달 및 범위 계산 ────────────────────────────────────────────────
def test_next_month_and_range_calculation():
    assert next_year_month(2026, 11) == (2026, 12)
    assert next_year_month(2026, 12) == (2027, 1)
    assert default_months(dt.date(2026, 12, 15)) == [(2026, 12), (2027, 1)]
    assert months_between("2026-10", "2027-02") == [
        (2026, 10),
        (2026, 11),
        (2026, 12),
        (2027, 1),
        (2027, 2),
    ]


# ── 4. 품목(item) 동기화: 빠진 코드는 is_listed=false, 삭제하지 않음 ─────────
def test_sync_items_marks_missing_codes_unlisted_without_deleting():
    fetched = parse_report_items(REPORT_ITEM_XML)
    existing = [
        dict(
            ovr_item_code="OVR0000000999",
            prt_ovr_item_code="OVR0000000093",
            krei_item_code="99",
            prt_krei_item_code="0",
            item_name="쪽파(구코드)",
            prt_item_name="원예",
            level=3,
            order_num=99,
            img_link=None,
            is_listed=True,
            updated_at="2026-10-01T00:00:00+09:00",
        )
    ]
    merged = sync_items(fetched, existing, "2026-11-07T20:40:00+09:00")
    assert len(merged) == 44
    by_code = {r["ovr_item_code"]: r for r in merged}
    assert by_code["OVR0000000999"]["is_listed"] is False
    assert by_code["OVR0000000999"]["item_name"] == "쪽파(구코드)"
    assert by_code["OVR0000000005"]["is_listed"] is True
    assert check_rows("krei.item", merged) == []


# ── 5. 글자 좌표(pages.json.gz) 및 1쪽 통권·발행일 추출 ──────────────────────
def test_extract_pdf_bytes_and_first_page_meta():
    pdf_bytes = make_test_pdf(["통권", "제312호", "2026년", "11월", "3일", "발행", "과일"])
    ext = extract_pdf_bytes(pdf_bytes)
    assert ext["pdf_bytes"] == len(pdf_bytes)
    assert len(ext["pdf_sha256"]) == 64
    assert ext["pages"] == 1
    assert ext["issue_no"] == 312
    assert ext["pub_date"] == "2026-11-03"
    assert "no_text_pages" in ext

    # gzip 압축을 풀었을 때 pages.json.gz 구조 확인: [{"w", "h", "words": [[x0, top, x1, bottom, text], ...]}]
    decompressed = json.loads(gzip.decompress(ext["gz_bytes"]).decode("utf-8"))
    assert isinstance(decompressed, list) and len(decompressed) == 1
    page0 = decompressed[0]
    assert set(page0.keys()) == {"w", "h", "words"}
    assert page0["w"] == 595.3 and page0["h"] == 841.9
    assert len(page0["words"]) >= 1
    w0 = page0["words"][0]
    assert len(w0) == 5
    assert all(isinstance(v, float) for v in w0[:4]) and isinstance(w0[4], str)


def test_extract_first_page_meta_returns_none_when_absent():
    pdf_bytes = make_test_pdf(["딸기", "관측속보", "생육", "동향"])
    ext = extract_pdf_bytes(pdf_bytes)
    assert ext["issue_no"] is None and ext["pub_date"] is None
    assert extract_first_page_meta("아무 내용 없음") == (None, None)


def test_build_pages_payload_rounds_to_1_decimal():
    pages = build_pages_payload(
        [
            {
                "w": 595.27559,
                "h": 841.88976,
                "words": [{"x0": 72.14, "top": 100.56, "x1": 120.89, "bottom": 112.54, "text": "사과"}],
            }
        ]
    )
    assert pages == [{"w": 595.3, "h": 841.9, "words": [[72.1, 100.6, 120.9, 112.5, "사과"]]}]
    raw = json.loads(gzip.decompress(compress_pages_json(pages)))
    assert raw == pages


# ── 6. 재실행(멱등) 및 mod_date 변경 감지 ────────────────────────────────────
def test_rerun_skips_already_fetched_and_refetches_only_when_mod_date_changes():
    clock = FakeClock("2026-11-07 20:40:00")
    http1 = FakeKreiHttp()
    r1 = K.run(months=[(2026, 11)], http=http1, clock=clock)
    assert not r1.failed
    assert len(r1.fetched) == 12 and r1.skipped_count == 0
    assert K.check(r1) == []

    # 2차 실행: 같은 목록으로 다시 돌리면 PDF 다운로드 건수 0개 (모두 skipped)
    prev_reports = {row["file_id"]: row for row in r1.reports}
    http2 = FakeKreiHttp()
    r2 = K.run(
        months=[(2026, 11)],
        existing_items=r1.items,
        existing_reports=prev_reports,
        http=http2,
        clock=clock,
    )
    assert len(r2.fetched) == 0 and r2.skipped_count == 12
    pdf_calls_2 = [u for u, _, _ in http2.calls if "/download/" in u]
    assert pdf_calls_2 == []

    # 3차 실행: 8778(과일) 한 건만 mod_date 가 새로 채워지면 그 1건만 다시 받는다
    modified_xml = MONTHLY_11_XML.decode("utf-8").replace(
        "<reg_date>20261103151004</reg_date>\t\t<mod_date></mod_date>",
        "<reg_date>20261103151004</reg_date>\t\t<mod_date>20261105100000</mod_date>",
        1,
    )
    http3 = FakeKreiHttp()
    http3.monthly_xml["2026-11"] = modified_xml.encode("utf-8")
    r3 = K.run(
        months=[(2026, 11)],
        existing_items=r2.items,
        existing_reports={row["file_id"]: row for row in r2.reports},
        http=http3,
        clock=clock,
    )
    assert [f["file_id"] for f in r3.fetched] == [8778]
    assert r3.skipped_count == 11
    fruit_row = next(row for row in r3.reports if row["file_id"] == 8778)
    assert fruit_row["mod_at"] == parse_kst_timestamp("20261105100000")


def test_needs_refetch_handles_utc_returned_by_supabase():
    existing = {"file_id": 8778, "fetch_status": "fetched", "mod_at": "2026-11-05T01:00:00+00:00"}
    same_in_kst = {"file_id": 8778, "mod_at": "2026-11-05T10:00:00+09:00"}
    changed_in_kst = {"file_id": 8778, "mod_at": "2026-11-05T11:00:00+09:00"}
    assert needs_refetch(same_in_kst, existing) is False
    assert needs_refetch(changed_in_kst, existing) is True


# ── 7. 요청 예절(1초 쉼·User-Agent·타임아웃·5초 뒤 1회 재시도) 및 DB/Storage 쓰기 ─
def test_politeness_retry_and_publish():
    fail_url = "https://aglook.krei.re.kr/main/api/reportItem"
    clock = FakeClock("2026-11-07 20:40:00")
    http = FakeKreiHttp(fail_once_urls={fail_url})
    r = K.run(months=[(2026, 11)], http=http, clock=clock)
    assert not r.failed
    # 첫 요청 실패 뒤 5초 대기 재시도 + 이후 요청마다 1초 대기
    assert 5.0 in clock.sleeps and 1.0 in clock.sleeps
    for url, timeout, headers in http.calls:
        assert headers["User-Agent"] == USER_AGENT
        assert timeout == (120 if "/download/" in url else 60)

    # 가짜 Supabase 에 publish: Content-Profile: krei 와 Storage krei-raw 경로 확인
    requests_log: list[tuple[str, str, dict, bytes]] = []

    def fake_sb(method, url, headers, body):
        requests_log.append((method, url, headers, body))
        return 201, b""

    writer = SupabaseWriter("https://x.supabase.co", "srv", http=fake_sb)
    K.publish(r, writer)
    assert r.uploaded and not r.failed

    storage_paths = [
        urllib.parse.urlparse(u).path.removeprefix("/storage/v1/object/")
        for _, u, _, _ in requests_log
        if "/storage/v1/object/" in u
    ]
    assert "krei-raw/list/monthly/2026-11.xml" in storage_paths
    assert "krei-raw/list/news/2026-11.xml" in storage_paths
    assert "krei-raw/text/OVR0000000013/2026/11/8778.pages.json.gz" in storage_paths

    rest_calls = [(u, h) for _, u, h, _ in requests_log if "/rest/v1/" in u]
    assert len(rest_calls) == 2
    assert rest_calls[0][0].endswith("/rest/v1/item?on_conflict=ovr_item_code")
    assert rest_calls[0][1]["Content-Profile"] == "krei"
    assert rest_calls[1][0].endswith("/rest/v1/report?on_conflict=file_id")
    assert rest_calls[1][1]["Content-Profile"] == "krei"


# ── 8. 감시 규칙 (core/monitor.py & collectors/monitor.py) ───────────────────
def _ok_run(now_str: str) -> list[dict]:
    t = TS(now_str) - pd.Timedelta(hours=1)
    return [
        dict(
            run_at=t.tz_localize("Asia/Seoul").isoformat(),
            run_name="저녁",
            stn_id="101",
            started_at=None,
            finished_at=(t + pd.Timedelta(minutes=15)).tz_localize("Asia/Seoul").isoformat(),
            result="정시 성공",
            on_time=True,
            error=None,
        )
    ]


def _hort_reports(codes: list[str], yyyy: int = 2026, mm: int = 11) -> list[dict]:
    names = {
        "OVR0000000013": "과일",
        "OVR0000000031": "과채",
        "OVR0000000008": "엽근채소",
        "OVR0000000036": "양념채소",
    }
    return [
        dict(
            file_id=8700 + i,
            yyyy=yyyy,
            mm=mm,
            ovr_item_code=c,
            item_name=names.get(c, c),
            report_type="월보",
            fetch_status="fetched",
            fetch_error=None,
        )
        for i, c in enumerate(codes)
    ]


def test_monitor_krei_9th_missing_hort_warns_7th_does_not_and_grain_ignored():
    # 과일·과채만 있고 엽근채소·양념채소가 없는 상태
    partial = _hort_reports(["OVR0000000013", "OVR0000000031"])

    # 7일이면 아직 마감(8일 23:59) 전이라 경고 없음
    now_7th = TS("2026-11-07 21:00")
    alerts_7 = check_health(now_7th, _ok_run("2026-11-07 21:00"), {}, {"101": "춘천"}, krei_reports=partial)
    assert [a for a in alerts_7 if a.code == "krei"] == []

    # 8일 23:59 도 아직 마감 안이라 경고 없음
    now_8th = TS("2026-11-08 23:59")
    alerts_8 = check_health(now_8th, _ok_run("2026-11-08 23:59"), {}, {"101": "춘천"}, krei_reports=partial)
    assert [a for a in alerts_8 if a.code == "krei"] == []

    # 9일 00:00 KST 경계부터 즉시 미수집 경고 발생
    now_9th_00 = TS("2026-11-09 00:00")
    alerts_9_00 = check_health(
        now_9th_00, _ok_run("2026-11-09 00:00"), {}, {"101": "춘천"}, krei_reports=partial
    )
    krei_9_00 = [a for a in alerts_9_00 if a.code == "krei"]
    assert len(krei_9_00) == 1
    assert krei_9_00[0].level == "warn"
    assert krei_9_00[0].text == "KREI 11월 월보 미수집 — 엽근채소, 양념채소"

    # 9일인데 엽근채소·양념채소 월보가 없으면 경고
    now_9th = TS("2026-11-09 09:40")
    alerts_9 = check_health(now_9th, _ok_run("2026-11-09 09:40"), {}, {"101": "춘천"}, krei_reports=partial)
    krei_alerts = [a for a in alerts_9 if a.code == "krei"]
    assert len(krei_alerts) == 1
    assert krei_alerts[0].level == "warn"
    assert krei_alerts[0].text == "KREI 11월 월보 미수집 — 엽근채소, 양념채소"
    assert "🟡 경고: KREI 11월 월보 미수집 — 엽근채소, 양념채소" in report(alerts_9, now_9th)

    # 원예 4부류가 모두 fetched 이면 곡물(쌀·콩 등)이나 축산이 없어도 경고 없음
    all_hort = _hort_reports(["OVR0000000013", "OVR0000000031", "OVR0000000008", "OVR0000000036"])
    alerts_ok = check_health(now_9th, _ok_run("2026-11-09 09:40"), {}, {"101": "춘천"}, krei_reports=all_hort)
    assert [a for a in alerts_ok if a.code == "krei"] == []


def test_monitor_krei_failed_report_warns_immediately_even_before_9th():
    now_5th = TS("2026-11-05 10:00")
    failed_rows = [
        dict(
            file_id=8780,
            yyyy=2026,
            mm=11,
            ovr_item_code="OVR0000000036",
            item_name="양념채소",
            report_type="월보",
            fetch_status="failed",
            fetch_error="PDF 다운로드 실패: HTTP 500\n상세 스택...",
        )
    ]
    alerts = check_health(now_5th, _ok_run("2026-11-05 10:00"), {}, {"101": "춘천"}, krei_reports=failed_rows)
    assert len(alerts) == 1
    assert alerts[0].level == "warn" and alerts[0].code == "krei"
    assert alerts[0].text == "KREI 수집 실패 (양념채소 #8780) — PDF 다운로드 실패: HTTP 500"


def test_monitor_read_krei_reports_sends_accept_profile_krei():
    seen: list[tuple[str, dict]] = []

    def fake_get(url: str, headers: dict) -> tuple[int, bytes]:
        seen.append((url, headers))
        return 200, b"[]"

    rows = M.read_krei_reports("https://x.supabase.co", "srv", http=fake_get, now=TS("2026-11-09 10:00"))
    assert rows == []
    assert len(seen) == 1
    url, headers = seen[0]
    assert "/rest/v1/report?" in url
    assert "yyyy.eq.2026,mm.eq.11" in url and "fetch_status.eq.failed" in url
    assert headers["Accept-Profile"] == "krei"


def test_monitor_krei_query_failure_warns_krei_only_and_continues_water_heat():
    now = TS("2026-11-09 19:00")
    ok_runs = _ok_run("2026-11-09 19:00")

    def fake_get(url: str, headers: dict) -> tuple[int, bytes]:
        if headers.get("Accept-Profile") == "krei":
            return 406, b'{"message":"The schema must be one of the following: public"}'
        if "/rest/v1/forecast_runs" in url:
            return 200, json.dumps(ok_runs).encode("utf-8")
        if "/rest/v1/stations" in url:
            return 200, json.dumps([{"stn_id": "101", "name_ko": "춘천"}]).encode("utf-8")
        if "/rest/v1/weather_daily" in url:
            # 어제(11/08) 관측이 없고 11/07까지만 있는 상태 → asos 경고가 나와야 함
            return 200, json.dumps([{"date": "2026-11-07"}]).encode("utf-8")
        if "/rest/v1/heat_daily" in url:
            # 어제(11/08) 적산온도가 없는 상태 → heat 경고가 나와야 함
            return 200, b"[]"
        return 200, b"[]"

    state = M.read_state("https://x.supabase.co", "srv", http=fake_get, now=now, include_krei=True)
    assert state["krei_reports"] is None
    assert "406" in (state.get("krei_error") or "")

    text, action = M.run_monitor(state, alerts_sink=None, now=now)
    assert action == "dry"
    # KREI 조회가 실패해도 멈추지 않고 asos · heat · krei 경고가 모두 함께 보고됨
    assert "🟡 경고: 어제(11/08) ASOS 관측 없음 — 101 춘천" in text
    assert "🟡 경고: 어제(11/08) 적산온도 없음 — 101 춘천" in text
    assert "🟡 경고: KREI 상태 조회 실패 — Supabase report 읽기 실패: HTTP 406" in text


# ── 9. 마이그레이션 SQL 구조 검증 ────────────────────────────────────────────
def test_krei_migration_sql_contains_schemas_rls_view_and_bucket():
    sql_path = Path(__file__).parent.parent / "supabase" / "migrations" / "20261108000000_krei_schema.sql"
    sql = sql_path.read_text(encoding="utf-8").lower()
    assert "create schema if not exists krei;" in sql
    assert "create table if not exists krei.item" in sql
    assert "create table if not exists krei.report" in sql
    assert "alter table krei.item enable row level security;" in sql
    assert "alter table krei.report enable row level security;" in sql
    assert "grant usage on schema krei to anon, authenticated, service_role;" in sql
    assert "grant all on all tables in schema krei to service_role;" in sql
    assert "grant all on all sequences in schema krei to service_role;" in sql
    assert "alter default privileges in schema krei grant all on tables to service_role;" in sql
    assert "alter default privileges in schema krei grant all on sequences to service_role;" in sql
    assert "grant usage on schema krei_api to anon, authenticated, service_role;" in sql
    assert "grant select on krei_api.reports to anon, authenticated, service_role;" in sql
    assert "grant select on krei.item to anon, authenticated;" in sql
    assert "grant select on krei.report to anon, authenticated;" in sql
    assert "create schema if not exists krei_api;" in sql
    assert "create or replace view krei_api.reports" in sql
    assert "security_invoker = true" in sql
    assert "where r.fetch_status = 'fetched'" in sql
    assert "values ('krei-raw', 'krei-raw', false)" in sql
    assert set(SPECS["krei.item"].columns) <= {
        "ovr_item_code",
        "prt_ovr_item_code",
        "krei_item_code",
        "prt_krei_item_code",
        "item_name",
        "prt_item_name",
        "level",
        "order_num",
        "img_link",
        "is_listed",
        "updated_at",
    }


def test_resolve_months_cli_validation():
    clock = FakeClock("2026-12-05 20:40:00")
    assert K.resolve_months(clock) == [(2026, 12), (2027, 1)]
    assert K.resolve_months(clock, yyyy="2026", mm="11") == [(2026, 11)]
    assert K.resolve_months(clock, from_ym="2026-01", to_ym="2026-03") == [
        (2026, 1),
        (2026, 2),
        (2026, 3),
    ]
    with pytest.raises(ValueError, match="함께 지정"):
        K.resolve_months(clock, yyyy="2026")


# ── 10. 시간 예산(25분)·한 건씩 저장·이어 받기 및 시험 실행(표본 1건) ───────────
def test_time_budget_stops_saves_incrementally_and_resumes_next_run(tmp_path: Path):
    clock1 = FakeClock("2026-11-07 20:40:00")
    base_http = FakeKreiHttp()

    # PDF 1건마다 시계를 10분 진행 → 25분 예산이면 3건(0·10·20분 시작)만 받고 멈춤
    def slow_pdf_http(url: str, timeout: int, headers: dict[str, str]):
        res = base_http(url, timeout, headers)
        if "/download/" in url:
            clock1.sleep(600)
        return res

    sb_calls: list[tuple[str, str, dict, bytes]] = []

    def fake_sb(method, url, headers, body):
        sb_calls.append((method, url, headers, body))
        return 201, b""

    writer1 = SupabaseWriter("https://x.supabase.co", "srv", http=fake_sb)
    r1 = K.run(
        months=[(2026, 11)],
        http=slow_pdf_http,
        clock=clock1,
        write=True,
        writer=writer1,
        budget_minutes=25.0,
        out_dir=tmp_path,
    )
    # 1차 실행: 12건 중 3건만 받고 시간 예산(25분)에 걸려 정상 종료 (실패·경고 아님)
    assert not r1.failed
    assert r1.listed_count == 12
    assert len(r1.fetched) == 3
    assert r1.remaining_count == 9
    assert [f["file_id"] for f in r1.fetched] == [8773, 8774, 8775]
    brief1 = K.brief_text(r1)
    assert "남은 9건, 다음 실행에서 이어 받음" in brief1

    # out/ 요약 파일이 처리 중에 갱신되어 남아 있음
    out_brief = (tmp_path / "krei_202611" / "brief.md").read_text(encoding="utf-8")
    assert "남은 9건, 다음 실행에서 이어 받음" in out_brief

    # 한 건씩 저장: report upsert 가 3번(각 1행씩) 호출되었고, 글자 좌표 업로드도 3번 끝남
    report_upserts = [
        json.loads(body.decode("utf-8"))
        for _, u, _, body in sb_calls
        if "/rest/v1/report?on_conflict=file_id" in u
    ]
    assert len(report_upserts) == 3
    assert [rows[0]["file_id"] for rows in report_upserts] == [8773, 8774, 8775]
    text_uploads = [u for _, u, _, _ in sb_calls if "/storage/v1/object/krei-raw/text/" in u]
    assert len(text_uploads) == 3

    # 2차 실행: 이미 fetched 된 3건(8773, 8774, 8775)은 건너뛰고 나머지 9건을 이어서 받는다
    saved_reports = {rows[0]["file_id"]: rows[0] for rows in report_upserts}
    clock2 = FakeClock("2026-11-08 20:40:00")
    http2 = FakeKreiHttp()
    r2 = K.run(
        months=[(2026, 11)],
        existing_items=r1.items,
        existing_reports=saved_reports,
        http=http2,
        clock=clock2,
        write=True,
        budget_minutes=25.0,
    )
    assert not r2.failed
    assert r2.skipped_count == 3
    assert len(r2.fetched) == 9
    assert r2.remaining_count == 0
    assert {f["file_id"] for f in r2.fetched}.isdisjoint({8773, 8774, 8775})
    assert len(r2.reports) == 12


def test_dry_run_write_false_fetches_at_most_one_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = FakeClock("2026-11-07 20:40:00")
    http = FakeKreiHttp()
    r = K.run(
        months=[(2026, 11)],
        http=http,
        clock=clock,
        write=False,
        out_dir=tmp_path,
    )
    assert not r.failed
    assert r.listed_count == 12
    assert len(r.fetched) == 1
    assert r.sample_skipped_count == 11
    pdf_downloads = [u for u, _, _ in http.calls if "/download/" in u]
    assert len(pdf_downloads) == 1
    assert "시험 실행(write=false): PDF 표본 1건만 받고 나머지 11건 생략" in K.brief_text(r)

    # 모두 이미 fetched 인 상태에서 시험 실행하면 PDF 를 0건(최대 1건 이하)만 받고 끝난다
    all_fetched = {
        fid: {"file_id": fid, "fetch_status": "fetched", "mod_at": None}
        for fid in [8773, 8774, 8775, 8776, 8777, 8778, 8779, 8780, 8783, 8784, 8785, 8786]
    }
    http_zero = FakeKreiHttp()
    r_zero = K.run(
        months=[(2026, 11)],
        existing_reports=all_fetched,
        http=http_zero,
        clock=clock,
        write=False,
    )
    assert len(r_zero.fetched) == 0
    assert [u for u, _, _ in http_zero.calls if "/download/" in u] == []

    # 환경변수 KREI_BUDGET_MINUTES 및 인자 해석 확인
    monkeypatch.setenv("KREI_BUDGET_MINUTES", "15")
    assert K.resolve_budget_minutes(None) == 15.0
    assert K.resolve_budget_minutes("40") == 40.0
    with pytest.raises(ValueError, match="0보다 커야"):
        K.resolve_budget_minutes(0)


# ── 11. 쪽 단위 판정(no_text·partial·ok), 소급 채우기, 이슈 댓글 자동 갱신 ─────
def test_compute_page_text_stats_and_no_text_summary():
    def _page(n_chars: int) -> dict:
        return {"w": 595, "h": 842, "words": [[0, 0, 10, 10, "가" * n_chars]] if n_chars else []}

    # 1쪽 199자(<200자) → no_text_pages=1, no_text / 1쪽 200자(>=200자) → no_text_pages=0, ok
    assert compute_page_text_stats([_page(199)]) == (199, 1, "no_text")
    assert compute_page_text_stats([_page(200)]) == (200, 0, "ok")
    assert compute_text_stats([_page(199)]) == (199, "no_text")
    assert compute_text_stats([_page(200)]) == (200, "ok")

    # 4쪽 중 4쪽 모두 <200자 → no_text
    assert compute_page_text_stats([_page(0), _page(50), _page(199), _page(0)]) == (249, 4, "no_text")
    # 4쪽 중 2쪽(절반 이상) <200자 → partial
    assert compute_page_text_stats([_page(0), _page(199), _page(200), _page(600)]) == (999, 2, "partial")
    # 4쪽 중 1쪽(절반 미만) <200자 → ok
    assert compute_page_text_stats([_page(120), _page(200), _page(500), _page(800)]) == (1620, 1, "ok")

    # #8703 (2026-06 과채 월보) 실제 분포: 20쪽 모두 2~127자(<200자) → no_text_pages=20, no_text
    p8703_counts = [2, 63, 86, 70, 95, 64, 127, 64, 90, 68, 96, 65, 91, 69, 89, 68, 88, 51, 72, 21]
    assert compute_page_text_stats([_page(c) for c in p8703_counts]) == (1439, 20, "no_text")

    # 글자 0개인 그림·윤곽선 PDF → fetched, no_text_pages=1, text_status='no_text', 요약에 표시됨
    empty_pdf = make_test_pdf([])
    clock = FakeClock("2026-11-07 20:40:00")
    http = FakeKreiHttp(pdf_bytes=empty_pdf)
    r = K.run(months=[(2026, 11)], http=http, clock=clock, write=False)
    assert not r.failed
    assert len(r.fetched) == 1
    rep = r.reports[0]
    assert rep["fetch_status"] == "fetched"
    assert rep["text_chars"] == 0
    assert rep["no_text_pages"] == 1
    assert rep["text_status"] == "no_text"
    summary = K.brief_text(r)
    assert "글자 없음(그림 PDF)" in summary
    assert "## 글자 없음(그림 PDF)" in summary


def test_backfill_text_status_reads_storage_without_downloading_pdf(tmp_path: Path):
    clock = FakeClock("2026-11-08 10:00:00")
    ok_gz = compress_pages_json([{"w": 595.3, "h": 841.9, "words": [[10, 10, 50, 20, "가" * 500]]}])
    empty_gz = compress_pages_json([{"w": 595.3, "h": 841.9, "words": []}])
    partial_gz = compress_pages_json(
        [
            {"w": 595.3, "h": 841.9, "words": [[10, 10, 50, 20, "가" * 80]]},
            {"w": 595.3, "h": 841.9, "words": [[10, 10, 50, 20, "가" * 600]]},
        ]
    )

    db_reports = [
        # 기존에 text_chars·text_status는 있으나 no_text_pages가 None인 행 → 다시 계산되어야 함
        dict(
            file_id=8778,
            api="monthly",
            report_type="월보",
            yyyy=2026,
            mm=11,
            ovr_item_code="OVR0000000013",
            prt_ovr_item_code="OVR0000000093",
            item_name="과일",
            pdf_link="https://aglook.krei.re.kr/main/uObserveMonth/OVR0000000013/download/8778/ORGTEXT_FILE",
            reg_at="2026-11-03T15:10:04+09:00",
            mod_at=None,
            description=None,
            first_seen_at="2026-11-07T20:40:00+09:00",
            pdf_sha256="a" * 64,
            pdf_bytes=8000000,
            pages=1,
            issue_no=1422,
            pub_date="2026-11-03",
            text_path="text/OVR0000000013/2026/11/8778.pages.json.gz",
            text_chars=500,
            no_text_pages=None,
            text_status="ok",
            fetch_status="fetched",
            fetch_error=None,
            parse_status="pending",
            parser_version=None,
            parsed_at=None,
            updated_at="2026-11-07T20:40:00+09:00",
        ),
        dict(
            file_id=8780,
            api="monthly",
            report_type="월보",
            yyyy=2026,
            mm=11,
            ovr_item_code="OVR0000000036",
            prt_ovr_item_code="OVR0000000093",
            item_name="양념채소",
            pdf_link="https://aglook.krei.re.kr/main/uObserveMonth/OVR0000000036/download/8780/ORGTEXT_FILE",
            reg_at="2026-11-03T15:10:04+09:00",
            mod_at=None,
            description=None,
            first_seen_at="2026-11-07T20:40:00+09:00",
            pdf_sha256="b" * 64,
            pdf_bytes=4000000,
            pages=1,
            issue_no=None,
            pub_date=None,
            text_path="text/OVR0000000036/2026/11/8780.pages.json.gz",
            text_chars=None,
            no_text_pages=None,
            text_status=None,
            fetch_status="fetched",
            fetch_error=None,
            parse_status="pending",
            parser_version=None,
            parsed_at=None,
            updated_at="2026-11-07T20:40:00+09:00",
        ),
        dict(
            file_id=8779,
            api="monthly",
            report_type="월보",
            yyyy=2026,
            mm=11,
            ovr_item_code="OVR0000000031",
            prt_ovr_item_code="OVR0000000093",
            item_name="과채",
            pdf_link="https://aglook.krei.re.kr/main/uObserveMonth/OVR0000000031/download/8779/ORGTEXT_FILE",
            reg_at="2026-11-03T15:10:04+09:00",
            mod_at=None,
            description=None,
            first_seen_at="2026-11-07T20:40:00+09:00",
            pdf_sha256="c" * 64,
            pdf_bytes=5000000,
            pages=2,
            issue_no=None,
            pub_date=None,
            text_path="text/OVR0000000031/2026/11/8779.pages.json.gz",
            text_chars=None,
            no_text_pages=None,
            text_status=None,
            fetch_status="fetched",
            fetch_error=None,
            parse_status="pending",
            parser_version=None,
            parsed_at=None,
            updated_at="2026-11-07T20:40:00+09:00",
        ),
    ]

    get_urls: list[str] = []

    def fake_rest_get(url: str, headers: dict) -> tuple[int, bytes]:
        get_urls.append(url)
        if "/rest/v1/item" in url:
            return 200, b"[]"
        if "/rest/v1/report" in url:
            return 200, json.dumps(db_reports).encode("utf-8")
        if url.endswith("8778.pages.json.gz"):
            return 200, ok_gz
        if url.endswith("8780.pages.json.gz"):
            return 200, empty_gz
        if url.endswith("8779.pages.json.gz"):
            return 200, partial_gz
        return 404, b"Not Found"

    upserted: list[dict] = []

    def fake_sb_write(method, url, headers, body):
        if "/rest/v1/report" in url:
            upserted.extend(json.loads(body.decode("utf-8")))
        return 201, b""

    writer = SupabaseWriter("https://x.supabase.co", "srv", http=fake_sb_write)
    r = K.backfill_text_status(
        "https://x.supabase.co",
        "srv",
        http=fake_rest_get,
        writer=writer,
        clock=clock,
        out_dir=tmp_path,
    )
    assert not r.failed
    # KREI PDF 다운로드 요청 없이 Storage 의 pages.json.gz 만 읽었는지 확인
    assert all("aglook.krei.re.kr" not in u for u in get_urls)
    assert len(upserted) == 3
    by_id = {row["file_id"]: row for row in upserted}
    assert by_id[8778]["text_chars"] == 500
    assert by_id[8778]["no_text_pages"] == 0
    assert by_id[8778]["text_status"] == "ok"
    assert by_id[8780]["text_chars"] == 0
    assert by_id[8780]["no_text_pages"] == 1
    assert by_id[8780]["text_status"] == "no_text"
    assert by_id[8779]["text_chars"] == 680
    assert by_id[8779]["no_text_pages"] == 1
    assert by_id[8779]["text_status"] == "partial"

    # 이미 no_text_pages까지 채워진 행이라도 force=True 면 전체를 다시 계산한다
    db_reports = list(by_id.values())
    db_reports[0]["no_text_pages"] = 99
    upserted.clear()
    r_force = K.backfill_text_status(
        "https://x.supabase.co",
        "srv",
        http=fake_rest_get,
        writer=writer,
        clock=clock,
        force=True,
    )
    assert not r_force.failed
    assert len(upserted) == 3
    assert next(row for row in upserted if row["file_id"] == 8778)["no_text_pages"] == 0


def test_notify_unreadable_issue_comments_for_in_scope_and_never_reopens_closed_issue():
    clock = FakeClock("2026-11-07 20:40:00")
    empty_pdf = make_test_pdf([])
    http = FakeKreiHttp(pdf_bytes=empty_pdf)

    # 닫혀 있는 "[krei] 판독 불가 월보 목록(그림 PDF)" 이슈 준비
    issues: list[dict] = [
        dict(number=57, title=UNREADABLE_ISSUE_TITLE, state="closed", body="초기 본문"),
    ]
    comments: list[tuple[int, str]] = []
    patched_states: list[str] = []

    def fake_gh(method, url, headers, body):
        path = urllib.parse.urlparse(url).path.removeprefix("/repos/o/r")
        data = json.loads(body) if body else None
        if path == "/issues" and method == "GET":
            return 200, json.dumps(issues).encode("utf-8")
        if path == "/issues/57/comments" and method == "POST":
            comments.append((57, data["body"]))
            return 201, b"{}"
        if path == "/issues/57" and method == "PATCH":
            if "state" in data:
                patched_states.append(data["state"])
            issues[0].update(data)
            return 200, json.dumps(issues[0]).encode("utf-8")
        return 404, b"Not Found"

    sink = IssueAlerts("o/r", "tok", http=fake_gh, title=UNREADABLE_ISSUE_TITLE, label="krei")
    r = K.run(months=[(2026, 11)], http=http, clock=clock, write=True, issue_sink=sink)
    assert not r.failed
    assert r.issue_action == "commented"
    # 이슈가 닫혀 있어도 다시 열지 않고(PATCH state 없음) 댓글만 1건 달림
    assert patched_states == []
    assert issues[0]["state"] == "closed"
    assert len(comments) == 1
    comment_body = comments[0][1]
    # 범위 안 부류(원예 4부류 + 쌀·콩·감자)만 포함
    assert "#8775" in comment_body and "쌀" in comment_body
    assert "#8778" in comment_body and "과일" in comment_body
    assert "#8779" in comment_body and "과채" in comment_body
    assert "#8780" in comment_body and "양념채소" in comment_body
    assert "#8786" in comment_body and "엽근채소" in comment_body
    assert (
        "https://aglook.krei.re.kr/main/uObserveMonth/OVR0000000008/download/8786/ORGTEXT_FILE"
        in comment_body
    )
    # 범위 밖 부류(육계 #8773, 수입과일 #8774, 산란계 #8776/#8784, 국제곡물 #8777, 해외축산 #8785) 제외
    assert "#8776" not in comment_body
    assert "#8777" not in comment_body
    assert "#8785" not in comment_body


def test_monitor_no_text_and_partial_do_not_warn():
    now = TS("2026-11-05 10:00")
    rows = [
        dict(
            file_id=8780,
            yyyy=2026,
            mm=11,
            ovr_item_code="OVR0000000036",
            item_name="양념채소",
            report_type="월보",
            fetch_status="fetched",
            text_chars=0,
            no_text_pages=16,
            text_status="no_text",
        ),
        dict(
            file_id=8779,
            yyyy=2026,
            mm=11,
            ovr_item_code="OVR0000000031",
            item_name="과채",
            report_type="월보",
            fetch_status="fetched",
            text_chars=300,
            no_text_pages=12,
            text_status="partial",
        ),
    ]
    alerts = check_health(now, _ok_run("2026-11-05 10:00"), {}, {"101": "춘천"}, krei_reports=rows)
    assert [a for a in alerts if a.code == "krei"] == []


def test_krei_second_and_third_migrations():
    mig_dir = Path(__file__).parent.parent / "supabase" / "migrations"
    first_sql = (mig_dir / "20261108000000_krei_schema.sql").read_text(encoding="utf-8").lower()
    second_sql = (mig_dir / "20261108010000_krei_report_text_status.sql").read_text(encoding="utf-8").lower()
    third_sql = (
        (mig_dir / "20261108020000_krei_unreadable_and_page_status.sql").read_text(encoding="utf-8").lower()
    )
    assert "text_chars" not in first_sql
    assert "add column if not exists text_chars int" in second_sql
    assert "add column if not exists text_status text" in second_sql
    assert "check (text_status in ('ok', 'no_text'))" in second_sql
    assert "no_text_pages" not in second_sql

    assert "add column if not exists no_text_pages smallint" in third_sql
    assert "check (text_status in ('ok', 'partial', 'no_text'))" in third_sql
    assert "create or replace view krei_api.unreadable" in third_sql
    assert "r.text_status in ('no_text', 'partial')" in third_sql
    assert "grant select on krei_api.unreadable to anon, authenticated, service_role;" in third_sql
