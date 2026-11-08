"""DB 용량 경고 (core/monitor.py, collectors/monitor.py, io/db_stats.py). 이슈 #61(#28 S3-b).

규칙(승인 2026-11-08): DB 경고 400MB / 오류 450MB. 정확한 크기(CropMon#77 db_size_bytes())를 읽기 전에는 줄 수 × 줄당 실측 크기로
추정하고 360MB(오류 405MB)부터 알린다. 크기를 못 읽으면 "조회 실패"로 알린다(조용히 넘어가지 않음). 입력은 가짜 값이다.
"""

# ruff: noqa: E501

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent / "data"))

from retention_fakes import FakeSupabase  # noqa: E402

from croplib_dp.collectors import monitor as M  # noqa: E402
from croplib_dp.core import retention as R  # noqa: E402
from croplib_dp.core.monitor import check_health, report  # noqa: E402

NOW = pd.Timestamp("2026-11-08 14:00")
RUN = [
    dict(
        run_at="2026-11-08 02:00",
        stn_id="101",
        result="정시 성공",
        finished_at="2026-11-08 02:05",
        on_time=True,
    )
]


def _alerts(**kw):
    return [
        a for a in check_health(NOW, RUN, {"101": NOW.date()}, {"101": "춘천"}, **kw) if a.code == "dbsize"
    ]


# ── 규칙 (순수 함수) ────────────────────────────────────────────────────
def test_no_db_size_given_means_no_alert_so_existing_callers_are_unchanged():
    assert _alerts() == []


def test_exact_size_below_400_is_quiet_400_warns_450_errors():
    assert _alerts(db_size=dict(mb=399.0, exact=True)) == []
    (w,) = _alerts(db_size=dict(mb=412.4, exact=True))
    assert w.level == "warn" and "412" in w.text and "정확" in w.text and "500MB" in w.text
    (e,) = _alerts(db_size=dict(mb=455.0, exact=True))
    assert e.level == "error" and "455" in e.text


def test_estimated_size_warns_from_360_and_errors_from_405_and_says_it_is_an_estimate():
    assert _alerts(db_size=dict(mb=359.0, exact=False)) == []
    (w,) = _alerts(db_size=dict(mb=361.0, exact=False))
    assert w.level == "warn" and "추정" in w.text and "CropMon#77" in w.text
    (e,) = _alerts(db_size=dict(mb=406.0, exact=False))
    assert e.level == "error"


def test_alert_key_is_stable_while_the_number_changes_so_comments_do_not_pile_up():
    a = _alerts(db_size=dict(mb=410.0, exact=True))[0]
    b = _alerts(db_size=dict(mb=412.0, exact=True))[0]
    assert a.key == b.key
    assert a.key != _alerts(db_size=dict(mb=455.0, exact=True))[0].key  # 오류로 올라가면 새 경고


def test_failure_to_read_the_size_is_a_visible_warning_not_silence():
    (a,) = _alerts(db_size_error="HTTP 500 boom")
    assert a.level == "warn" and "조회 실패" in a.text and "HTTP 500 boom" in a.text


def test_alert_appears_in_the_report_text():
    alerts = check_health(NOW, RUN, {"101": NOW.date()}, {"101": "춘천"}, db_size=dict(mb=420.0, exact=True))
    assert "DB 용량" in report(alerts, NOW)


# ── 크기 읽기: RPC 가 있으면 정확, 없으면 줄 수로 추정 ─────────────────────
BASE, KEY = "https://x.supabase.co", "SERVICE-KEY"


def _read(f):
    return M.read_db_size(BASE, KEY, post=f.request, head=f.head)


def test_exact_size_from_the_rpc_when_the_function_exists():
    f = FakeSupabase()
    f.rpc_result = {"db_bytes": 420 * 1024 * 1024, "tables": {"heat_daily": 1}}
    s = _read(f)
    assert s["exact"] is True and s["mb"] == pytest.approx(420.0) and s["source"] == "rpc"
    assert f.head_calls == []  # 정확한 값이 있으면 줄 수를 세지 않는다


def test_falls_back_to_the_estimate_when_the_function_is_missing_404():
    f = FakeSupabase()
    f.counts = {"heat_daily": 114_975, "soil_water_daily": 37_200, "sun_daily": 26_280, "water_balance_daily": 10_140,
                "water_outlook": 5_640, "weather_daily": 3_187, "water_outlook_summary": 1_405, "forecast_runs": 282}  # fmt: skip
    s = _read(f)
    assert s["exact"] is False and s["source"] == "estimate" and s["mb"] == pytest.approx(57.0, abs=1.0)
    assert sorted(f.head_calls) == sorted(R.COUNT_TABLES)  # 표 8개만, 한 번씩


@pytest.mark.parametrize("status", [401, 403])
def test_falls_back_when_the_function_is_not_allowed_for_this_key(status):
    f = FakeSupabase()
    f.rpc_result = status
    assert _read(f)["exact"] is False


def test_other_rpc_errors_are_not_hidden_behind_the_estimate():
    f = FakeSupabase()
    f.rpc_result = 500
    with pytest.raises(RuntimeError, match="500"):
        _read(f)


def test_a_bad_rpc_answer_is_an_error_not_a_zero():
    f = FakeSupabase()
    f.rpc_result = {"unexpected": 1}
    with pytest.raises(RuntimeError, match="db_bytes"):
        _read(f)


def test_state_has_db_size_or_a_visible_error():
    f = FakeSupabase()
    f.rpc_result = {"db_bytes": 100 * 1024 * 1024}
    ok = M.db_size_state(BASE, KEY, post=f.request, head=f.head)
    assert ok["db_size"]["mb"] == pytest.approx(100.0) and "db_size_error" not in ok
    f.rpc_result = 500
    bad = M.db_size_state(BASE, KEY, post=f.request, head=f.head)
    assert "db_size" not in bad and "500" in bad["db_size_error"]


def test_run_monitor_passes_the_size_to_the_rules(tmp_path):
    state = dict(
        runs=RUN, names={"101": "춘천"}, weather_last={"101": NOW.date()}, heat_last={"101": NOW.date()},
        db_size=dict(mb=430.0, exact=True),
    )  # fmt: skip
    text, action = M.run_monitor(state, None, now=NOW)
    assert "DB 용량" in text and action == "dry"
    state.pop("db_size")
    assert "DB 용량" not in M.run_monitor(state, None, now=NOW)[0]


def test_monitor_main_adds_the_size_check_after_the_existing_state(monkeypatch):
    calls = []
    monkeypatch.setenv("SUPABASE_URL", BASE)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(
        M, "read_state",
        lambda *a, **k: dict(runs=RUN, names={"101": "춘천"}, weather_last={"101": NOW.date()}, heat_last={"101": NOW.date()}),
    )  # fmt: skip
    monkeypatch.setattr(
        M, "db_size_state", lambda *a, **k: (calls.append(1), dict(db_size=dict(mb=500.0, exact=True)))[1]
    )

    class Sink:
        def sync(self, keys, text):
            calls.append(keys)
            return "opened"

    monkeypatch.setattr(M, "IssueAlerts", lambda *a, **k: Sink())
    assert M.main([]) == 0
    assert calls[0] == 1 and any("dbsize" in k for k in calls[1])
