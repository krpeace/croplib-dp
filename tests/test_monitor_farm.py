"""수집 감시의 농장 계산 점검 (core/monitor.py, collectors/monitor.py). 이슈 #131, 결정 0040.

실패한 농장이나 오래 대기 중인 요청을 알린다. 경고에는 곳 수와 가장 오래된 대기 시간만 쓴다(농장 정보 없음).
입력은 모두 가짜 값이다.
"""

import json

import pandas as pd

from croplib_dp.collectors import monitor as M
from croplib_dp.core.monitor import FARM_PENDING_H, check_health

NOW = pd.Timestamp("2026-06-10 14:00")  # 한국 시각
FARM_ID = "11111111-1111-4111-8111-111111111111"


RUN = dict(
    run_at="2026-06-10T02:00:00+09:00",
    stn_id="101",
    result="정시 성공",
    finished_at="2026-06-10T02:20:00+09:00",
)


def farm_alerts(rows=None, error=None):
    out = check_health(NOW, [RUN], {}, {"101": "춘천"}, farm_status=rows, farm_error=error)
    return [a for a in out if a.code == "farm"]


def st(requested, done=None, result="ok"):
    return dict(requested_at=requested, done_at=done, result=result)


def test_no_farm_status_means_no_farm_check_and_empty_rows_are_quiet():
    assert farm_alerts(None) == [] and farm_alerts([]) == []


def test_failed_farms_are_counted_and_named_nowhere():
    got = farm_alerts(
        [st(None, None, "failed"), st(None, None, "failed"), st(None, "2026-06-10T00:00:00+00:00", "ok")]
    )
    assert len(got) == 1 and got[0].level == "warn" and "농장 계산 실패 — 2곳" in got[0].text
    assert FARM_ID not in got[0].text


def test_pending_request_older_than_two_hours_warns_with_the_oldest_wait():
    old = "2026-06-10T00:00:00+00:00"  # 09:00 KST → 5시간 전
    got = farm_alerts([st(old), st("2026-06-10T04:30:00+00:00")])  # 두 번째는 13:30 KST → 30분 전(정상)
    assert len(got) == 1 and "대기 지연 — 1곳이 2시간 넘게" in got[0].text and "5시간" in got[0].text


def test_request_already_done_or_recent_is_not_a_delay():
    old = "2026-06-10T00:00:00+00:00"
    assert farm_alerts([st(old, done="2026-06-10T00:00:00+00:00")]) == []  # requested_at == done_at → 처리됨
    assert farm_alerts([st(old, done="2026-06-10T01:00:00+00:00")]) == []
    assert farm_alerts([st(old, done="2026-06-09T00:00:00+00:00")])  # requested_at > done_at → 아직 대기
    assert farm_alerts([st("2026-06-10T04:30:00+00:00")]) == []  # 30분 전
    assert FARM_PENDING_H == 2 and farm_alerts([st(None)]) == []  # 요청이 없는 농장


def test_alert_keys_do_not_change_with_the_counts_so_no_new_comment_every_hour():
    one = farm_alerts([st(None, None, "failed")])[0].key
    three = farm_alerts([st(None, None, "failed")] * 3)[0].key
    assert one == three == "warn:farm:농장 계산 실패"
    w1 = farm_alerts([st("2026-06-10T00:00:00+00:00")])[0].key
    w2 = farm_alerts([st("2026-06-09T00:00:00+00:00")] * 4)[0].key
    assert w1 == w2 == "warn:farm:농장 계산 대기 지연"


def test_farm_status_read_error_is_a_warning_not_silence():
    got = farm_alerts(None, error="RuntimeError: Supabase farm_calc_status 읽기 실패: HTTP 503\n나머지")
    assert (
        len(got) == 1
        and got[0].text.startswith("농장 계산 상태 조회 실패")
        and "503" in got[0].text
        and "나머지" not in got[0].text
    )


def test_read_farm_status_asks_only_for_candidates_without_farm_ids():
    seen = []

    def fake(url, headers):
        seen.append(url)
        return 200, json.dumps([st(None, None, "failed")]).encode()

    rows = M.read_farm_status("https://x.example", "k", fake, now=NOW)
    assert rows == [st(None, None, "failed")]
    url = seen[0]
    assert (
        "farm_calc_status" in url
        and "select=requested_at,done_at,result" in url
        and "farm_id," not in url.split("order=")[0]
    )
    assert "result.eq.failed" in url and "done_at.is.null" in url and "requested_at.gte." in url


def test_read_state_includes_farm_status_only_when_asked_and_keeps_a_failure_as_an_error_field():
    def fake(url, headers):
        if "farm_calc_status" in url:
            return 503, b"down"
        return 200, b"[]"

    s = M.read_state("https://x.example", "k", fake, now=NOW, include_farm=True)
    assert s["farm_status"] is None and "503" in s["farm_error"]
    assert "farm_status" not in M.read_state("https://x.example", "k", fake, now=NOW)  # 기본은 안 읽는다
    text, action = M.run_monitor(dict(s, runs=[], weather_last={}, names={}), None, now=NOW)
    assert "농장 계산 상태 조회 실패" in text and action == "dry"
