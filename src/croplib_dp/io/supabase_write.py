"""Supabase 쓰기: 표 덮어쓰기(upsert)와 Storage 올리기.

서버 키(SUPABASE_SERVICE_ROLE_KEY)로 쓴다. 이 키는 RLS를 건너뛰므로 이 저장소의 수집기에서만 쓴다.
  - 표: PostgREST `POST /rest/v1/<표>?on_conflict=<키>` + `Prefer: resolution=merge-duplicates`
    → 같은 키의 줄은 새 값으로 바뀐다(같은 회차를 다시 돌려도 줄이 늘지 않음). 500줄씩 나눠 보낸다.
  - Storage: `POST /storage/v1/object/<버킷>/<경로>` + `x-upsert: true`
실패하면 오류를 낸다(조용히 넘어가지 않는다). 오류 문구에 키는 들어가지 않는다.
"""

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

Request = Callable[
    [str, str, dict, bytes], tuple[int, bytes]
]  # (method, url, headers, body) → (status, body)
BATCH = 500


def http_request(method: str, url: str, headers: dict, body: bytes) -> tuple[int, bytes]:
    import requests

    r = requests.request(method, url, headers=headers, data=body, timeout=60)
    return r.status_code, r.content


class SupabaseWriter:
    def __init__(self, base_url: str, key: str, http: Request = http_request, batch: int = BATCH):
        self.base = base_url.rstrip("/")
        self.key, self.http, self.batch = key, http, batch
        self.calls = 0

    def _headers(self, **extra: str) -> dict:
        return {"apikey": self.key, "Authorization": f"Bearer {self.key}", **extra}

    def upsert(
        self,
        table: str,
        rows: Iterable[Mapping[str, Any]],
        on_conflict: tuple[str, ...],
        returning: bool = False,
    ) -> list[dict]:
        """줄 목록을 덮어쓰기. returning=True면 저장된 줄(자동 번호 포함)을 돌려준다"""
        rows = list(rows)
        out: list[dict] = []
        url = f"{self.base}/rest/v1/{table}?on_conflict={','.join(on_conflict)}"
        prefer = "resolution=merge-duplicates," + ("return=representation" if returning else "return=minimal")
        headers = self._headers(**{"Content-Type": "application/json", "Prefer": prefer})
        for i in range(0, len(rows), self.batch):
            part = rows[i : i + self.batch]
            status, body = self.http(
                "POST", url, headers, json.dumps(part, ensure_ascii=False).encode("utf-8")
            )
            self.calls += 1
            if status not in (200, 201, 204):
                text = (
                    body.decode("utf-8", "replace")[:300]
                    if isinstance(body, bytes | bytearray)
                    else str(body)
                )
                raise RuntimeError(
                    f"Supabase {table} 쓰기 실패 ({i + 1}~{i + len(part)}번째 줄): HTTP {status} {text}"
                )
            if returning and body:
                out += json.loads(body)
        return out

    def upload(self, bucket: str, path: str, data: bytes, content_type: str = "application/gzip") -> None:
        url = f"{self.base}/storage/v1/object/{bucket}/{path}"
        headers = self._headers(**{"Content-Type": content_type, "x-upsert": "true"})
        status, body = self.http("POST", url, headers, data)
        self.calls += 1
        if status not in (200, 201):
            text = body.decode("utf-8", "replace")[:300] if isinstance(body, bytes | bytearray) else str(body)
            raise RuntimeError(f"Storage {bucket}/{path} 올리기 실패: HTTP {status} {text}")
