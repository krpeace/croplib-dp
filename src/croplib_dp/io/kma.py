"""기상청 공공 API 조회 (공공데이터포털): 단기예보(동네예보), ASOS 일자료.

원본: cropwater `kma_fcst.py`의 HTTP 부분 (feat/cycle02-forecast).
  - 키는 환경변수(DATA_GO_KR)에서 받아 URL에만 넣는다. 기록·오류 문구에는 mask_key로 감춘 주소만 남긴다.
  - 응답은 JSON. 인증 오류는 XML로 오기도 해서 XML에서도 코드·메시지를 찾는다.
  - 결과 구분: ok / nodata(아직 자료 없음 → 다시) / retry(일시 오류 → 다시) / fatal(다시 해도 소용없음 → 멈춤)
HTTP 함수(http)는 바꿔 끼울 수 있어서, 테스트는 가짜 응답으로 돈다.
"""

import json
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

import pandas as pd

VILAGE_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
ASOS_URL = "http://apis.data.go.kr/1360000/AsosDalyInfoService/getWthrDataList"
NUM_ROWS = 1000  # 단기예보 한 쪽 행 수 (발표당 약 800~1,100행 → 1~2쪽)
ASOS_ROWS = 400  # ASOS 한 쪽 행 수 (작기 한 해치가 한 쪽에 들어감)
TIMEOUT_S = 30
FATAL_CODES = {"10", "11", "12", "20", "21", "30", "31", "32", "33"}  # 요청·인증 오류
NODATA_CODES = {"03"}  # 자료 없음 (발표 직후, 어제 관측이 아직 안 나옴)

Http = Callable[[str, float], tuple[int, bytes]]


def http_get(url: str, timeout: float = TIMEOUT_S) -> tuple[int, bytes]:
    import requests

    r = requests.get(url, timeout=timeout)
    return r.status_code, r.content


def service_key(key: str) -> str:
    """공공데이터포털 키: 인코딩 키(% 포함)는 그대로, 디코딩 키는 URL 인코딩해서 쓴다"""
    key = (key or "").strip()
    return key if "%" in key else urllib.parse.quote(key, safe="")


def vilage_url(key: str, issue: Any, nx: int, ny: int, page: int = 1, rows: int = NUM_ROWS) -> str:
    issue = pd.Timestamp(issue)
    return (
        f"{VILAGE_URL}?serviceKey={service_key(key)}&pageNo={page}&numOfRows={rows}&dataType=JSON"
        f"&base_date={issue:%Y%m%d}&base_time={issue:%H}00&nx={int(nx)}&ny={int(ny)}"
    )


def asos_url(key: str, stn: str, start: Any, end: Any, page: int = 1, rows: int = ASOS_ROWS) -> str:
    return (
        f"{ASOS_URL}?serviceKey={service_key(key)}&pageNo={page}&numOfRows={rows}&dataType=JSON"
        f"&dataCd=ASOS&dateCd=DAY&startDt={pd.Timestamp(start):%Y%m%d}&endDt={pd.Timestamp(end):%Y%m%d}"
        f"&stnIds={stn}"
    )


def mask_key(text: str) -> str:
    """주소·오류 문구에서 키를 감춘다"""
    return re.sub(r"serviceKey=[^&\s'\"]*", "serviceKey=***", str(text))


def parse_response(body: bytes | str) -> dict:
    """응답 본문 → dict(code, msg, items, total). JSON이 아니면 XML에서 코드·메시지를 찾는다"""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes | bytearray) else str(body)
    try:
        js = json.loads(text)
        resp = js.get("response", js)
        hdr = resp.get("header", {}) or {}
        b = resp.get("body") or {}
        items = b.get("items") or {}
        items = items.get("item", []) if isinstance(items, dict) else []
        items = items if isinstance(items, list) else [items]
        return dict(
            code=str(hdr.get("resultCode", "?")),
            msg=str(hdr.get("resultMsg", "")),
            items=items,
            total=int(b.get("totalCount", len(items)) or 0),
        )
    except (ValueError, AttributeError):
        code = re.search(r"<(?:resultCode|returnReasonCode)>\s*([^<\s]+)\s*<", text)
        msg = re.search(r"<(?:resultMsg|returnAuthMsg|errMsg)>\s*([^<]+?)\s*<", text)
        return dict(
            code=code.group(1) if code else "?",
            msg=(msg.group(1) if msg else text[:120].replace("\n", " ")),
            items=[],
            total=0,
        )


def classify(http: int | None, code: Any) -> str:
    """HTTP 상태·응답 코드 → 'ok' / 'nodata' / 'retry' / 'fatal'"""
    if http in (401, 403):
        return "fatal"
    if http is None or http >= 500 or http != 200:
        return "retry"
    code = str(code).zfill(2) if str(code).isdigit() else str(code)
    if code == "00":
        return "ok"
    if code in NODATA_CODES:
        return "nodata"
    if code in FATAL_CODES:
        return "fatal"
    return "retry"


def fetch_pages(
    url_of_page: Callable[[int], str], http: Http = http_get, timeout: float = TIMEOUT_S, rows: int = NUM_ROWS
):
    """쪽을 이어 받는다.
    반환 dict(status, http, code, msg, items, total, bodies[(page, bytes)], error, elapsed_s, url)"""
    t0 = time.monotonic()
    out = dict(status="retry", http=None, code="", msg="", items=[], total=0, bodies=[], error="", url="")
    page = 1
    while True:
        url = url_of_page(page)
        out["url"] = mask_key(url)
        try:
            status, body = http(url, timeout)
        except Exception as e:  # 시간 초과·연결 오류 (문구에 주소가 들어갈 수 있어 키를 감춘다)
            out.update(status="retry", error=mask_key(f"{type(e).__name__}: {str(e)[:160]}"))
            break
        out["http"] = status
        if status != 200:
            out.update(status=classify(status, ""), error=f"HTTP {status}")
            break
        out["bodies"].append((page, body))
        p = parse_response(body)
        out.update(code=p["code"], msg=mask_key(p["msg"]))
        st = classify(200, p["code"])
        if st != "ok":
            out["status"] = st
            break
        out["items"] += p["items"]
        out["total"] = p["total"]
        if p["total"] == 0:
            out["status"] = "nodata"
            break
        if len(out["items"]) >= p["total"] or not p["items"] or page * rows >= p["total"]:
            out["status"] = "ok"
            break
        page += 1
    out["elapsed_s"] = round(time.monotonic() - t0, 2)
    return out


def fetch_vilage(
    key: str, issue: Any, nx: int, ny: int, http: Http = http_get, timeout: float = TIMEOUT_S
) -> dict:
    """단기예보 한 발표(issue: 발표시각) → fetch_pages 결과"""
    return fetch_pages(lambda p: vilage_url(key, issue, nx, ny, p), http, timeout, NUM_ROWS)


def fetch_asos_days(
    key: str, stn: str, start: Any, end: Any, http: Http = http_get, timeout: float = TIMEOUT_S
):
    """ASOS 일자료 start~end → fetch_pages 결과(items = 날짜별 dict)"""
    return fetch_pages(lambda p: asos_url(key, stn, start, end, p), http, timeout, ASOS_ROWS)


def obs_row_complete(item: dict) -> bool:
    """ASOS 한 날의 ETo 필수 입력(최고·최저기온, 풍속, 습도류, 일사)이 있는지"""

    def g(k):
        return str(item.get(k, "") or "").strip() not in ("", "-", "None", "nan")

    return bool(
        g("maxTa") and g("minTa") and g("avgWs") and (g("avgPv") or g("avgTd") or g("avgRhm")) and g("sumGsr")
    )
