"""GitHub 이슈로 알리기 (공3). 저장소 주인에게는 이슈가 열리거나 댓글이 달리면 GitHub가 메일을 보낸다.

  - 경고가 생기면 "수집 경고" 이슈를 연다(이미 열려 있으면 그 이슈를 쓴다)
  - 새 경고가 생길 때만 댓글을 단다(같은 경고로 메일이 쌓이지 않게)
  - 경고가 모두 사라지면 "정상 회복" 댓글을 달고 닫는다
GitHub Actions 기본 토큰(GITHUB_TOKEN, issues: write)을 쓴다.
"""

import json
import re
import urllib.parse
from collections.abc import Callable

Request = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]
LABEL = "수집 경고"
TITLE = "[수집 경고] 물 수집"
MARK = re.compile(r"<!-- keys: (.*?) -->")


def http_request(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    import requests

    r = requests.request(method, url, headers=headers, data=body, timeout=30)
    return r.status_code, r.content


class IssueAlerts:
    def __init__(
        self, repo: str, token: str, http: Request = http_request, api: str = "https://api.github.com"
    ):
        self.repo, self.token, self.http, self.api = repo, token, http, api.rstrip("/")

    def _call(self, method: str, path: str, data: dict | None = None, ok=(200, 201)) -> object:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        status, out = self.http(method, f"{self.api}/repos/{self.repo}{path}", headers, body)
        if status not in ok:
            text = out.decode("utf-8", "replace")[:200] if isinstance(out, bytes | bytearray) else str(out)
            raise RuntimeError(f"GitHub {method} {path} 실패: HTTP {status} {text}")
        return json.loads(out) if out else None

    def ensure_label(self) -> None:
        try:
            self._call("GET", f"/labels/{urllib.parse.quote(LABEL)}")
        except RuntimeError:
            self._call(
                "POST", "/labels", {"name": LABEL, "color": "d93f0b", "description": "수집 감시 자동 알림"}
            )

    def find_open(self) -> dict | None:
        issues = self._call("GET", f"/issues?state=open&labels={urllib.parse.quote(LABEL)}&per_page=10")
        issues = [i for i in issues if "pull_request" not in i]
        return issues[0] if issues else None

    @staticmethod
    def _keys(body: str | None) -> set[str]:
        m = MARK.search(body or "")
        return set(filter(None, m.group(1).split("|"))) if m else set()

    def sync(self, keys: list[str], text: str) -> str:
        """경고 열쇠 목록과 보고 글 → 이슈 열기·댓글·닫기.
        반환: opened / commented / unchanged / closed / none"""
        issue = self.find_open()
        body = f"{text}\n\n<!-- keys: {'|'.join(sorted(keys))} -->"
        if not keys:
            if issue is None:
                return "none"
            self._call("POST", f"/issues/{issue['number']}/comments", {"body": f"✅ 정상 회복\n\n{text}"})
            self._call("PATCH", f"/issues/{issue['number']}", {"state": "closed", "body": body})
            return "closed"
        if issue is None:
            self.ensure_label()
            self._call("POST", "/issues", {"title": TITLE, "body": body, "labels": [LABEL]})
            return "opened"
        new = set(keys) - self._keys(issue.get("body"))
        self._call("PATCH", f"/issues/{issue['number']}", {"body": body})
        if new:
            self._call("POST", f"/issues/{issue['number']}/comments", {"body": text})
            return "commented"
        return "unchanged"
