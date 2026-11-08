r"""KREI 농업관측 수집기 1단계: 품목분류·발행물 목록·글자 좌표(pages.json.gz) 수집.

순서:
  1. `reportItem` → `krei.item` 덮어쓰기 (이번 응답에 없는 코드는 지우지 않고 `is_listed = false` 로 전환)
  2. `montlyReport`·`newsReport` 를 이번 달과 다음 달(또는 `--yyyy --mm`, `--from --to`) 조회
     - 목록 XML 원문은 Storage `krei-raw/list/{api}/{yyyy}-{mm}.xml` 에 보관
     - `pdf_link` 에서 `/download/(\d+)/` 로 `file_id` 추출 (못 뽑으면 그 건만 실패로 기록하고 계속)
  3. 아직 `fetched` 가 아니거나 `mod_date` 가 새로 채워진/바뀐 건만 PDF 를 임시 폴더에 받아:
     - `write=false`(시험 실행)이면 PDF 는 표본 1건만 받고 끝낸다.
     - 시간 예산(기본 25분, `--budget-minutes` 또는 `KREI_BUDGET_MINUTES`)이 지나면 새 PDF 를 시작하지 않고
       정상 종료하며, 요약에 `"남은 N건, 다음 실행에서 이어 받음"`을 남긴다.
     - PDF 1건을 처리할 때마다 바로 글자 좌표(`pages.json.gz`) 업로드와 `krei.report` 행 upsert,
       그리고 `out/` 요약 파일 갱신을 끝내 중간에 취소돼도 이미 받은 건이 남게 한다.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from croplib_dp.collectors.water import Clock, RunLog, _env, _iso
from croplib_dp.core import CALC_VERSION
from croplib_dp.core.krei import (
    default_months,
    list_storage_path,
    months_between,
    needs_refetch,
    parse_report_items,
    parse_reports,
    sync_items,
    text_storage_path,
)
from croplib_dp.io.contract import SPECS, check_rows
from croplib_dp.io.dictionaries import RestGet, read_table, rest_get
from croplib_dp.io.krei import (
    STORAGE_BUCKET,
    KreiClient,
    KreiHttp,
    extract_pdf_bytes,
    http_get,
)

KREI_SCHEMA = "krei"
ITEM_KEY = SPECS["krei.item"].key
REPORT_KEY = SPECS["krei.report"].key
DEFAULT_BUDGET_MINUTES = 25.0
PdfExtractor = Callable[[bytes], dict[str, Any]]


@dataclass
class KreiResult:
    months: list[tuple[int, int]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    listed_count: int = 0  # 조회된 달 전체 고유 발행물 건수
    list_counts: dict[str, int] = field(default_factory=dict)  # "2026-11 monthly" -> 건수
    fetched: list[dict[str, Any]] = field(default_factory=list)  # 새로 받은 건 요약
    failed_reports: list[dict[str, Any]] = field(default_factory=list)  # 실패한 건 요약
    skipped_count: int = 0  # 이미 fetched 라 건너뛴 건수
    remaining_count: int = 0  # 시간 예산 도달로 다음 실행에 이어 받을 건수
    sample_skipped_count: int = 0  # 시험 실행(write=false)에서 표본 1건만 받고 생략한 건수
    list_files: dict[str, bytes] = field(default_factory=dict)  # Storage path -> xml bytes
    text_files: dict[str, bytes] = field(default_factory=dict)  # Storage path -> pages.json.gz bytes
    problems: list[str] = field(default_factory=list)
    log: RunLog = field(default_factory=RunLog)
    uploaded: bool = False
    _persisted_incrementally: bool = False

    @property
    def failed(self) -> bool:
        return bool(self.problems)


def read_existing(
    base_url: str, key: str, http: RestGet = rest_get
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Supabase krei.item 과 krei.report 의 현재 행을 읽는다."""
    items = read_table(base_url, key, "item", http=http, query="select=*", schema=KREI_SCHEMA)
    reports = read_table(base_url, key, "report", http=http, query="select=*", schema=KREI_SCHEMA)
    by_id = {int(r["file_id"]): r for r in reports if r.get("file_id") is not None}
    return items, by_id


def resolve_months(
    clock: Clock,
    yyyy: str | int | None = None,
    mm: str | int | None = None,
    from_ym: str | None = None,
    to_ym: str | None = None,
) -> list[tuple[int, int]]:
    """CLI 인자 → 조회할 (연, 월) 목록.
    - `--from` / `--to`: 범위 조회 (한쪽만 주면 그 달만)
    - `--yyyy` / `--mm`: 지정한 달만 조회
    - 아무것도 안 주면: KST 기준 이번 달과 다음 달"""
    if from_ym or to_ym:
        start = (from_ym or to_ym or "").strip()
        end = (to_ym or from_ym or "").strip()
        return months_between(start, end)
    if yyyy and mm:
        y, m = int(str(yyyy).strip()), int(str(mm).strip())
        if not (1 <= m <= 12):
            raise ValueError(f"월은 1~12 사이여야 합니다: {mm!r}")
        return [(y, m)]
    if yyyy or mm:
        raise ValueError("--yyyy 와 --mm 은 함께 지정해야 합니다")
    return default_months(clock.now().date())


def resolve_budget_minutes(budget_minutes: float | str | None = None) -> float:
    """CLI 인자 또는 환경변수 `KREI_BUDGET_MINUTES` 에서 시간 예산(분)을 읽는다 (기본 25분)."""
    if budget_minutes is not None and str(budget_minutes).strip() != "":
        val = float(budget_minutes)
    else:
        env_val = os.environ.get("KREI_BUDGET_MINUTES", "").strip()
        val = float(env_val) if env_val else DEFAULT_BUDGET_MINUTES
    if val <= 0:
        raise ValueError(f"시간 예산(budget_minutes)은 0보다 커야 합니다: {val}")
    return val


def _ensure_report_items_exist(
    items: list[dict[str, Any]], reports: Iterable[Mapping[str, Any]], updated_at: str
) -> None:
    """과거 월보에 현재 reportItem 에 없는 부류 코드가 있으면 외래키를 위해 is_listed=false 로 보충한다."""
    known = {str(it["ovr_item_code"]) for it in items}
    for r in reports:
        code = str(r["ovr_item_code"])
        if code in known:
            continue
        known.add(code)
        items.append(
            dict(
                ovr_item_code=code,
                prt_ovr_item_code=r.get("prt_ovr_item_code"),
                krei_item_code=None,
                prt_krei_item_code=None,
                item_name=r.get("item_name") or code,
                prt_item_name=None,
                level=2,
                order_num=None,
                img_link=None,
                is_listed=False,
                updated_at=updated_at,
            )
        )


def _persist_items_and_lists(r: KreiResult, writer: Any) -> bool:
    """품목 사전(krei.item)과 목록 XML을 먼저 올린다 (report 외래키 참조 보장)."""
    probs = check_rows("krei.item", r.items)
    if probs:
        r.problems += probs
        return False
    for path, xml_bytes in sorted(r.list_files.items()):
        try:
            writer.upload(STORAGE_BUCKET, path, xml_bytes, content_type="application/xml")
        except Exception as e:
            r.problems.append(f"목록 XML Storage 올리기 실패 ({path}): {type(e).__name__}: {str(e)[:200]}")
    try:
        if r.items:
            writer.upsert("item", r.items, on_conflict=ITEM_KEY, schema=KREI_SCHEMA)
        r.uploaded = True
        return True
    except Exception as e:
        r.problems.append(f"DB 올리기 실패 (item): {type(e).__name__}: {str(e)[:200]}")
        return False


def _persist_one_report(
    r: KreiResult,
    writer: Any,
    rep: dict[str, Any],
    gz_bytes: bytes | None = None,
) -> None:
    """PDF 1건 처리 직후 글자 좌표 업로드와 krei.report 1행 upsert를 바로 끝낸다."""
    probs = check_rows("krei.report", [rep])
    if probs:
        r.problems += probs
        return
    t_path = rep.get("text_path")
    if rep.get("fetch_status") == "fetched" and t_path and gz_bytes is not None:
        try:
            writer.upload(STORAGE_BUCKET, t_path, gz_bytes, content_type="application/gzip")
        except Exception as e:
            err = f"Storage 올리기 실패: {type(e).__name__}: {str(e)[:200]}"
            rep["fetch_status"] = "failed"
            rep["fetch_error"] = err
            rep["text_path"] = None
            r.problems.append(f"#{rep['file_id']} {rep['item_name']}: {err}")
            r.failed_reports.append(
                dict(
                    file_id=rep["file_id"],
                    item_name=rep["item_name"],
                    report_type=rep["report_type"],
                    error=err,
                )
            )
            r.fetched = [f for f in r.fetched if f.get("file_id") != rep["file_id"]]
    try:
        writer.upsert("report", [rep], on_conflict=REPORT_KEY, schema=KREI_SCHEMA)
        r.uploaded = True
    except Exception as e:
        r.problems.append(f"#{rep['file_id']} DB 올리기 실패: {type(e).__name__}: {str(e)[:200]}")


def run(
    months: list[tuple[int, int]] | None = None,
    existing_items: Iterable[Mapping[str, Any]] = (),
    existing_reports: Mapping[int, Mapping[str, Any]] | None = None,
    http: KreiHttp = http_get,
    clock: Clock | None = None,
    pdf_extractor: PdfExtractor = extract_pdf_bytes,
    *,
    write: bool = True,
    writer: Any | None = None,
    budget_minutes: float | None = DEFAULT_BUDGET_MINUTES,
    max_pdfs: int | None = None,
    out_dir: Path | None = None,
) -> KreiResult:
    """KREI 목록·원문 수집 1회 실행.
    - `write=False`(시험 실행)이면 목록은 모두 조회하되 PDF 는 최대 1건(표본)만 받는다.
    - `writer`가 주어지면 PDF 1건을 처리할 때마다 즉시 Storage·DB에 반영한다.
    - `budget_minutes`(기본 25분)가 지나면 새 PDF 를 시작하지 않고 남은 건수를 기록해 정상 종료한다.
    - `out_dir`이 주어지면 목록 조회 직후와 PDF 1건 처리마다 요약 파일을 갱신한다."""
    clock = clock or Clock()
    started_at = clock.now()
    budget_s = budget_minutes * 60.0 if budget_minutes is not None else None
    effective_max_pdfs = max_pdfs if max_pdfs is not None else (1 if not write else None)

    target_months = months if months is not None else default_months(started_at.date())
    existing_by_id = dict(existing_reports or {})
    r = KreiResult(months=list(target_months))
    client = KreiClient(http=http, clock=clock, log=r.log)
    now_iso = _iso(started_at) or ""

    # 1. reportItem 조회 및 동기화
    ok_item, item_xml, item_err = client.fetch_report_items()
    if not ok_item:
        r.problems.append(f"품목분류(reportItem) 조회 실패: {item_err}")
        parsed_items: list[dict[str, Any]] = []
    else:
        try:
            parsed_items = parse_report_items(item_xml)
        except ValueError as e:
            r.problems.append(str(e))
            parsed_items = []
    r.items = sync_items(parsed_items, existing_items, now_iso)

    # 2. 월별 montlyReport · newsReport 조회
    listed_by_id: dict[int, dict[str, Any]] = {}
    for yyyy, mm in target_months:
        ym = f"{yyyy:04d}-{mm:02d}"
        for api, fetcher in (("monthly", client.fetch_monthly_list), ("news", client.fetch_news_list)):
            ok_list, xml_bytes, list_err = fetcher(yyyy, mm)
            if not ok_list:
                r.problems.append(f"{ym} {api} 목록 조회 실패: {list_err}")
                continue
            r.list_files[list_storage_path(api, yyyy, mm)] = xml_bytes
            try:
                rows, bad_links = parse_reports(xml_bytes, api)
            except ValueError as e:
                r.problems.append(f"{ym} {api}: {e}")
                continue
            r.list_counts[f"{ym} {api}"] = len(rows)
            for msg in bad_links:
                r.problems.append(msg)
                r.failed_reports.append({"file_id": None, "item_name": ym, "report_type": api, "error": msg})
            for row in rows:
                listed_by_id[int(row["file_id"])] = row

    r.listed_count = len(listed_by_id)
    _ensure_report_items_exist(r.items, listed_by_id.values(), now_iso)

    if writer is not None:
        r._persisted_incrementally = True
        if not _persist_items_and_lists(r, writer):
            if out_dir is not None:
                write_outputs(r, out_dir)
            return r

    # 3. 각 발행물 재수집 여부 판정
    todo_items: list[tuple[int, dict[str, Any]]] = []
    for file_id, item in sorted(listed_by_id.items()):
        prev = existing_by_id.get(file_id)
        first_seen = (prev.get("first_seen_at") if prev else None) or now_iso
        updated_at = _iso(clock.now()) or now_iso
        if not needs_refetch(item, prev):
            assert prev is not None
            r.skipped_count += 1
            r.reports.append(
                dict(
                    file_id=file_id,
                    api=item["api"],
                    report_type=item["report_type"],
                    yyyy=item["yyyy"],
                    mm=item["mm"],
                    ovr_item_code=item["ovr_item_code"],
                    prt_ovr_item_code=item.get("prt_ovr_item_code"),
                    item_name=item["item_name"],
                    pdf_link=item["pdf_link"],
                    reg_at=item.get("reg_at") or prev.get("reg_at"),
                    mod_at=item.get("mod_at") or prev.get("mod_at"),
                    description=item.get("description") or prev.get("description"),
                    first_seen_at=first_seen,
                    pdf_sha256=prev.get("pdf_sha256"),
                    pdf_bytes=prev.get("pdf_bytes"),
                    pages=prev.get("pages"),
                    issue_no=prev.get("issue_no"),
                    pub_date=prev.get("pub_date"),
                    text_path=prev.get("text_path"),
                    fetch_status="fetched",
                    fetch_error=None,
                    parse_status=prev.get("parse_status") or "pending",
                    updated_at=updated_at,
                )
            )
        else:
            todo_items.append((file_id, item))

    if out_dir is not None:
        write_outputs(r, out_dir)

    # 4. PDF 내려받기 → 글자 좌표 추출 → 1건씩 즉시 저장 및 요약 파일 갱신
    for idx, (file_id, item) in enumerate(todo_items):
        if effective_max_pdfs is not None and idx >= effective_max_pdfs:
            r.sample_skipped_count = len(todo_items) - idx
            break
        elapsed_s = (clock.now() - started_at).total_seconds()
        if budget_s is not None and elapsed_s >= budget_s:
            r.remaining_count = len(todo_items) - idx
            break

        prev = existing_by_id.get(file_id)
        first_seen = (prev.get("first_seen_at") if prev else None) or now_iso
        ok_pdf, pdf_bytes, pdf_err = client.fetch_pdf(item["pdf_link"], file_id)
        updated_at = _iso(clock.now()) or now_iso
        if not ok_pdf:
            err_msg = f"PDF 다운로드 실패: {pdf_err}"
            r.problems.append(f"#{file_id} {item['item_name']}({item['report_type']}): {err_msg}")
            r.failed_reports.append(
                dict(
                    file_id=file_id,
                    item_name=item["item_name"],
                    report_type=item["report_type"],
                    error=err_msg,
                )
            )
            rep_row = dict(
                file_id=file_id,
                api=item["api"],
                report_type=item["report_type"],
                yyyy=item["yyyy"],
                mm=item["mm"],
                ovr_item_code=item["ovr_item_code"],
                prt_ovr_item_code=item.get("prt_ovr_item_code"),
                item_name=item["item_name"],
                pdf_link=item["pdf_link"],
                reg_at=item.get("reg_at"),
                mod_at=item.get("mod_at"),
                description=item.get("description"),
                first_seen_at=first_seen,
                pdf_sha256=None,
                pdf_bytes=None,
                pages=None,
                issue_no=None,
                pub_date=None,
                text_path=None,
                fetch_status="failed",
                fetch_error=err_msg[:500],
                parse_status="pending",
                updated_at=updated_at,
            )
            r.reports.append(rep_row)
            if writer is not None:
                _persist_one_report(r, writer, rep_row)
            if out_dir is not None:
                write_outputs(r, out_dir)
            continue

        try:
            ext = pdf_extractor(pdf_bytes)
        except Exception as e:
            err_msg = f"PDF 글자 추출 실패: {type(e).__name__}: {e}"
            r.problems.append(f"#{file_id} {item['item_name']}({item['report_type']}): {err_msg}")
            r.failed_reports.append(
                dict(
                    file_id=file_id,
                    item_name=item["item_name"],
                    report_type=item["report_type"],
                    error=err_msg,
                )
            )
            rep_row = dict(
                file_id=file_id,
                api=item["api"],
                report_type=item["report_type"],
                yyyy=item["yyyy"],
                mm=item["mm"],
                ovr_item_code=item["ovr_item_code"],
                prt_ovr_item_code=item.get("prt_ovr_item_code"),
                item_name=item["item_name"],
                pdf_link=item["pdf_link"],
                reg_at=item.get("reg_at"),
                mod_at=item.get("mod_at"),
                description=item.get("description"),
                first_seen_at=first_seen,
                pdf_sha256=None,
                pdf_bytes=len(pdf_bytes),
                pages=None,
                issue_no=None,
                pub_date=None,
                text_path=None,
                fetch_status="failed",
                fetch_error=err_msg[:500],
                parse_status="pending",
                updated_at=updated_at,
            )
            r.reports.append(rep_row)
            if writer is not None:
                _persist_one_report(r, writer, rep_row)
            if out_dir is not None:
                write_outputs(r, out_dir)
            continue

        t_path = text_storage_path(item["ovr_item_code"], item["yyyy"], item["mm"], file_id)
        gz_bytes = ext["gz_bytes"]
        r.text_files[t_path] = gz_bytes
        r.fetched.append(
            dict(
                file_id=file_id,
                yyyy=item["yyyy"],
                mm=item["mm"],
                ovr_item_code=item["ovr_item_code"],
                item_name=item["item_name"],
                report_type=item["report_type"],
                pages=ext["pages"],
                pdf_bytes=ext["pdf_bytes"],
                gz_bytes=len(gz_bytes),
                issue_no=ext["issue_no"],
                pub_date=ext["pub_date"],
            )
        )
        rep_row = dict(
            file_id=file_id,
            api=item["api"],
            report_type=item["report_type"],
            yyyy=item["yyyy"],
            mm=item["mm"],
            ovr_item_code=item["ovr_item_code"],
            prt_ovr_item_code=item.get("prt_ovr_item_code"),
            item_name=item["item_name"],
            pdf_link=item["pdf_link"],
            reg_at=item.get("reg_at"),
            mod_at=item.get("mod_at"),
            description=item.get("description"),
            first_seen_at=first_seen,
            pdf_sha256=ext["pdf_sha256"],
            pdf_bytes=ext["pdf_bytes"],
            pages=ext["pages"],
            issue_no=ext["issue_no"],
            pub_date=ext["pub_date"],
            text_path=t_path,
            fetch_status="fetched",
            fetch_error=None,
            parse_status="pending",
            updated_at=updated_at,
        )
        r.reports.append(rep_row)
        if writer is not None:
            _persist_one_report(r, writer, rep_row, gz_bytes=gz_bytes)
        if out_dir is not None:
            write_outputs(r, out_dir)

    if out_dir is not None:
        write_outputs(r, out_dir)
    return r


def check(r: KreiResult) -> list[str]:
    return check_rows("krei.item", r.items) + check_rows("krei.report", r.reports)


def publish(r: KreiResult, writer: Any) -> KreiResult:
    """계약 검사 통과 후 Storage(krei-raw)와 DB(krei.item, krei.report)에 반영한다.
    `run(..., writer=writer)`로 이미 한 건씩 저장한 경우에는 중복 업로드를 건너뛴다."""
    if r._persisted_incrementally:
        return r
    probs = check(r)
    if probs:
        r.problems += probs
        return r
    for path, xml_bytes in sorted(r.list_files.items()):
        try:
            writer.upload(STORAGE_BUCKET, path, xml_bytes, content_type="application/xml")
        except Exception as e:
            r.problems.append(f"목록 XML Storage 올리기 실패 ({path}): {type(e).__name__}: {str(e)[:200]}")

    for rep in r.reports:
        t_path = rep.get("text_path")
        if not t_path or t_path not in r.text_files:
            continue
        try:
            writer.upload(STORAGE_BUCKET, t_path, r.text_files[t_path], content_type="application/gzip")
        except Exception as e:
            err = f"Storage 올리기 실패: {type(e).__name__}: {str(e)[:200]}"
            rep["fetch_status"] = "failed"
            rep["fetch_error"] = err
            rep["text_path"] = None
            r.problems.append(f"#{rep['file_id']} {rep['item_name']}: {err}")
            r.failed_reports.append(
                dict(
                    file_id=rep["file_id"],
                    item_name=rep["item_name"],
                    report_type=rep["report_type"],
                    error=err,
                )
            )

    try:
        if r.items:
            writer.upsert("item", r.items, on_conflict=ITEM_KEY, schema=KREI_SCHEMA)
        if r.reports:
            writer.upsert("report", r.reports, on_conflict=REPORT_KEY, schema=KREI_SCHEMA)
        r.uploaded = True
    except Exception as e:
        r.problems.append(f"DB 올리기 실패: {type(e).__name__}: {str(e)[:200]}")
    return r


def brief_text(r: KreiResult) -> str:
    """실행 요약: 조회한 달, 목록 건수, 새로 받은 건(부류·종류·파일번호·쪽수·크기), 남은 건, 실패 건."""
    ym_str = ", ".join(f"{y:04d}-{m:02d}" for y, m in r.months)
    total_listed = r.listed_count if r.listed_count else len(r.reports)
    lines = [
        f"# KREI 농업관측 수집 ({ym_str}, 계산 {CALC_VERSION})",
        f"- 조회한 달: {ym_str}",
        f"- 품목분류: {len(r.items)}건 · 목록: {total_listed}건 "
        f"(새로 받음 {len(r.fetched)}건 · 기존 건너뜀 {r.skipped_count}건 · 실패 {len(r.failed_reports)}건)",
    ]
    if r.remaining_count > 0:
        lines.append(f"- 남은 {r.remaining_count}건, 다음 실행에서 이어 받음")
    if r.sample_skipped_count > 0:
        lines.append(f"- 시험 실행(write=false): PDF 표본 1건만 받고 나머지 {r.sample_skipped_count}건 생략")
    if r.list_counts:
        counts_str = ", ".join(f"{k}: {v}건" for k, v in sorted(r.list_counts.items()))
        lines.append(f"- 달별 응답: {counts_str}")
    if r.fetched:
        lines.append("\n## 새로 받은 건")
        for f in r.fetched:
            kb = f["gz_bytes"] / 1024
            mb = f["pdf_bytes"] / 1e6
            meta = ""
            if f.get("issue_no") or f.get("pub_date"):
                meta = f" (통권 {f.get('issue_no') or '-'}호, 발행 {f.get('pub_date') or '-'})"
            lines.append(
                f"- {f['yyyy']:04d}-{f['mm']:02d} {f['item_name']} · {f['report_type']} · "
                f"#{f['file_id']} · {f['pages']}쪽 · PDF {mb:.2f}MB → 좌표 {kb:.1f}KB{meta}"
            )
    if r.failed_reports:
        lines.append("\n## 실패 건")
        for fl in r.failed_reports:
            fid = f"#{fl['file_id']}" if fl.get("file_id") is not None else "번호없음"
            lines.append(f"- {fl['item_name']} ({fl['report_type']}) {fid}: {fl['error']}")
    if r.problems:
        lines.append("\n## 문제")
        for p in r.problems[:10]:
            lines.append(f"- ! {p}"[:300])
    lines.append(
        f"\nDB 올리기: {'완료' if r.uploaded else '안 함'} · "
        f"item {len(r.items)}줄 · report {len(r.reports)}줄"
    )
    return "\n".join(lines) + "\n"


summary_text = brief_text


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def write_outputs(r: KreiResult, out_dir: Path) -> Path:
    tag = "_".join(f"{y:04d}{m:02d}" for y, m in r.months[:2]) or "run"
    d = out_dir / f"krei_{tag}"
    d.mkdir(parents=True, exist_ok=True)
    for name, data in (
        ("items", r.items),
        ("reports", r.reports),
        ("fetched", r.fetched),
        ("failed", r.failed_reports),
        ("problems", r.problems),
    ):
        _atomic_write_text(d / f"{name}.json", json.dumps(data, ensure_ascii=False, indent=1))
    _atomic_write_text(
        d / "collect_log.jsonl",
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in r.log.entries),
    )
    text = brief_text(r)
    _atomic_write_text(d / "brief.md", text)
    _atomic_write_text(d / "summary.md", text)
    return d


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="KREI 농업관측 수집기 (품목·목록·글자 좌표)")
    p.add_argument("--yyyy", help="조회할 연도 4자리 (--mm 과 함께)")
    p.add_argument("--mm", help="조회할 월 (--yyyy 와 함께)")
    p.add_argument("--from", dest="from_ym", help="조회 시작 연-월 (예: 2026-01)")
    p.add_argument("--to", dest="to_ym", help="조회 끝 연-월 (예: 2026-11)")
    p.add_argument(
        "--budget-minutes",
        type=float,
        default=None,
        help="시간 예산(분, 기본 25분 또는 환경변수 KREI_BUDGET_MINUTES)",
    )
    p.add_argument("--out", default="out", help="결과 폴더")
    p.add_argument("--write", action="store_true", help="계약 검사를 통과하면 Supabase DB·Storage에 쓰기")
    a = p.parse_args(argv)

    clock = Clock()
    months = resolve_months(clock, yyyy=a.yyyy, mm=a.mm, from_ym=a.from_ym, to_ym=a.to_ym)
    budget_minutes = resolve_budget_minutes(a.budget_minutes)

    existing_items: list[dict[str, Any]] = []
    existing_reports: dict[int, dict[str, Any]] = {}
    sb_url = os.environ.get("SUPABASE_URL", "").strip()
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if a.write:
        sb_url = _env("SUPABASE_URL")
        sb_key = _env("SUPABASE_SERVICE_ROLE_KEY")
    if sb_url and sb_key:
        try:
            existing_items, existing_reports = read_existing(sb_url, sb_key)
        except Exception as e:
            if a.write:
                raise
            print(f"참고: 기존 DB 조회 생략 ({e})")

    writer = None
    if a.write:
        from croplib_dp.io.supabase_write import SupabaseWriter

        writer = SupabaseWriter(sb_url, sb_key)

    out_path = Path(a.out)
    r = run(
        months=months,
        existing_items=existing_items,
        existing_reports=existing_reports,
        clock=clock,
        write=a.write,
        writer=writer,
        budget_minutes=budget_minutes,
        out_dir=out_path,
    )
    if not a.write:
        r.problems += check(r)
    out = write_outputs(r, out_path)
    print(brief_text(r))
    print(f"결과 폴더: {out}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
