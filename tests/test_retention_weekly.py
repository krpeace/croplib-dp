"""retention 주 1회 정기 실행 (collectors/retention.py weekly, retention.yml). 이슈 #61(#28 S3-b).

규칙(계획 보강 2026-11-08): 기본은 읽기만(plan) + 경고. 자동 삭제는 `--auto`(워크플로에서는 Actions 변수 RETENTION_AUTO_APPLY=true)
일 때만, S3-a 와 같은 확인을 모두 거치고 한 번에 지우는 상한(--max-rows)을 넘으면 멈춘다. Storage 용량은 모든 버킷을 합쳐
800MB 경고 / 950MB 오류. 입력은 모두 가짜 값이다.
"""

# ruff: noqa: E501

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent / "data"))

from retention_fakes import FakeIssues, FakeSupabase  # noqa: E402

from croplib_dp.collectors import retention as RT  # noqa: E402
from croplib_dp.core import retention as R  # noqa: E402
from croplib_dp.io.github_issue import IssueAlerts  # noqa: E402
from test_retention_collector import NOW, OUT_PATH, _env, _fake  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MB = 1024 * 1024


def _sink(gh):
    return IssueAlerts(
        "o/r", "tok", http=gh, title=RT.ALERT_TITLE, label=RT.ALERT_LABEL, label_desc="용량·보관 알림"
    )


def _keys(res):
    return sorted(a.key for a in res.alerts)


# ── 기본: 읽기만 + 보관 대기 알림 ─────────────────────────────────────
def test_weekly_by_default_only_reads_and_reminds_that_archiving_is_waiting():
    f, gh = _fake(), FakeIssues()
    res = RT.weekly(_env(f), NOW, auto=False, sink=_sink(gh))
    assert f.writes == [] and res.applied is None and not res.failed  # 쓰기·삭제 0
    assert _keys(res) == ["warn:retention:보관 대기"]
    assert "16줄" in res.alerts[0].text and "RETENTION_AUTO_APPLY" in res.alerts[0].text
    assert (
        res.action == "opened"
        and gh.issues[0]["title"] == RT.ALERT_TITLE
        and RT.ALERT_LABEL in gh.issues[0]["labels"]
    )
    assert len(f.tables["water_outlook"]) == 18  # 아무것도 안 지웠다


def test_weekly_reminder_is_one_issue_not_a_comment_every_week():
    f, gh = _fake(), FakeIssues()
    RT.weekly(_env(f), NOW, auto=False, sink=_sink(gh))
    res = RT.weekly(_env(f), NOW + pd.Timedelta(days=7), auto=False, sink=_sink(gh))
    assert res.action in ("unchanged", "commented") and len(gh.issues) == 1 and gh.comments == []


def test_nothing_to_do_and_small_storage_is_quiet():
    f, gh = FakeSupabase(), FakeIssues()
    res = RT.weekly(_env(f), NOW, auto=False, sink=_sink(gh))
    assert res.alerts == [] and res.action == "none" and gh.issues == []
    assert "이상 없음" in res.text


# ── 자동 삭제 (--auto) ────────────────────────────────────────────────
def test_auto_apply_runs_the_same_verified_deletion_and_clears_the_reminder():
    f, gh = _fake(), FakeIssues()
    res = RT.weekly(_env(f), NOW, auto=True, max_rows=8000, sink=_sink(gh))
    assert res.applied is not None and sum(res.applied.deleted.values()) == 16
    assert [r["run_id"] for r in f.tables["water_outlook"]] == [4, 4, 4, 5, 5, 5]
    assert len(f.tables["forecast_runs"]) == 6  # forecast_runs 는 그대로
    assert res.alerts == [] and not res.failed
    assert ("raw", OUT_PATH) in f.storage  # 보관 파일이 올라갔다
    assert "16" in res.text


def test_auto_apply_refuses_when_more_rows_than_the_cap_are_waiting():
    f, gh = _fake(), FakeIssues()
    res = RT.weekly(_env(f), NOW, auto=True, max_rows=10, sink=_sink(gh))
    assert res.applied is None and f.writes == []
    assert _keys(res) == ["error:retention:자동 삭제 상한 초과"] and res.failed
    assert "16줄" in res.alerts[0].text and "10줄" in res.alerts[0].text


def test_auto_apply_failure_stops_reports_an_error_and_never_raises():
    f, gh = _fake(), FakeIssues()
    f.tamper_on_download = {R.archive_path("water_outlook", "2026-09")}  # 첫 달 확인에서 어긋남
    res = RT.weekly(_env(f), NOW, auto=True, sink=_sink(gh))
    assert res.failed and _keys(res) == ["error:retention:보관·삭제 중단"]
    assert "내려받" in res.alerts[0].text
    assert len(f.tables["water_outlook"]) == 18  # 지우지 않았다
    assert gh.issues and gh.issues[0]["state"] == "open"


def test_unexpected_errors_are_reported_not_swallowed():
    f, gh = _fake(), FakeIssues()

    def broken(url, headers):
        return 500, b"server error"

    env = RT.Env("https://x.supabase.co", "K", f.request, broken, f.head)
    res = RT.weekly(env, NOW, auto=False, sink=_sink(gh))
    assert res.failed and _keys(res) == ["error:retention:보관·삭제 중단"] and "500" in res.alerts[0].text


def test_a_recovered_week_closes_the_issue():
    f, gh = _fake(), FakeIssues()
    RT.weekly(_env(f), NOW, auto=False, sink=_sink(gh))  # 보관 대기 → 이슈 열림
    assert gh.issues[0]["state"] == "open"
    res = RT.weekly(_env(f), NOW, auto=True, sink=_sink(gh))  # 자동 삭제로 해소
    assert res.action == "closed" and gh.issues[0]["state"] == "closed"


# ── Storage 용량 (모든 버킷 합계) ──────────────────────────────────────
def _big(f, mb, bucket="raw", path="fcst/73_134/2026/10/big.json.gz"):
    f.storage[(bucket, path)] = b"x"
    if bucket == "raw":
        f.size_override[path] = int(mb * MB)
    else:  # 다른 버킷도 크기는 같은 방식(경로 이름으로 구분)
        f.size_override[path] = int(mb * MB)


def test_storage_warns_at_800mb_and_errors_at_950mb_with_all_buckets_counted():
    f, gh = FakeSupabase(), FakeIssues()
    _big(f, 500)
    f.storage[("krei-raw", "text/OVR1/2026/11/1.pages.json.gz")] = b"y"
    f.size_override["text/OVR1/2026/11/1.pages.json.gz"] = 310 * MB  # 두 버킷 합 810MB
    res = RT.weekly(_env(f), NOW, auto=False, sink=_sink(gh))
    assert _keys(res) == ["warn:storage:Storage 용량 경고선 초과"] and res.storage_mb == pytest.approx(
        810.0, abs=0.1
    )
    assert "800" in res.alerts[0].text and "950" in res.alerts[0].text
    f.size_override["text/OVR1/2026/11/1.pages.json.gz"] = 460 * MB  # 960MB
    res = RT.weekly(_env(f), NOW, auto=False, sink=_sink(gh))
    assert _keys(res) == ["error:storage:Storage 용량 오류선 초과"] and res.failed


def test_storage_below_800_gives_no_storage_alert_and_the_number_is_in_the_report():
    f, gh = FakeSupabase(), FakeIssues()
    _big(f, 799)
    res = RT.weekly(_env(f), NOW, auto=False, sink=_sink(gh))
    assert res.alerts == [] and "799" in res.text


def test_storage_alert_key_is_stable_while_size_changes():
    f, gh = FakeSupabase(), FakeIssues()
    _big(f, 820)
    a = RT.weekly(_env(f), NOW, auto=False, sink=_sink(gh))
    f.size_override["fcst/73_134/2026/10/big.json.gz"] = int(830 * MB)
    b = RT.weekly(_env(f), NOW, auto=False, sink=_sink(gh))
    assert _keys(a) == _keys(b) and b.action == "unchanged"


def test_storage_listing_failure_is_a_visible_error():
    f, gh = FakeSupabase(), FakeIssues()
    orig = f.request

    def hook(method, url, headers, body):
        if url.endswith("/storage/v1/bucket"):
            return 500, b"nope"
        return orig(method, url, headers, body)

    env = RT.Env("https://x.supabase.co", "K", hook, f.rest_get, f.head)
    res = RT.weekly(env, NOW, auto=False, sink=_sink(gh))
    assert res.failed and any(a.code == "retention" for a in res.alerts)


# ── CLI ────────────────────────────────────────────────────────────────
def test_cli_weekly_writes_reports_and_returns_zero_without_issue(tmp_path):
    f = _fake()
    code = RT.main(["weekly", "--no-issue", "--out", str(tmp_path)], env=_env(f), now=NOW)
    assert code == 0 and f.writes == []
    assert (tmp_path / "retention" / "weekly.md").exists()
    data = json.loads((tmp_path / "retention" / "weekly.json").read_text(encoding="utf-8"))
    assert data["auto"] is False and data["rows_waiting"] == 16 and data["failed"] is False


def test_cli_weekly_auto_returns_one_when_the_run_failed(tmp_path):
    f = _fake()
    f.tamper_on_download = {R.archive_path("water_outlook", "2026-09")}
    assert RT.main(["weekly", "--auto", "--no-issue", "--out", str(tmp_path)], env=_env(f), now=NOW) == 1


def test_cli_weekly_auto_with_a_small_cap_returns_one(tmp_path):
    f = _fake()
    assert (
        RT.main(
            ["weekly", "--auto", "--max-rows", "5", "--no-issue", "--out", str(tmp_path)],
            env=_env(f),
            now=NOW,
        )
        == 1
    )
    assert f.writes == []


def test_cli_weekly_default_cap_is_8000_rows():
    assert RT.DEFAULT_MAX_ROWS == 8000


# ── 워크플로 설정 (글자 검사) ──────────────────────────────────────────
def test_workflow_has_a_weekly_schedule_and_auto_delete_is_gated_by_an_actions_variable():
    y = (ROOT / ".github" / "workflows" / "retention.yml").read_text(encoding="utf-8")
    assert "schedule:" in y and 'cron: "30 18 * * 0"' in y  # 월요일 03:30 KST
    assert "vars.RETENTION_AUTO_APPLY" in y and "--auto" in y
    assert "issues: write" in y and "GITHUB_TOKEN" in y
    assert "inputs.mode || 'weekly'" in y  # 예약 실행은 weekly(읽기만 + 경고, 자동 삭제는 변수가 켜졌을 때만)


def test_the_scheduled_run_never_applies_without_the_variable():
    y = (ROOT / ".github" / "workflows" / "retention.yml").read_text(encoding="utf-8")
    assert "args+=(--auto)" in y
    block = y[y.index("AUTO_APPLY: ${{ vars.RETENTION_AUTO_APPLY }}") :]
    assert '"$AUTO_APPLY" = "true"' in block


def test_keepalive_re_enables_the_retention_schedule_too():
    y = (ROOT / ".github" / "workflows" / "keepalive.yml").read_text(encoding="utf-8")
    assert "retention.yml" in y
