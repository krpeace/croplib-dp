"""Supabase에서 내려받은 응답 바이트 합계 (egress 실측, 결정 0031).

Supabase 무료 플랜은 주기(10/23~11/23)마다 egress 5GB 한도가 있다. 이 저장소가 DB에서 읽는 양을
실행마다 한 줄로 남겨, 켜기 전 추정을 실측으로 바꾼다.
**저장 크기가 아니라 응답 본문(JSON) 바이트**다. 쓰기 요청의 응답도 센다
(`forecast_runs` 올리기는 `return=representation`으로 줄을 돌려받는다). GitHub·기상청 응답은 세지 않는다.
실행(프로세스)마다 0에서 시작하는 합계이고, 요청 주소의 경로로 표(또는 rpc·storage)를 구분한다.
"""

from collections import defaultdict
from urllib.parse import urlparse

_BYTES: dict[str, int] = defaultdict(int)
_CALLS: dict[str, int] = defaultdict(int)


def label(url: str) -> str:
    """요청 주소 → 표 이름(`rest/v1/<표>`), `rpc:<함수>`, `storage`, 그 밖은 `기타`."""
    path = urlparse(url).path
    if "/rest/v1/rpc/" in path:
        return "rpc:" + path.split("/rest/v1/rpc/")[1].split("/")[0]
    if "/rest/v1/" in path:
        return path.split("/rest/v1/")[1].split("/")[0] or "기타"
    if "/storage/v1/" in path:
        return "storage"
    return "기타"


def record(url: str, body: bytes | None) -> None:
    """응답 하나를 센다. 본문이 없어도(HEAD 등) 요청 수는 센다."""
    key = label(url)
    _BYTES[key] += len(body or b"")
    _CALLS[key] += 1


def reset() -> None:
    _BYTES.clear()
    _CALLS.clear()


def total_bytes() -> int:
    return sum(_BYTES.values())


def total_calls() -> int:
    return sum(_CALLS.values())


def by_label() -> dict[str, tuple[int, int]]:
    """표별 (바이트, 요청 수)"""
    return {k: (_BYTES[k], _CALLS[k]) for k in _CALLS}


def summary_line(top: int = 6) -> str:
    """로그에 남기는 한 줄:
    `DB 읽기(이 실행): 0.14MB (요청 31번) — forecast_runs 0.09MB · scenarios 0.04MB …`"""
    head = f"DB 읽기(이 실행): {total_bytes() / 1024 / 1024:.2f}MB (요청 {total_calls()}번)"
    rows = sorted(by_label().items(), key=lambda kv: -kv[1][0])[:top]
    if not rows:
        return head
    return head + " — " + " · ".join(f"{k} {b / 1024 / 1024:.2f}MB" for k, (b, _) in rows)
