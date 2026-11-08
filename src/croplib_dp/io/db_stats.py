"""DB 읽기 도우미: 줄 수 세기·여러 쪽 이어 읽기 (이슈 #61, #28 S3-a). 읽기만 한다, 계산은 하지 않는다.

PostgREST 는 한 번에 최대 1,000줄만 돌려주므로(Supabase 기본) `limit`·`offset`으로 이어 읽는다.
줄 수는 `Prefer: count=exact` 와 `Range` 로 한 줄만 받고 `Content-Range` 의 전체 개수를 읽는다.
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

from collections.abc import Callable

from croplib_dp.io.dictionaries import RestGet, read_table, rest_get

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
