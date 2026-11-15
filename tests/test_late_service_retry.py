"""지연 재실행에서도 서비스 발표 받기를 30초·90초 간격으로 재시도하고, 회차 전체 재시도 시간에 상한을 둔다
(이슈 #107, 구현 #118, 결정 0036). 입력은 모두 가짜 값이다.

배경: 2026-11-12 08:28 늦은 예약 실행에서 서귀포(189)의 서비스 발표 받기가 시간 초과 한 번으로 `백업: 직전 발표`가 됐다.
마감(발표 + 90분) 뒤에 도는 지연 재실행은 서비스 발표를 한 번만 시도했기 때문이다.
규칙(2026-11-15 승인): 마감 뒤에 도는 지점의 서비스 발표 첫 시도가 일시 오류(`retry`)면 30초·90초 뒤 두 번 더 받는다.
시작 전에 `쓴 시간 + 대기 > 300초`면 그 재시도는 하지 않고 기록한다. 이 상한은 **새 재시도만** 센다(기존 필요한 앞선 발표·ASOS
재시도는 값·동작 모두 그대로). 정시 회차(마감 안)의 동작은 바뀌지 않는다.
"""

# ruff: noqa: E501

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from late_retry_fakes import TimeoutApi  # noqa: E402

from croplib_dp.collectors import water as W  # noqa: E402
from test_collector import ISSUE, ISSUES, KEY, FakeClock, _dicts  # noqa: E402
from test_s4a import _dicts_with_nonrep  # noqa: E402

TS = pd.Timestamp
SVC = "202606100200"  # 02시 회차의 서비스 발표(6/10 02:00)
LATE = "2026-06-10 14:00"  # 마감(03:30)을 한참 넘긴 지연 재실행
ONTIME = "2026-06-10 02:10"  # 정시 회차(발표 + 10분)


def _go(start, fail, stations=("101",), d=None, wait=True, elapsed=0.0):
    clock = FakeClock(start)
    api = TimeoutApi(clock, ISSUES, fail=fail, elapsed=elapsed)
    r = W.run_slot(
        "아침", ISSUE, d or _dicts(), KEY, http=api, clock=clock, stations=list(stations), wait=wait
    )
    return r, api, clock


def _asked(api, key):
    return sum(1 for u in api.calls if f"base_date={key[:8]}&base_time={key[8:]}" in u)


def _run_of(r, stn="101"):
    return next(x for x in r.forecast_runs if x["stn_id"] == stn)


def _line(text):
    return next(x for x in text.splitlines() if x.startswith("단계:"))


# ── 1. 지연 재실행, 서비스 발표 첫 시도 시간 초과 → 30초 뒤 성공 ─────────────────────────────
def test_late_run_retries_a_timed_out_service_issue_after_30_seconds_and_it_is_not_a_backup():
    r, api, clock = _go(LATE, {SVC: 1})
    x = _run_of(r)
    assert x["result"] == "지연 수집" and not x["on_time"] and x["attempts"] == 2
    assert clock.now() - TS(LATE) == pd.Timedelta(seconds=30)
    assert _asked(api, SVC) == 2 and not r.failed
    assert r.retry_budget.retries == 1 and r.retry_budget.spent_s == 30 and r.retry_budget.skipped == []


# ── 2. 세 번 다 시간 초과 → 앞선 발표로 백업(지금과 같은 결과) ────────────────────────────────
def test_late_run_gives_up_after_three_timeouts_and_uses_the_previous_issue():
    r, api, clock = _go(LATE, {SVC: 99})
    x = _run_of(r)
    assert x["result"] == "백업: 직전 발표" and x["attempts"] == 3 and x["error"] is None
    assert clock.now() - TS(LATE) == pd.Timedelta(seconds=120)  # 30초 + 90초
    assert _asked(api, SVC) == 3 and not r.failed and r.water_outlook
    assert r.retry_budget.retries == 2


def test_late_run_with_a_normal_first_attempt_asks_once_and_never_waits():
    r, api, clock = _go(LATE, {})
    x = _run_of(r)
    assert x["result"] == "지연 수집" and x["attempts"] == 1
    assert clock.now() == TS(LATE) and _asked(api, SVC) == 1
    assert r.retry_budget.retries == 0 and r.retry_budget.spent_s == 0


# ── 3. 합계 상한을 넘으면 이후 지점은 재시도 없이 진행하고 요약에 표시 ──────────────────────────
def test_the_budget_stops_further_retries_and_the_summary_says_which_stations(monkeypatch):
    monkeypatch.setattr(W, "RETRY_BUDGET_S", 60)
    r, api, clock = _go(
        LATE, {SVC: 999}, stations=("101", "301"), d=_dicts_with_nonrep(), elapsed=34.0
    )  # 시간 초과 한 번 = 34초
    a, b = _run_of(r, "101"), _run_of(r, "301")
    # 101: 첫 재시도(대기 30 + 요청 34 = 64초)는 하고, 두 번째(90초 대기)는 64 + 90 > 60 이라 안 함
    assert a["attempts"] == 2 and a["result"] == "백업: 직전 발표"
    # 301: 이미 64초를 썼으니 첫 재시도(30초 대기)부터 안 함
    assert b["attempts"] == 1 and b["result"].startswith(
        "백업"
    )  # (대표 밖은 S3 백업으로 나오기도 한다: 기존 분류)
    assert r.retry_budget.skipped == ["101", "301"] and r.retry_budget.retries == 1
    assert not r.failed  # 실행 실패가 아니다
    text = W.summary_text(r)
    assert "지연 재시도 상한(60초) 도달" in text and "101" in text and "301" in text


# ── 4. 정시 회차 동작은 그대로 (구현 전 코드에서 뽑은 값) ───────────────────────────────────────
def test_on_time_slot_is_unchanged_when_the_service_issue_times_out_once():
    r, api, clock = _go(ONTIME, {SVC: 1})
    x = _run_of(r)
    assert x["result"] == "정시 성공" and x["on_time"] and x["attempts"] == 2
    assert clock.now() == TS("2026-06-10 02:15:00") and _asked(api, SVC) == 2 and len(api.calls) == 11
    assert r.retry_budget.retries == 0 and r.retry_budget.spent_s == 0  # 새 재시도 규칙은 안 쓰임


def test_on_time_slot_is_unchanged_when_the_service_issue_never_arrives():
    r, api, clock = _go(ONTIME, {SVC: 99})
    x = _run_of(r)
    assert x["result"] == "백업: 직전 발표" and not x["on_time"] and x["attempts"] == 9
    assert clock.now() == TS("2026-06-10 03:30:00") and _asked(api, SVC) == 9 and len(api.calls) == 18
    assert r.retry_budget.retries == 0


def test_a_trial_run_without_waiting_still_asks_once_even_after_the_deadline():
    r, api, clock = _go(LATE, {SVC: 1}, wait=False)
    x = _run_of(r)
    assert x["result"] == "백업: 직전 발표" and x["attempts"] == 1
    assert clock.now() == TS(LATE) and r.retry_budget.retries == 0


# ── 5. 기존 재시도는 값·동작 그대로이고 새 상한에 세지 않는다 ────────────────────────────────────
def test_existing_retries_keep_their_values_and_do_not_use_the_new_budget():
    assert W.NEEDED_ISSUE_RETRY_S == (30, 90) and W.ASOS_RETRY_S == (30, 90, 180)
    assert W.LATE_SERVICE_RETRY_S == (30, 90) and W.RETRY_BUDGET_S == 300
    # 지연 재실행에서 필요한 앞선 발표(어제 02시)가 시간 초과여도 기존 규칙대로 3번 받고, 새 예산은 0
    r, api, clock = _go(LATE, {"202606090200": 99})
    assert _asked(api, "202606090200") == 3
    assert r.retry_budget.retries == 0 and r.retry_budget.spent_s == 0
    assert clock.now() - TS(LATE) == pd.Timedelta(seconds=120)  # 앞선 발표 재시도 대기(30+90)만


def test_setting_the_late_retry_values_to_empty_restores_todays_behavior(monkeypatch):
    monkeypatch.setattr(W, "LATE_SERVICE_RETRY_S", ())
    r, api, clock = _go(LATE, {SVC: 1})
    x = _run_of(r)
    assert x["result"] == "백업: 직전 발표" and x["attempts"] == 1 and clock.now() == TS(LATE)


# ── 6. 요약에 남긴다(조용히 넘기지 않음) ────────────────────────────────────────────────────────
def test_summary_line_reports_the_late_retries_only_when_they_were_used():
    used, _, _ = _go(LATE, {SVC: 1})
    assert "지연 재시도 1번 (대기·요청 30초)" in _line(W.summary_text(used))
    assert any("지연 재시도 1번" in x for x in W.brief_text(used).splitlines() if x.startswith("단계:"))
    for start, fail in [(LATE, {}), (ONTIME, {SVC: 1})]:
        quiet, _, _ = _go(start, fail)
        assert "지연 재시도" not in W.summary_text(quiet) and "지연 재시도" not in W.brief_text(quiet)
