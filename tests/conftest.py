"""시험 공통 도우미.

보관 확인의 재시도 대기(`collectors.retention.SLEEP`)를 시험에서는 기다리지 않고 기록만 한다(이슈 #117).
`sleeps` 도구를 받으면 대기한 초 목록을 볼 수 있다. 자동으로 적용되므로 기존 시험도 실제로 기다리지 않는다.
"""

import pytest


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    from croplib_dp.collectors import retention as RT

    waited: list[float] = []
    monkeypatch.setattr(RT, "SLEEP", waited.append, raising=False)
    return waited
