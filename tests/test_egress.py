"""Supabase 응답 바이트 합계(egress 실측, 결정 0031). 입력은 모두 가짜 값이다.

Supabase 무료 플랜 egress(10/23~11/23 주기 5GB)가 빠듯해서(11/11 15시 4.73GB), 이 저장소가 DB에서 내려받는 양을
실행마다 한 줄로 남긴다: 저장 크기가 아니라 **응답 본문(JSON) 바이트**. 수집 요약과 감시 출력에 `DB 읽기(이 실행): …`.
"""

# ruff: noqa: E501

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import monitor as M  # noqa: E402
from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.io import db_stats, dictionaries, egress, supabase_write  # noqa: E402
from test_collector import ISSUES, FakeApi, FakeClock  # noqa: E402
from test_db_size import BASE, KEY, NOW, RUN  # noqa: E402
from test_obs_all import _main_env, _obs_dicts  # noqa: E402

REST = "https://x.supabase.co/rest/v1/"


@pytest.fixture(autouse=True)
def _fresh():
    egress.reset()
    yield
    egress.reset()


def test_labels_name_the_table_the_rpc_and_storage():
    assert egress.label(REST + "stations?select=*") == "stations"
    assert egress.label(REST + "rpc/db_size_bytes") == "rpc:db_size_bytes"
    assert egress.label("https://x.supabase.co/storage/v1/object/raw/fcst/a.json.gz") == "storage"
    assert egress.label("https://x.supabase.co/other") == "기타"


def test_record_sums_bytes_and_calls_per_label():
    egress.record(REST + "scenarios?select=*", b"x" * 1000)
    egress.record(REST + "scenarios?select=*", b"x" * 500)
    egress.record(REST + "stations?select=*", b"y" * 250)
    assert egress.total_bytes() == 1750 and egress.total_calls() == 3
    assert egress.by_label() == {"scenarios": (1500, 2), "stations": (250, 1)}
    egress.record(REST + "x", None)  # 본문이 없어도 요청 수는 센다(HEAD 등)
    assert egress.total_calls() == 4 and egress.total_bytes() == 1750


def test_summary_line_is_one_line_with_the_biggest_tables_first_and_mb_to_two_decimals():
    egress.record(REST + "forecast_runs?x", b"a" * 90_000)
    egress.record(REST + "stations?x", b"a" * 20_000)
    egress.record(REST + "scenarios?x", b"a" * 40_000)
    line = egress.summary_line()
    assert "\n" not in line
    assert line.startswith("DB 읽기(이 실행): 0.14MB (요청 3번)")  # 150,000B = 0.14MB
    assert line.index("forecast_runs") < line.index("scenarios") < line.index("stations")
    assert egress.summary_line(top=1).count("·") == 0 and "stations" not in egress.summary_line(top=1)


def test_empty_summary_still_prints_zero_so_the_log_always_has_the_line():
    assert egress.summary_line() == "DB 읽기(이 실행): 0.00MB (요청 0번)"


class _Resp:
    def __init__(self, content):
        self.status_code, self.content, self.headers = 200, content, {"content-type": "application/json"}


def test_the_three_real_http_helpers_count_what_they_download(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda url, **k: _Resp(b"g" * 300))
    monkeypatch.setattr(requests, "request", lambda method, url, **k: _Resp(b"r" * 700))
    assert dictionaries.rest_get(REST + "stations?select=*", {}) == (200, b"g" * 300)
    assert supabase_write.http_request("POST", REST + "forecast_runs?on_conflict=x", {}, b"{}")[0] == 200
    assert db_stats.http_request_h("HEAD", REST + "weather_daily?select=stn_id", {}, b"")[0] == 200
    assert egress.by_label() == {"stations": (300, 1), "forecast_runs": (700, 1), "weather_daily": (700, 1)}


def test_collect_summary_and_the_run_print_the_line(monkeypatch, tmp_path, capsys):
    clock = FakeClock("2026-06-10 02:20")
    _main_env(monkeypatch, _obs_dicts(), clock, FakeApi(clock, ISSUES))
    egress.record(REST + "scenarios?select=*", b"z" * 120_000)  # 이 실행에서 읽은 것으로 친다
    code = W.main(["--slot", "2026-06-10 02", "--out", str(tmp_path), "--event", "workflow_dispatch",
                   "--input-stations", "101", "--input-backfill", "", "--input-dry-run", "true"])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0 and "DB 읽기(이 실행): 0.11MB (요청 1번) — scenarios 0.11MB" in out
    summary = (tmp_path / "20260610_0200" / "summary.md").read_text(encoding="utf-8")
    assert "DB 읽기(이 실행): 0.11MB" in summary
    assert "DB 읽기(이 실행)" in (tmp_path / "20260610_0200" / "brief.md").read_text(encoding="utf-8")


def test_monitor_prints_the_line_after_its_result(monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_URL", BASE)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(
        M, "read_state",
        lambda *a, **k: dict(runs=RUN, names={"101": "춘천"}, weather_last={"101": NOW.date()}, heat_last={"101": NOW.date()}),
    )  # fmt: skip
    monkeypatch.setattr(M, "db_size_state", lambda *a, **k: dict(db_size=dict(mb=100.0, exact=True)))
    egress.record(REST + "forecast_runs?select=x", b"q" * 50_000)

    class Sink:
        def sync(self, keys, text):
            return "none"

    monkeypatch.setattr(M, "IssueAlerts", lambda *a, **k: Sink())
    assert M.main([]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1].startswith("DB 읽기(이 실행): 0.05MB (요청 1번)") and "forecast_runs" in lines[-1]
    assert lines[-2].startswith("이슈:")
