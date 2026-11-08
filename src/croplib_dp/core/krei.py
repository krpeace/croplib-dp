"""KREI 농업관측 수집기 순수 규칙: XML 파싱, 파일번호 추출, 조회 달 계산, 재수집 판정, 글자 좌표 가공.

DB·네트워크·파일 시스템을 모르는 순수 함수만 둔다 (클린 아키텍처 원칙).
"""

import datetime as dt
import gzip
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd

HORT_MONTH_ITEMS: dict[str, str] = {
    "OVR0000000013": "과일",
    "OVR0000000031": "과채",
    "OVR0000000008": "엽근채소",
    "OVR0000000036": "양념채소",
}
GRAIN_ITEMS: dict[str, str] = {
    "OVR0000000070": "쌀",
    "OVR0000000072": "콩",
    "OVR0000000021": "감자",
}
UNREADABLE_SCOPE_ITEMS: dict[str, str] = {**HORT_MONTH_ITEMS, **GRAIN_ITEMS}
NO_TEXT_WARN_ITEMS: dict[str, str] = UNREADABLE_SCOPE_ITEMS
UNREADABLE_ISSUE_TITLE = "[krei] 판독 불가 월보 목록(그림 PDF)"
MIN_CHARS_PER_PAGE = 30
_UNUSED_CLOSE = {}

_FILE_ID_RE = re.compile(r"/download/(\d+)(?:/|$)")
_ISSUE_NO_RE = re.compile(r"통권\s*제\s*(\d+)\s*호")
_PUB_DATE_RE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*발행")


def _txt(el: ET.Element, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def parse_report_items(xml_data: bytes | str) -> list[dict[str, Any]]:
    """reportItem XML → krei.item 행 목록 (updated_at 제외).
    krei_item_code·prt_krei_item_code 는 앞자리 0 이 있는 문자열 그대로 보존한다."""
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        raise ValueError(f"reportItem XML 파싱 실패: {e}") from e
    rows: list[dict[str, Any]] = []
    for it in root.findall(".//item"):
        code = _txt(it, "ovr_item_code")
        name = _txt(it, "item_name")
        level_s = _txt(it, "level")
        if not code or not name or not level_s:
            continue
        order_s = _txt(it, "order_num")
        rows.append(
            dict(
                ovr_item_code=code,
                prt_ovr_item_code=_txt(it, "prt_ovr_item_code") or None,
                krei_item_code=_txt(it, "krei_item_code") or None,
                prt_krei_item_code=_txt(it, "prt_krei_item_code") or None,
                item_name=name,
                prt_item_name=_txt(it, "prt_item_name") or None,
                level=int(level_s),
                order_num=int(order_s) if order_s else None,
                img_link=_txt(it, "ovr_item_img_link") or None,
                is_listed=True,
            )
        )
    return rows


def sync_items(
    fetched_items: Iterable[Mapping[str, Any]],
    existing_items: Iterable[Mapping[str, Any]],
    updated_at: str,
) -> list[dict[str, Any]]:
    """이번 reportItem 응답과 기존 DB 행을 합쳐 krei.item 덮어쓰기 목록을 만든다.
    응답에서 사라진 코드는 지우지 않고 is_listed = false 로 바꾼다."""
    out: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for it in fetched_items:
        code = str(it["ovr_item_code"])
        seen_codes.add(code)
        out.append(
            dict(
                ovr_item_code=code,
                prt_ovr_item_code=it.get("prt_ovr_item_code"),
                krei_item_code=it.get("krei_item_code"),
                prt_krei_item_code=it.get("prt_krei_item_code"),
                item_name=it["item_name"],
                prt_item_name=it.get("prt_item_name"),
                level=int(it["level"]),
                order_num=it.get("order_num"),
                img_link=it.get("img_link"),
                is_listed=True,
                updated_at=updated_at,
            )
        )
    for ex in existing_items:
        code = str(ex["ovr_item_code"])
        if code in seen_codes:
            continue
        seen_codes.add(code)
        out.append(
            dict(
                ovr_item_code=code,
                prt_ovr_item_code=ex.get("prt_ovr_item_code"),
                krei_item_code=ex.get("krei_item_code"),
                prt_krei_item_code=ex.get("prt_krei_item_code"),
                item_name=ex["item_name"],
                prt_item_name=ex.get("prt_item_name"),
                level=int(ex["level"]),
                order_num=ex.get("order_num"),
                img_link=ex.get("img_link"),
                is_listed=False,
                updated_at=updated_at,
            )
        )
    return out


def extract_file_id(pdf_link: str | None) -> int | None:
    r"""pdf_link 에서 '/download/(\d+)/' 로 파일번호를 뽑는다. 못 뽑으면 None."""
    if not pdf_link:
        return None
    m = _FILE_ID_RE.search(str(pdf_link).strip())
    return int(m.group(1)) if m else None


def resolve_pdf_url(pdf_link: str, base_url: str = "https://aglook.krei.re.kr/main/api") -> str:
    """상대 경로 pdf_link 를 전체 URL 로 바꾼다."""
    link = pdf_link.strip()
    if link.startswith("http://") or link.startswith("https://"):
        return link
    origin = base_url.split("/main/api")[0].rstrip("/")
    return f"{origin}/{link.lstrip('/')}"


def parse_kst_timestamp(raw: str | None) -> str | None:
    """'YYYYMMDDHHMMSS'(또는 'YYYYMMDD') 문자열을 KST timestamptz ISO 문자열로 바꾼다."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{14}", s):
        try:
            t = dt.datetime.strptime(s, "%Y%m%d%H%M%S")
            return t.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        except ValueError:
            return None
    if re.fullmatch(r"\d{8}", s):
        try:
            d = dt.datetime.strptime(s, "%Y%m%d")
            return d.strftime("%Y-%m-%dT00:00:00+09:00")
        except ValueError:
            return None
    try:
        ts = pd.Timestamp(s)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("Asia/Seoul").tz_localize(None)
        return ts.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except Exception:
        return None


def parse_reports(xml_data: bytes | str, api: str) -> tuple[list[dict[str, Any]], list[str]]:
    """montlyReport('monthly') 또는 newsReport('news') XML → (보고서 목록, 파일번호 실패 설명 목록).
    파일번호가 안 뽑히는 건은 실패로 기록하고 건너뛴다(전체 중단 없음)."""
    if api not in ("monthly", "news"):
        raise ValueError(f"알 수 없는 api 구분: {api!r}")
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        raise ValueError(f"{api} XML 파싱 실패: {e}") from e
    reports: list[dict[str, Any]] = []
    problems: list[str] = []
    for it in root.findall(".//item"):
        yyyy_s = _txt(it, "yyyy")
        mm_s = _txt(it, "mm")
        code = _txt(it, "ovr_item_code")
        name = _txt(it, "item_name")
        title = _txt(it, "title")
        pdf_link = _txt(it, "pdf_link")
        file_id = extract_file_id(pdf_link)
        if file_id is None:
            problems.append(
                f"{api} {yyyy_s}-{mm_s} {name}({title}): pdf_link에서 파일번호 추출 실패 ({pdf_link!r})"
            )
            continue
        reports.append(
            dict(
                file_id=file_id,
                api=api,
                report_type=title,
                yyyy=int(yyyy_s),
                mm=int(mm_s),
                ovr_item_code=code,
                prt_ovr_item_code=_txt(it, "prt_ovr_item_code") or None,
                item_name=name,
                pdf_link=pdf_link,
                reg_at=parse_kst_timestamp(_txt(it, "reg_date")),
                mod_at=parse_kst_timestamp(_txt(it, "mod_date")),
                description=_txt(it, "description") or None,
            )
        )
    return reports, problems


def next_year_month(yyyy: int, mm: int) -> tuple[int, int]:
    """다음 달 (연, 월). 12월이면 다음 해 1월."""
    if not (1 <= mm <= 12):
        raise ValueError(f"월 범위 오류: {mm}")
    return (yyyy + 1, 1) if mm == 12 else (yyyy, mm + 1)


def default_months(today: dt.date) -> list[tuple[int, int]]:
    """평소 조회 대상: 이번 달과 다음 달."""
    cur = (today.year, today.month)
    return [cur, next_year_month(*cur)]


def parse_ym(s: str) -> tuple[int, int]:
    """'YYYY-MM' → (yyyy, mm)"""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", str(s).strip())
    if not m:
        raise ValueError(f"연-월 형식이 아닙니다 (예: 2026-11): {s!r}")
    yyyy, mm = int(m.group(1)), int(m.group(2))
    if not (1 <= mm <= 12):
        raise ValueError(f"월은 1~12 사이여야 합니다: {s!r}")
    return yyyy, mm


def months_between(from_ym: str, to_ym: str) -> list[tuple[int, int]]:
    """'2026-01' ~ '2026-11' 사이의 모든 (연, 월) 목록 (양끝 포함)."""
    y1, m1 = parse_ym(from_ym)
    y2, m2 = parse_ym(to_ym)
    if (y1, m1) > (y2, m2):
        raise ValueError(f"시작 연월({from_ym})이 끝 연월({to_ym})보다 늦습니다")
    out: list[tuple[int, int]] = []
    y, m = y1, m1
    while (y, m) <= (y2, m2):
        out.append((y, m))
        y, m = next_year_month(y, m)
    return out


def _norm_mod(val: Any) -> str | None:
    if val is None or val == "":
        return None
    return parse_kst_timestamp(str(val)) or str(val).strip()


def needs_refetch(new_row: Mapping[str, Any], existing_row: Mapping[str, Any] | None) -> bool:
    """이미 fetched 인 건은 다시 받지 않는다. 단 mod_date(mod_at)가 새로 채워졌거나 바뀌었으면 다시 받는다."""
    if existing_row is None:
        return True
    if existing_row.get("fetch_status") != "fetched":
        return True
    new_mod = _norm_mod(new_row.get("mod_at") or new_row.get("mod_date"))
    old_mod = _norm_mod(existing_row.get("mod_at") or existing_row.get("mod_date"))
    if new_mod is not None and new_mod != old_mod:
        return True
    return False


def extract_first_page_meta(text: str | None) -> tuple[int | None, str | None]:
    r"""1쪽 글자에서 통권 번호('통권 제(\d+)호')와 발행일('(\d{4})년 (\d{1,2})월 (\d{1,2})일 발행') 추출.
    못 찾으면 None 으로 둔다(오류 아님)."""
    if not text:
        return None, None
    m_no = _ISSUE_NO_RE.search(text)
    issue_no = int(m_no.group(1)) if m_no else None
    pub_date: str | None = None
    m_dt = _PUB_DATE_RE.search(text)
    if m_dt:
        try:
            pub_date = dt.date(int(m_dt.group(1)), int(m_dt.group(2)), int(m_dt.group(3))).isoformat()
        except ValueError:
            pub_date = None
    return issue_no, pub_date


def build_pages_payload(raw_pages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """쪽별 크기·글자 좌표를 소수 1자리로 반올림해 {"w","h","words":[...]} 목록으로 만든다."""
    out: list[dict[str, Any]] = []
    for p in raw_pages:
        w = round(float(p["w"]), 1)
        h = round(float(p["h"]), 1)
        words: list[list[Any]] = []
        for wd in p.get("words", ()):
            if isinstance(wd, Mapping):
                txt = str(wd.get("text", "")).strip()
                if not txt:
                    continue
                words.append(
                    [
                        round(float(wd["x0"]), 1),
                        round(float(wd["top"]), 1),
                        round(float(wd["x1"]), 1),
                        round(float(wd["bottom"]), 1),
                        txt,
                    ]
                )
            else:
                x0, top, x1, bottom, txt = wd
                txt = str(txt).strip()
                if not txt:
                    continue
                words.append(
                    [
                        round(float(x0), 1),
                        round(float(top), 1),
                        round(float(x1), 1),
                        round(float(bottom), 1),
                        txt,
                    ]
                )
        out.append({"w": w, "h": h, "words": words})
    return out


def compress_pages_json(pages: list[dict[str, Any]]) -> bytes:
    """pages 목록을 JSON 직렬화 후 gzip 압축한다."""
    raw = json.dumps(pages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, mtime=0)


def decompress_pages_json(gz_bytes: bytes) -> list[dict[str, Any]]:
    """gzip 압축된 pages.json.gz 바이트를 풀어 쪽별 단어 좌표 목록으로 돌려준다."""
    return json.loads(gzip.decompress(gz_bytes).decode("utf-8"))


def compute_page_text_stats(pages: Iterable[Mapping[str, Any]]) -> tuple[int, int, str]:
    """쪽별 단어 좌표 목록(pages)에서 (전체 글자 수 text_chars, 글자 없는 쪽 수 no_text_pages,
    상태 'ok' | 'partial' | 'no_text')를 계산한다.
    - 쪽당 글자 30자(`MIN_CHARS_PER_PAGE`) 미만인 쪽을 '글자 없는 쪽'(`no_text_pages`)으로 센다.
    - 모든 쪽이 글자 없는 쪽이면 'no_text', 절반 이상(`no_text_pages * 2 >= n_pages`)이면 'partial',
      나머지는 'ok'로 판정한다."""
    page_list = list(pages)
    n_pages = len(page_list)
    text_chars = 0
    no_text_pages = 0
    for p in page_list:
        page_chars = 0
        for wd in p.get("words", ()):
            if isinstance(wd, Mapping):
                txt = str(wd.get("text", "")).strip()
            else:
                txt = str(wd[4]).strip() if len(wd) >= 5 else ""
            page_chars += len(txt)
        text_chars += page_chars
        if page_chars < MIN_CHARS_PER_PAGE:
            no_text_pages += 1
    if n_pages <= 0 or no_text_pages == n_pages:
        status = "no_text"
    elif no_text_pages * 2 >= n_pages:
        status = "partial"
    else:
        status = "ok"
    return text_chars, no_text_pages, status


def compute_text_stats(pages: Iterable[Mapping[str, Any]]) -> tuple[int, str]:
    """하위 호환용: (text_chars, text_status) 2-튜플 반환."""
    text_chars, _no_text_pages, status = compute_page_text_stats(pages)
    return text_chars, status


def is_unreadable_in_scope(ovr_item_code: str | None, text_status: str | None) -> bool:
    """대상 범위(원예 4부류 + 쌀·콩·감자)에서 판독 불가('no_text' 또는 'partial')인지 여부."""
    return str(ovr_item_code or "") in UNREADABLE_SCOPE_ITEMS and text_status in ("no_text", "partial")


def format_unreadable_comment(reports: Iterable[Mapping[str, Any]]) -> str:
    """새로 받은 범위 안 판독 불가('no_text'·'partial') 건을 GitHub 이슈 댓글 마크다운으로 만든다."""
    rows = list(reports)
    lines = [
        "### 새로 확인된 판독 불가 발행물",
        "",
        "| 부류 | 월호 | 파일번호 | 쪽수 (글자 없는 쪽) | 상태 | PDF 링크 |",
        "|---|---:|---:|---:|:---:|---|",
    ]
    for r in rows:
        code = str(r.get("ovr_item_code") or "")
        name = r.get("item_name") or UNREADABLE_SCOPE_ITEMS.get(code, code)
        ym = f"{int(r['yyyy']):04d}-{int(r['mm']):02d} {r.get('report_type') or '월보'}"
        fid = int(r["file_id"])
        pages = int(r.get("pages") or 0)
        no_pages = int(r.get("no_text_pages") or 0)
        status = r.get("text_status") or "no_text"
        url = resolve_pdf_url(str(r.get("pdf_link") or ""))
        lines.append(
            f"| {name} | {ym} | `#{fid}` | {pages}쪽 ({no_pages}쪽) | `{status}` | [원문 PDF]({url}) |"
        )
    return "\n".join(lines)


def list_storage_path(api: str, yyyy: int, mm: int) -> str:
    """Storage krei-raw 버킷 안의 목록 XML 경로: list/{api}/{yyyy}-{mm}.xml"""
    return f"list/{api}/{yyyy:04d}-{mm:02d}.xml"


def text_storage_path(ovr_item_code: str, yyyy: int, mm: int, file_id: int) -> str:
    """Storage krei-raw 버킷 안의 글자 좌표 경로: text/{ovr_item_code}/{yyyy}/{mm}/{file_id}.pages.json.gz"""
    return f"text/{ovr_item_code}/{yyyy:04d}/{mm:02d}/{file_id}.pages.json.gz"
