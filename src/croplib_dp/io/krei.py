"""KREI 농업관측 API 조회 및 PDF 글자 좌표 추출 (io 계층).

- 요청 예절: 요청 사이 1초 쉼, User-Agent `croplib-dp/<버전>`, 타임아웃 60/120초, 실패 시 5초 뒤 1회 재시도.
- PDF 원본은 임시 폴더에만 받고, 글자 좌표를 뽑은 뒤 바로 지운다 (Storage 1GB 한도 보호).
"""

import hashlib
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

from croplib_dp.core import CALC_VERSION
from croplib_dp.core.krei import (
    build_pages_payload,
    compress_pages_json,
    extract_first_page_meta,
    resolve_pdf_url,
)

BASE_URL = "https://aglook.krei.re.kr/main/api"
USER_AGENT = f"croplib-dp/{CALC_VERSION}"
LIST_TIMEOUT_S = 60
PDF_TIMEOUT_S = 120
REQUEST_DELAY_S = 1.0
RETRY_DELAY_S = 5.0
STORAGE_BUCKET = "krei-raw"

# (url, timeout, headers) -> (status_code, body, error_msg)
KreiHttp = Callable[[str, int, dict[str, str]], tuple[int | None, bytes, str | None]]


def http_get(
    url: str, timeout: int = LIST_TIMEOUT_S, headers: dict[str, str] | None = None
) -> tuple[int | None, bytes, str | None]:
    import requests

    req_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    try:
        r = requests.get(url, headers=req_headers, timeout=timeout)
        err = None if r.status_code == 200 else f"HTTP {r.status_code}"
        return r.status_code, r.content, err
    except Exception as e:
        return None, b"", f"{type(e).__name__}: {e}"


def extract_pdf_bytes(pdf_bytes: bytes) -> dict[str, Any]:
    """PDF 바이트를 임시 폴더에 저장해 pdfplumber 로 쪽별 글자 좌표와 1쪽 통권·발행일을 뽑는다.
    임시 파일은 함수가 끝나면 자동으로 지워진다."""
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("PDF 파일(%PDF 시작)이 아닙니다")
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    size = len(pdf_bytes)
    with tempfile.TemporaryDirectory(prefix="krei_pdf_") as tmp_dir:
        pdf_path = Path(tmp_dir) / "report.pdf"
        pdf_path.write_bytes(pdf_bytes)
        import pdfplumber

        raw_pages: list[dict[str, Any]] = []
        first_page_texts: list[str] = []
        with pdfplumber.open(pdf_path) as pdf:
            for idx, page in enumerate(pdf.pages):
                words = page.extract_words() or []
                raw_pages.append({"w": page.width, "h": page.height, "words": words})
                if idx == 0:
                    extracted = page.extract_text() or ""
                    if extracted:
                        first_page_texts.append(extracted)
                    if words:
                        first_page_texts.append(" ".join(str(w.get("text", "")) for w in words))
    pages_data = build_pages_payload(raw_pages)
    gz_bytes = compress_pages_json(pages_data)
    issue_no, pub_date = extract_first_page_meta("\n".join(first_page_texts))
    return dict(
        pdf_sha256=sha256,
        pdf_bytes=size,
        pages=len(pages_data),
        issue_no=issue_no,
        pub_date=pub_date,
        gz_bytes=gz_bytes,
        pages_data=pages_data,
    )


class KreiClient:
    """KREI API 요청기: 요청 사이 1초 쉼, 실패 시 5초 뒤 1회 재시도."""

    def __init__(
        self,
        http: KreiHttp = http_get,
        clock: Any = None,
        log: Any = None,
        base_url: str = BASE_URL,
    ):
        self.http = http
        self.clock = clock
        self.log = log
        self.base_url = base_url.rstrip("/")
        self._has_requested = False

    def _sleep(self, sec: float) -> None:
        if self.clock is not None:
            self.clock.sleep(sec)
        elif sec > 0:
            time.sleep(sec)

    def _request(
        self,
        kind: str,
        target: str,
        url: str,
        timeout: int,
        validator: Callable[[bytes], str | None],
    ) -> tuple[bool, bytes, str | None]:
        headers = {"User-Agent": USER_AGENT}
        last_err: str | None = None
        last_body = b""
        for attempt in (1, 2):
            if attempt == 1:
                if self._has_requested:
                    self._sleep(REQUEST_DELAY_S)
                self._has_requested = True
            else:
                self._sleep(RETRY_DELAY_S)
            t0 = time.time()
            status, body, err = self.http(url, timeout, headers)
            elapsed = round(time.time() - t0, 2)
            v_err = None if (err is None and status == 200) else (err or f"HTTP {status}")
            if v_err is None:
                v_err = validator(body)
            ok = v_err is None
            if self.log is not None and self.clock is not None:
                self.log.add(
                    self.clock,
                    kind,
                    target,
                    attempt,
                    {"http": status, "elapsed_s": elapsed, "error": v_err or ""},
                    "ok" if ok else ("retry" if attempt == 1 else "failed"),
                )
            if ok:
                return True, body, None
            last_err = v_err
            last_body = body
        return False, last_body, last_err

    @staticmethod
    def _validate_xml(body: bytes) -> str | None:
        if not body:
            return "빈 응답"
        try:
            ET.fromstring(body)
            return None
        except ET.ParseError as e:
            return f"XML 파싱 오류: {e}"

    @staticmethod
    def _validate_pdf(body: bytes) -> str | None:
        if not body or not body.startswith(b"%PDF"):
            return "PDF 시그니처(%PDF) 아님"
        return None

    def fetch_report_items(self) -> tuple[bool, bytes, str | None]:
        url = f"{self.base_url}/reportItem"
        return self._request("krei_item", "reportItem", url, LIST_TIMEOUT_S, self._validate_xml)

    def fetch_monthly_list(self, yyyy: int, mm: int) -> tuple[bool, bytes, str | None]:
        q = urllib.parse.urlencode({"yyyy": f"{yyyy:04d}", "mm": f"{mm:02d}"})
        url = f"{self.base_url}/montlyReport?{q}"
        return self._request("krei_monthly", f"{yyyy:04d}-{mm:02d}", url, LIST_TIMEOUT_S, self._validate_xml)

    def fetch_news_list(self, yyyy: int, mm: int) -> tuple[bool, bytes, str | None]:
        q = urllib.parse.urlencode({"yyyy": f"{yyyy:04d}", "mm": f"{mm:02d}"})
        url = f"{self.base_url}/newsReport?{q}"
        return self._request("krei_news", f"{yyyy:04d}-{mm:02d}", url, LIST_TIMEOUT_S, self._validate_xml)

    def fetch_pdf(self, pdf_link: str, file_id: int) -> tuple[bool, bytes, str | None]:
        url = resolve_pdf_url(pdf_link, self.base_url)
        return self._request("krei_pdf", f"#{file_id}", url, PDF_TIMEOUT_S, self._validate_pdf)
