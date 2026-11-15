"""지연 재실행 재시도 시험용 가짜 API (이슈 #107, #118). 입력은 모두 가짜 값이다.

`tests/test_asos_empty_row.py`의 `FlakyApi`와 같은 방식(지정한 단기예보 발표를 앞 n번 시간 초과로 만든다)에,
시간 초과 한 번이 걸리는 시간(`elapsed`초)을 가짜 시계에 더하는 기능을 얹었다. 합계 상한 시험에서 쓴다.
"""

# ruff: noqa: E501

import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tests/ (test_collector 를 가져오려고)

from test_collector import FakeApi  # noqa: E402


class TimeoutApi(FakeApi):
    """`fail` = {`YYYYMMDDHHMM`(발표): 앞으로 시간 초과로 만들 횟수}. 시간 초과가 날 때마다 시계를 `elapsed`초 보낸다."""

    def __init__(self, clock, issues, *a, fail=None, elapsed=0.0, **k):
        super().__init__(clock, issues, *a, **k)
        self.fail = dict(fail or {})
        self.elapsed = elapsed

    def __call__(self, url, timeout):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if "base_date" in q:
            key = q["base_date"] + q["base_time"]
            if self.fail.get(key, 0) > 0:
                self.fail[key] -= 1
                self.calls.append(url)
                if self.elapsed:
                    self.clock.sleep(self.elapsed)  # 요청이 제한 시간까지 걸린 것을 흉내
                raise TimeoutError("Read timed out. (read timeout=30)")
        return super().__call__(url, timeout)
