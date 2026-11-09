"""시험 실행(dry-run)도 쓰기 직전과 같은 계약 검사를 돌린다.

소급 5묶음(2026-11-09): dry-run 은 통과했는데 쓰기에서 `rain_mm=654.3 (범위 0~500)`으로 막혔다.
계약 검사는 `publish` 안에 있고 `publish` 는 쓰기일 때만 도는데, 시험 실행의 목적은 "쓰면 막힐 줄"을
미리 알리는 것이므로 DB에 쓰지 않으면서 같은 검사를 돌려야 한다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import water as W  # noqa: E402
from test_collector import ISSUES, FakeApi, FakeClock  # noqa: E402
from test_obs_all import _main_env, _obs_dicts, _run  # noqa: E402

DRY = [
    "--slot", "2026-06-10 02", "--event", "workflow_dispatch",
    "--input-stations", "301,302", "--input-backfill", "true", "--input-dry-run", "true",
]  # fmt: skip


def _corrupting(monkeypatch, **bad):
    """run_slot 결과의 첫 weather_daily 줄을 일부러 계약 밖 값으로 바꾼다."""
    inner = W.run_slot

    def run(*a, **k):
        r = inner(*a, **k)
        r.weather_daily[0].update(bad)
        return r

    monkeypatch.setattr(W, "run_slot", run)


def test_dry_run_with_a_value_the_write_would_reject_fails_and_names_the_row(monkeypatch, tmp_path, capsys):
    clock = FakeClock("2026-06-10 02:20")
    calls = _main_env(monkeypatch, _obs_dicts(), clock, FakeApi(clock, ISSUES))
    _corrupting(monkeypatch, rain_mm=1500.0)
    code = W.main([*DRY, "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1  # 쓰기 때와 같이 실패로 끝난다
    assert "## 문제" in out and "weather_daily 1번째 줄: rain_mm=1500.0 (범위 0~1000)" in out
    assert "DB 올리기: 안 함" in out
    assert calls == []  # 그래도 DB에는 쓰지 않는다


def test_dry_run_with_good_values_says_the_contract_check_passed(monkeypatch, tmp_path, capsys):
    clock = FakeClock("2026-06-10 02:20")
    calls = _main_env(monkeypatch, _obs_dicts(), clock, FakeApi(clock, ISSUES))
    code = W.main([*DRY, "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and "계약 검사: 통과 (쓰지 않고 미리 검사함)" in out and "DB 올리기: 안 함" in out
    assert "## 문제" not in out and calls == []


def test_the_real_record_654_3_mm_passes_the_dry_run(monkeypatch, tmp_path, capsys):
    clock = FakeClock("2026-06-10 02:20")
    _main_env(monkeypatch, _obs_dicts(), clock, FakeApi(clock, ISSUES))
    _corrupting(monkeypatch, rain_mm=654.3)
    assert W.main([*DRY, "--out", str(tmp_path)]) == 0
    assert "## 문제" not in capsys.readouterr().out


def test_a_normal_write_run_is_not_checked_twice(monkeypatch, tmp_path):
    """쓰기 실행의 검사는 지금처럼 publish 안에서만 한다(미리 검사 문구 없음)."""
    clock = FakeClock("2026-06-10 02:20")
    calls = _main_env(monkeypatch, _obs_dicts(), clock, FakeApi(clock, ISSUES))
    monkeypatch.setattr(W, "slot_done", lambda *a, **k: False)
    monkeypatch.setattr(W, "load_soil_prior", lambda *a, **k: {})
    W.main(["--slot", "2026-06-10 02", "--out", str(tmp_path), "--event", "schedule",
            "--input-stations", "", "--input-backfill", "", "--input-dry-run", ""])  # fmt: skip
    assert len(calls) == 1
    assert "미리 검사" not in (tmp_path / "20260610_0200" / "summary.md").read_text(encoding="utf-8")


def test_precheck_contract_marks_the_result_without_touching_the_db():
    r, _, _ = _run(obs_all=True)
    taw = W.taw_map(_obs_dicts())
    W.precheck_contract(r, taw)
    assert r.contract_checked and r.problems == [] and not r.uploaded
    r.weather_daily[0]["rain_mm"] = 1000.1
    W.precheck_contract(r, taw)
    assert any("rain_mm=1000.1 (범위 0~1000)" in p for p in r.problems) and r.failed
