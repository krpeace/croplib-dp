"""Supabase Storage 입출력: 목록(재귀, 파일 크기 포함)·내려받기·올리기·지우기 (이슈 #61, #28 S3-a).

서버 키(SUPABASE_SERVICE_ROLE_KEY)로 쓴다. 이 저장소의 수집기에서만 쓴다. 계산은 하지 않는다.
실패하면 오류를 낸다(조용히 넘어가지 않는다). 오류 문구에 키는 들어가지 않는다.
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import json
from collections.abc import Callable
from typing import Any

from croplib_dp.io.supabase_write import http_request

Request = Callable[[str, str, dict, bytes], tuple[int, bytes]]
LIST_PAGE = 1000
DELETE_BATCH = 100


def _text(body: Any) -> str:
    return body.decode("utf-8", "replace")[:300] if isinstance(body, bytes | bytearray) else str(body)[:300]


class Storage:
    def __init__(self, base_url: str, key: str, http: Request = http_request):
        self.base, self.key, self.http = base_url.rstrip("/"), key, http
        self.calls = 0

    def _headers(self, **extra: str) -> dict:
        return {"apikey": self.key, "Authorization": f"Bearer {self.key}", **extra}

    def _folder(self, bucket: str, prefix: str) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            body = json.dumps(
                {
                    "prefix": prefix,
                    "limit": LIST_PAGE,
                    "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"},
                }
            ).encode()
            status, resp = self.http(
                "POST",
                f"{self.base}/storage/v1/object/list/{bucket}",
                self._headers(**{"Content-Type": "application/json"}),
                body,
            )
            self.calls += 1
            if status != 200:
                raise RuntimeError(f"Storage {bucket}/{prefix} 목록 조회 실패: HTTP {status} {_text(resp)}")
            page = json.loads(resp)
            out += page
            if len(page) < LIST_PAGE:
                return out
            offset += LIST_PAGE

    def list_files(self, bucket: str, prefix: str = "") -> list[dict]:
        """버킷 안(prefix 아래) 모든 파일: [{path, size}]. 폴더는 따라 내려가고 파일만 돌려준다."""
        files: list[dict] = []
        stack = [prefix.strip("/")]
        while stack:
            folder = stack.pop()
            for it in self._folder(bucket, folder):
                path = f"{folder}/{it['name']}" if folder else it["name"]
                if it.get("id") is None:  # 폴더
                    stack.append(path)
                else:
                    size = (it.get("metadata") or {}).get("size")
                    files.append({"path": path, "size": int(size) if size is not None else 0})
        return sorted(files, key=lambda f: f["path"])

    def download(self, bucket: str, path: str) -> bytes | None:
        """파일 내용. 없으면 None(없음과 오류를 구분한다)."""
        status, body = self.http(
            "GET", f"{self.base}/storage/v1/object/{bucket}/{path}", self._headers(), b""
        )
        self.calls += 1
        if status == 200:
            return bytes(body)
        if status == 404 or (status == 400 and b"not_found" in bytes(body)):
            return None
        raise RuntimeError(f"Storage {bucket}/{path} 내려받기 실패: HTTP {status} {_text(body)}")

    def upload(self, bucket: str, path: str, data: bytes, content_type: str = "application/gzip") -> None:
        headers = self._headers(**{"Content-Type": content_type, "x-upsert": "true"})
        status, body = self.http("POST", f"{self.base}/storage/v1/object/{bucket}/{path}", headers, data)
        self.calls += 1
        if status not in (200, 201):
            raise RuntimeError(f"Storage {bucket}/{path} 올리기 실패: HTTP {status} {_text(body)}")

    def delete(self, bucket: str, paths: list[str]) -> int:
        """파일 지우기(100개씩). 지운 개수를 돌려준다. 요청한 개수와 다르면 오류."""
        deleted = 0
        for i in range(0, len(paths), DELETE_BATCH):
            part = paths[i : i + DELETE_BATCH]
            status, body = self.http(
                "DELETE",
                f"{self.base}/storage/v1/object/{bucket}",
                self._headers(**{"Content-Type": "application/json"}),
                json.dumps({"prefixes": part}).encode(),
            )
            self.calls += 1
            if status != 200:
                raise RuntimeError(f"Storage {bucket} 지우기 실패: HTTP {status} {_text(body)}")
            n = len(json.loads(body))
            if n != len(part):
                raise RuntimeError(f"Storage {bucket}: {len(part)}개를 지우라고 했는데 {n}개가 지워졌습니다")
            deleted += n
        return deleted
