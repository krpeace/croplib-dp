"""DB 읽기 도우미: 줄 수 세기·여러 쪽 이어 읽기 (이슈 #61, #28 S3-a). 읽기만 한다, 계산은 하지 않는다.

PostgREST 는 한 번에 최대 1,000줄만 돌려주므로(Supabase 기본) `limit`·`offset`으로 이어 읽는다.
줄 수는 `Prefer: count=exact` 와 `Range` 로 한 줄만 받고 `Content-Range` 의 전체 개수를 읽는다.
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import json
from collections.abc import Callable

from croplib_dp.io.dictionaries import RestGet, read_table, rest_get
from croplib_dp.io.supabase_write import Request, http_request

HeadRequest = Callable[
    [str, str, dict, bytes], tuple[int, dict, bytes]
]  # (method, url, headers, body) → (status, headers, body)
PAGE = 1000


def http_request_h(method: str, url: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
    import requests

    r = requests.request(method, url, headers=headers, data=body, timeout=60)
    return r.status_code, dict(r.headers), r.content


def row_count(base: str, key: str, table: str, query: str = "", http: HeadRequest = http_request_h) -> int:
    """표(조건에 맞는 줄)의 개수. 응답을 못 읽으면 오류를 낸다(조용히 0으로 보지 않는다)."""
    url = f"{base.rstrip('/')}/rest/v1/{table}?select=*" + (f"&{query}" if query else "")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Prefer": "count=exact",
        "Range-Unit": "items",
        "Range": "0-0",
    }
    status, resp_headers, body = http("GET", url, headers, b"")
    if status not in (200, 206):
        raise RuntimeError(f"{table} 줄 수 조회 실패: HTTP {status} {body[:200]!r}")
    rng = {k.lower(): v for k, v in resp_headers.items()}.get("content-range", "")
    total = rng.rsplit("/", 1)[-1]
    if not total.isdigit():
        raise RuntimeError(f"{table} 줄 수를 읽지 못했습니다 (Content-Range: {rng!r})")
    return int(total)


def read_all(
    base: str, key: str, table: str, query: str, order: str, http: RestGet = rest_get, page: int = PAGE
) -> list[dict]:
    """조건에 맞는 모든 줄(정렬 필수 — 이어 읽기가 흔들리지 않게). 한 쪽씩 읽어 이어 붙인다."""
    out: list[dict] = []
    offset = 0
    while True:
        part = read_table(
            base, key, table, http, f"{query}&order={order}&limit={page}&offset={offset}".lstrip("&")
        )
        out += part
        if len(part) < page:
            return out
        offset += page


def rpc_db_size(base: str, key: str, post: Request = http_request) -> dict | None:
    """CropMon 함수 `public.db_size_bytes()`(CropMon#77)로 정확한 DB 크기를 읽는다. 돌려주는 값: {db_bytes, tables}.
    함수가 없거나(404) 이 키로 부를 수 없으면(401·403) None(→ 줄 수로 추정). 그 밖 오류와 엉뚱한 응답은 오류로 올린다."""
    url = f"{base.rstrip('/')}/rest/v1/rpc/db_size_bytes"
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    status, body = post("POST", url, headers, b"{}")
    if status in (401, 403, 404):
        return None
    if status != 200:
        raise RuntimeError(f"db_size_bytes() 호출 실패: HTTP {status} {body[:200]!r}")
    out = json.loads(body)
    if not isinstance(out, dict) or "db_bytes" not in out:
        raise RuntimeError(f"db_size_bytes() 응답에 db_bytes 가 없습니다: {str(out)[:200]}")
    return out
