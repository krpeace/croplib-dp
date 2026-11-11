"""수집 감시의 회차 기록 읽기를 최신 2회차로 줄이기 (이슈 #101, 사용자 승인: 최신 2회차 변형). 입력은 모두 가짜 값이다.

PR #98 실측: 감시가 `forecast_runs`를 최근 48시간 전부(97곳 × 4회차 = 388줄, 79KB)로 읽어 감시 1회가 0.10MB였다. 판정에 필요한 것은
지점별 가장 최근 줄이라 **최신 2회차**(발표 02시·17시 기준 최신 회차와 그 앞 회차)만 읽는다: 감시 1회 약 61KB(−39%).
"""

# ruff: noqa: E501

import json
import sys
import urllib.parse
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import monitor as M  # noqa: E402
from croplib_dp.core.monitor import check_health  # noqa: E402
from test_monitor import FakeDb  # noqa: E402

TS = pd.Timestamp
URL = "https://x.supabase.co"
IDS = [str(100 + i) for i in range(97)]


def _utc(kst: str) -> str:
    return TS(kst).tz_localize("Asia/Seoul").tz_convert("UTC").isoformat()


def _slot_rows(slot_kst: str, results=None, finished_after_min=40):
    """한 회차(발표 시각 slot_kst)의 97곳 줄. results: {지점: 결과}"""
    t = TS(slot_kst)
    out = []
    for s in IDS:
        out.append(
            dict(
                run_at=_utc(slot_kst),
                run_name="아침" if t.hour == 2 else "저녁",
                stn_id=s,
                started_at=_utc(str(t + pd.Timedelta(minutes=10))),
                finished_at=_utc(str(t + pd.Timedelta(minutes=finished_after_min))),
                result=(results or {}).get(s, "정시 성공"),
                on_time=True,
                error=None,
            )
        )
    return out


SLOTS = ["2026-11-09 17:00", "2026-11-10 02:00", "2026-11-10 17:00", "2026-11-11 02:00", "2026-11-11 17:00"]


class RunsDb(FakeDb):
    """forecast_runs 는 요청한 `run_at=gte.…` 조건으로 걸러 돌려준다(PostgREST처럼). 요청 주소를 기록한다."""

    def __init__(self, runs, **k):
        k.setdefault("heat_date", "2026-11-10")  # 감시가 어제(11/10) 적산온도를 묻는다
        super().__init__({}, **k)
        self.runs = runs
        self.read_rows = 0

    def __call__(self, url, headers):
        u = urllib.parse.urlparse(url)
        if u.path.endswith("/forecast_runs"):
            self.urls.append(url)
            q = dict(urllib.parse.parse_qsl(u.query))
            since = TS(q["run_at"].removeprefix("gte."))
            rows = [r for r in self.runs if TS(r["run_at"]) >= since]
            self.read_rows += len(rows)
            return 200, json.dumps(rows).encode()
        return super().__call__(url, headers)


@pytest.mark.parametrize(
    "now,since_kst",
    [
        ("2026-11-11 17:40", "2026-11-11 01:00"),  # 최신 17:00, 그 앞 02:00 → 02:00 − 1시간 여유
        ("2026-11-12 02:40", "2026-11-11 16:00"),  # 최신 02:00, 그 앞 전날 17:00
        ("2026-11-11 11:00", "2026-11-10 16:00"),  # 최신 02:00(오늘), 그 앞 전날 17:00
        (
            "2026-11-11 02:05",
            "2026-11-10 01:00",
        ),  # 02:00 회차는 02:10부터 시작 → 최신은 전날 17:00, 그 앞 전날 02:00
        ("2026-11-11 23:00", "2026-11-11 01:00"),
    ],
)
def test_runs_window_starts_one_hour_before_the_second_latest_slot(now, since_kst):
    got = pd.Timestamp(M.runs_since(TS(now))).tz_convert("Asia/Seoul").tz_localize(None)
    assert got == TS(since_kst)


def test_monitor_reads_only_the_latest_two_slots_not_48_hours():
    runs = [r for s in SLOTS for r in _slot_rows(s)]
    db = RunsDb(
        runs,
        stations=[{"stn_id": s, "name_ko": f"지점{s}", "has_forecast": True, "is_active": True} for s in IDS],
    )
    state = M.read_state(URL, "srv", db, now=TS("2026-11-11 18:40"))
    assert len(state["runs"]) == 2 * 97  # 11/11 02:00 + 17:00 두 회차 (예전에는 48시간 4회차 388줄)
    assert {r["run_at"] for r in state["runs"]} == {_utc("2026-11-11 02:00"), _utc("2026-11-11 17:00")}
    assert len(json.dumps(state["runs"], ensure_ascii=False).encode()) < 45_000  # 약 40KB (예전 79KB)
    (u,) = [x for x in db.urls if "forecast_runs" in x]
    assert "limit=20" not in u  # 쪽 나눠 읽기(limit 1000)는 그대로


def test_while_the_newest_slot_is_still_running_the_previous_two_are_enough():
    """17:15 회차가 도는 중(아직 줄이 없음)에도 직전 두 회차가 들어와 지점별 최신 줄이 있다."""
    runs = [r for s in SLOTS[:4] for r in _slot_rows(s)]  # 11/11 17:00 회차 줄은 아직 없음
    db = RunsDb(
        runs,
        stations=[{"stn_id": s, "name_ko": f"지점{s}", "has_forecast": True, "is_active": True} for s in IDS],
    )
    state = M.read_state(URL, "srv", db, now=TS("2026-11-11 17:20"))
    assert {r["run_at"] for r in state["runs"]} == {
        _utc("2026-11-11 02:00")
    }  # 최신 17:00은 아직 없고 02:00 한 회차
    assert len({r["stn_id"] for r in state["runs"]}) == 97


# ── 판정이 48시간 읽기와 같다 ────────────────────────────────────────────
def _names():
    return {s: f"지점{s}" for s in IDS}


def _alerts(rows, now):
    weather = {s: (TS(now) - pd.Timedelta(days=1)).date() for s in IDS}
    return sorted(
        (a.level, a.code, a.text)
        for a in check_health(TS(now), rows, weather, _names(), expected_run_stations=IDS)
    )


def test_judgement_with_two_slots_equals_judgement_with_all_48_hours():
    """실패·지연 수집·갱신 멈춤이 최신 2회차만으로도 전과 같은 경고를 낸다."""
    results = {"117": "실패", "118": "지연 수집", "119": "백업: 직전 발표"}
    all_rows = [r for s in SLOTS[:4] for r in _slot_rows(s)] + _slot_rows("2026-11-11 17:00", results)
    now = "2026-11-11 18:40"
    since = TS(M.runs_since(TS(now)))
    two = [r for r in all_rows if TS(r["run_at"]) >= since]
    win48 = [r for r in all_rows if TS(r["run_at"]) >= TS(_utc("2026-11-09 18:40"))]
    assert len(two) == 194 and len(win48) > len(two)
    got = _alerts(two, now)
    assert got == _alerts(win48, now)
    assert (
        sum(1 for a in got if a[0] == "error" and a[1] == "failed") == 1
        and sum(1 for a in got if a[1] == "late") == 2
    )


def test_a_station_that_missed_the_newest_slot_still_gets_the_stale_error_from_the_previous_slot():
    """최신 회차(11/11 17시)에 줄이 없는 120은 그 앞 회차(02시) 줄이 읽혀서 전과 같이 '갱신 멈춤' 오류가 난다(48시간 읽기와 같음)."""
    rows = [r for s in SLOTS[:4] for r in _slot_rows(s)]
    rows += [r for r in _slot_rows("2026-11-11 17:00") if r["stn_id"] != "120"]
    now = "2026-11-12 01:30"  # 02:00 회차 시작(02:10) 전: 최신 회차 17:00, 그 앞 02:00
    two = [r for r in rows if TS(r["run_at"]) >= TS(M.runs_since(TS(now)))]
    stale = [a for a in _alerts(two, now) if a[0] == "error" and a[1] == "stale"]
    assert len(stale) == 1 and "120 지점120" in stale[0][2] and "11/11 02시" in stale[0][2]
    assert _alerts(two, now) == _alerts(rows, now)  # 전체를 읽은 경우와 같다


def test_message_for_missing_stations_names_the_slots_not_48_hours():
    rows = _slot_rows("2026-11-11 17:00")
    weather = {s: TS("2026-11-11").date() for s in IDS}
    a = [
        x
        for x in check_health(
            TS("2026-11-11 19:00"), rows, weather, _names(), expected_run_stations=[*IDS, "999"]
        )
        if "기록 없음" in x.text
    ]
    assert a and "최근 2회차" in a[0].text and "48시간" not in a[0].text
