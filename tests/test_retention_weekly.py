"""retention 주 1회 정기 실행 (collectors/retention.py weekly, retention.yml). 이슈 #61(#28 S3-b).

규칙(승인 2026-11-08): 일요일 05:10 KST 예약 실행은 **같은 실행 안에서 plan → 그 줄 수로 apply**(기본 켜짐). 비상 정지는 Actions 변수
RETENTION_AUTO_APPLY=false(없으면 켜짐). 확인이 하나라도 어긋나면 지우지 않고 별도 경고 이슈를 열고 실패로 끝난다. 한 번에 지우는 상한
8,000줄(S4 때 함께 올린다). Storage 는 모든 버킷 합계 800MB 경고 / 950MB 오류(한도 1GB 확인됨). 수동 plan/apply/restore 는 그대로.
입력은 모두 가짜 값이다.
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


# ── 자동 삭제를 끈 점검(비상 정지·수동 점검): 읽기만 + 보관 대기 알림 ─────────────────────────────────────
def test_weekly_without_auto_only_reads_and_reminds_that_archiving_is_waiting():
    f, gh = _fake(), FakeIssues()
    res = RT.weekly(_env(f), NOW, auto=False, sink=_sink(gh))
    assert f.writes == [] and res.applied is None and not res.failed  # 쓰기·삭제 0
    assert _keys(res) == ["warn:retention:보관 대기"]
    assert "16줄" in res.alerts[0].text and "자동 삭제" in res.alerts[0].text
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


def test_weekly_default_is_to_apply_with_the_same_row_count_as_the_plan_in_the_same_run():
    f, gh = _fake(), FakeIssues()
    res = RT.weekly(_env(f), NOW, sink=_sink(gh))  # auto 를 따로 주지 않으면 켜짐
    assert res.auto is True and res.plan.total_rows == 16
    assert sum(res.applied.deleted.values()) == res.plan.total_rows == 16
    assert len(f.tables["forecast_runs"]) == 6 and res.alerts == []


def test_rows_that_appear_between_plan_and_apply_stop_the_run_with_nothing_deleted():
    """plan 이 센 줄 수와 apply 가 보는 줄 수가 다르면(그 사이 새로 14일을 넘은 회차) 지우지 않고 경고 이슈를 연다"""
    f, gh = _fake(), FakeIssues()
    orig = RT.apply

    def apply_after_a_new_run_expires(env, now, through, confirm_rows, confirm_raw_files=0):
        f.add_run(7, "2026-10-10T02:00:00+09:00")  # plan 이후에 생긴 오래된 회차
        f.add_outlook(7)
        return orig(env, now, through, confirm_rows, confirm_raw_files)

    RT.apply = apply_after_a_new_run_expires
    try:
        res = RT.weekly(_env(f), NOW, sink=_sink(gh))
    finally:
        RT.apply = orig
    assert (
        res.failed
        and _keys(res) == ["error:retention:보관·삭제 중단"]
        and "승인한 줄 수" in res.alerts[0].text
    )
    assert not [w for w in f.writes if w[0] == "DELETE"]
    assert gh.issues and gh.issues[0]["state"] == "open"


def test_a_failed_week_opens_the_alert_issue_and_the_next_good_week_closes_it():
    f, gh = _fake(), FakeIssues()
    f.tamper_on_download = {R.archive_path("water_outlook", "2026-09")}
    bad = RT.weekly(_env(f), NOW, sink=_sink(gh))
    assert bad.failed and bad.action == "opened" and gh.issues[0]["state"] == "open"
    again = RT.weekly(_env(f), NOW, sink=_sink(gh))  # 같은 문제가 이어지면 댓글을 다시 달지 않는다
    assert again.action == "unchanged" and gh.comments == []
    f.tamper_on_download = set()
    good = RT.weekly(_env(f), NOW + pd.Timedelta(days=7), sink=_sink(gh))
    assert not good.failed and good.action == "closed" and gh.issues[0]["state"] == "closed"
    assert any("정상 회복" in c for _, c in gh.comments)


def test_the_weekly_alert_issue_is_separate_from_the_monitor_issue():
    from croplib_dp.io.github_issue import LABEL, TITLE

    assert (
        RT.ALERT_TITLE != TITLE and RT.ALERT_LABEL != LABEL
    )  # monitor 가 이 경고를 "정상 회복"으로 닫지 않게 따로 둔다


# ── 예약 실행: 기본 켜짐, plan → 같은 줄 수로 apply ────────────────────────────────────────────────
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
    assert "1GB 확인" in res.alerts[0].text and "가정" not in res.alerts[0].text
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
def test_cli_weekly_no_auto_writes_reports_reads_only_and_returns_zero(tmp_path):
    f = _fake()
    code = RT.main(["weekly", "--no-auto", "--no-issue", "--out", str(tmp_path)], env=_env(f), now=NOW)
    assert code == 0 and f.writes == []
    assert (tmp_path / "retention" / "weekly.md").exists()
    data = json.loads((tmp_path / "retention" / "weekly.json").read_text(encoding="utf-8"))
    assert data["auto"] is False and data["rows_waiting"] == 16 and data["failed"] is False


def test_cli_weekly_by_default_applies_and_returns_one_when_the_run_failed(tmp_path):
    f = _fake()
    f.tamper_on_download = {R.archive_path("water_outlook", "2026-09")}
    assert RT.main(["weekly", "--no-issue", "--out", str(tmp_path)], env=_env(f), now=NOW) == 1


def test_cli_weekly_with_a_small_cap_returns_one_and_deletes_nothing(tmp_path):
    f = _fake()
    assert (
        RT.main(
            ["weekly", "--max-rows", "5", "--no-issue", "--out", str(tmp_path)],
            env=_env(f),
            now=NOW,
        )
        == 1
    )
    assert f.writes == []


def test_cli_weekly_by_default_deletes_and_reports(tmp_path):
    f = _fake()
    code = RT.main(["weekly", "--no-issue", "--out", str(tmp_path)], env=_env(f), now=NOW)
    data = json.loads((tmp_path / "retention" / "weekly.json").read_text(encoding="utf-8"))
    assert (
        code == 0
        and data["auto"] is True
        and data["applied_rows"] == 16
        and len(f.tables["water_outlook"]) == 6
    )


def test_the_old_auto_flag_is_gone_and_manual_modes_are_unchanged():
    import inspect

    src = inspect.getsource(RT.main)
    assert '"--auto"' not in src and '"--no-auto"' in src
    for mode in ('"plan"', '"apply"', '"restore"', '"weekly"'):
        assert mode in src


def test_cli_weekly_default_cap_is_8000_rows():
    assert RT.DEFAULT_MAX_ROWS == 8000


# ── 워크플로 설정 (글자 검사) ──────────────────────────────────────────
def _yml(name):
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_workflow_runs_every_sunday_0510_kst_and_has_the_issue_permission():
    y = _yml("retention.yml")
    assert "schedule:" in y and 'cron: "10 20 * * 6"' in y  # UTC 토요일 20:10 = 한국 일요일 05:10
    assert "issues: write" in y and "GITHUB_TOKEN" in y
    assert "inputs.mode || 'weekly'" in y  # 예약 실행은 weekly


def test_workflow_hands_the_three_facts_to_python_and_decides_nothing_itself():
    """끄는 판단은 셸이 아니라 파이썬(decide_auto)이 한다. 워크플로는 사실 셋(실행 방식·weekly_apply·변수 값)만 넘긴다."""
    y = _yml("retention.yml")
    assert "AUTO_APPLY: ${{ vars.RETENTION_AUTO_APPLY }}" in y and "EVENT: ${{ github.event_name }}" in y
    assert "WEEKLY_APPLY: ${{ inputs.weekly_apply }}" in y
    assert (
        '--event "$EVENT"' in y and '--weekly-apply "$WEEKLY_APPLY"' in y and '--auto-var "$AUTO_APPLY"' in y
    )
    assert "--no-auto" not in y.replace("# ", "")  # 셸에서 직접 끄는 줄이 없다(주석 제외)
    assert "RETENTION_AUTO_APPLY=true" not in y  # 켜기 위해 변수를 만들 필요가 없다


def test_manual_weekly_input_weekly_apply_exists():
    y = _yml("retention.yml")
    assert "weekly_apply" in y and "default: false" in y


# ── 켜짐·꺼짐 판단과 이유 (사용자 보고 2026-11-08: 변수를 만든 적이 없는데 "비상 정지 변수 false"로 나옴) ──────────
@pytest.mark.parametrize(
    "event, weekly_apply, var, auto, reason",
    [
        ("schedule", "", "", True, "schedule"),  # 예약 + 변수 없음(빈 글자) = 삭제 켜짐. 가장 중요한 경우
        ("schedule", "", "true", True, "schedule"),
        (
            "schedule",
            "false",
            "",
            True,
            "schedule",
        ),  # 예약에는 weekly_apply 가 비어 있다. 값이 있어도 영향 없음
        ("schedule", "", "false", False, "emergency"),  # 비상 정지
        ("schedule", "", " False ", False, "emergency"),  # 대소문자·공백이 달라도 끈다(안전한 쪽)
        ("workflow_dispatch", "false", "", False, "manual"),  # 수동 점검
        ("workflow_dispatch", "", "", False, "manual"),
        ("workflow_dispatch", "true", "", True, "weekly_apply"),  # 수동 + weekly_apply 켬
        ("workflow_dispatch", "true", "false", False, "emergency"),  # 비상 정지가 weekly_apply 보다 앞선다
        ("workflow_dispatch", "false", "false", False, "emergency"),  # 둘 다면 비상 정지가 이유
    ],
)
def test_decide_auto_covers_every_case(event, weekly_apply, var, auto, reason):
    d = RT.decide_auto(event, weekly_apply, var)
    assert (d.auto, d.reason) == (auto, reason)


def test_decide_auto_direct_cli_run_and_the_explicit_off_flag():
    assert RT.decide_auto(None, "", "") == RT.AutoDecision(True, "cli")
    assert RT.decide_auto("schedule", "", "", no_auto=True) == RT.AutoDecision(False, "flag")


def test_the_three_off_reasons_and_the_on_reasons_read_differently():
    texts = {r: RT.AUTO_REASON_TEXT[r] for r in ("manual", "emergency", "schedule", "weekly_apply")}
    assert "수동 점검" in texts["manual"] and "weekly_apply 꺼짐" in texts["manual"]
    assert "비상 정지" in texts["emergency"] and "RETENTION_AUTO_APPLY=false" in texts["emergency"]
    assert "예약" in texts["schedule"] and "weekly_apply" in texts["weekly_apply"]
    assert "RETENTION_AUTO_APPLY" not in texts["manual"] and "RETENTION_AUTO_APPLY" not in texts["schedule"]
    assert len(set(texts.values())) == 4


def _run_weekly_cli(tmp_path, *args):
    f = _fake()
    code = RT.main(["weekly", "--no-issue", "--out", str(tmp_path), *args], env=_env(f), now=NOW)
    data = json.loads((tmp_path / "retention" / "weekly.json").read_text(encoding="utf-8"))
    return f, code, data, (tmp_path / "retention" / "weekly.md").read_text(encoding="utf-8")


def test_scheduled_run_with_the_variable_unset_really_deletes(tmp_path):
    """예약 실행 + 변수 없음(Actions 에서는 빈 글자) → 실제로 지운다. 워크플로가 넘기는 인자 그대로."""
    f, code, data, md = _run_weekly_cli(
        tmp_path, "--event", "schedule", "--weekly-apply", "", "--auto-var", ""
    )
    assert code == 0 and data["auto"] is True and data["auto_reason"] == "schedule"
    assert data["applied_rows"] == 16 and len(f.tables["water_outlook"]) == 6
    assert "켜짐" in md and "예약 실행" in md and "꺼짐" not in md and "RETENTION_AUTO_APPLY" not in md


def test_scheduled_run_with_the_variable_false_only_reads_and_says_emergency_stop(tmp_path):
    f, code, data, md = _run_weekly_cli(
        tmp_path, "--event", "schedule", "--weekly-apply", "", "--auto-var", "false"
    )
    assert code == 0 and f.writes == [] and data["auto"] is False and data["auto_reason"] == "emergency"
    assert "꺼짐" in md and "비상 정지" in md and "RETENTION_AUTO_APPLY=false" in md
    assert "수동 점검" not in md and len(f.tables["water_outlook"]) == 18


def test_manual_weekly_without_weekly_apply_reads_only_and_says_manual_check(tmp_path):
    """사용자가 11/8 14:28 에 누른 경우: 변수는 없고 수동 실행, weekly_apply 꺼짐."""
    f, code, data, md = _run_weekly_cli(
        tmp_path, "--event", "workflow_dispatch", "--weekly-apply", "false", "--auto-var", ""
    )
    assert code == 0 and f.writes == [] and data["auto"] is False and data["auto_reason"] == "manual"
    assert "수동 점검(weekly_apply 꺼짐)" in md
    assert "비상 정지" not in md and "RETENTION_AUTO_APPLY=false" not in md  # 변수 탓이라고 하지 않는다


def test_manual_weekly_with_weekly_apply_deletes_like_the_schedule(tmp_path):
    f, code, data, md = _run_weekly_cli(
        tmp_path, "--event", "workflow_dispatch", "--weekly-apply", "true", "--auto-var", ""
    )
    assert code == 0 and data["auto"] is True and data["auto_reason"] == "weekly_apply"
    assert data["applied_rows"] == 16 and "weekly_apply 켬" in md


def test_waiting_reminder_names_the_real_reason():
    for reason, must, must_not in [
        ("manual", "수동 점검", "비상 정지"),
        ("emergency", "비상 정지", "수동 점검"),
    ]:
        f = _fake()
        res = RT.weekly(_env(f), NOW, auto=False, auto_reason=reason)
        t = res.alerts[0].text
        assert must in t and must_not not in t and "16줄" in t
        assert (
            res.alerts[0].key == "warn:retention:보관 대기"
        )  # 열쇠는 이유와 무관(이슈에 댓글이 쌓이지 않음)


def test_manual_modes_keep_their_inputs():
    y = _yml("retention.yml")
    for needle in (
        "confirm_rows",
        "confirm_raw_files",
        "restore_apply",
        "options: [plan, apply, restore, weekly]",
        "default: plan",
    ):
        assert needle in y


def test_keepalive_re_enables_the_retention_schedule_too():
    assert "retention.yml" in _yml("keepalive.yml")


# ── 문서 ────────────────────────────────────────────────────────────────
def test_decision_document_says_the_cap_goes_up_with_s4_and_that_storage_1gb_is_confirmed():
    d = (ROOT / "docs" / "decisions" / "0022-retention-archive.md").read_text(encoding="utf-8")
    assert "S4(예보 확대) 때 함께 올린다" in d
    assert "1GB 확인됨" in d and "한도 1GB 가정" not in d and "1GB는 가정" not in d
