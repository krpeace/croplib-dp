"""끝난 작기의 공개 물수지 지난 줄은 다시 쓰지 않는다 (이슈 #139, 결정 0042). 입력은 모두 가짜 값이다.

- 끝난 작기 = `scenarios.season_end`가 있고 14일 창 시작일(어제 − 13일)보다 앞. 그 조건은 대표·대표 밖,
  소급(--soil-backfill · --wb-backfill)과 상관없이 14일 창 줄만 올린다(창은 모두 작기 끝 뒤다).
- 그 창 안에서 상태가 dormant 가 아닌 줄은 올리지 않고 경고에 남긴다(회차는 실패가 아님).
- 작기 끝이 창 안이거나 앞으로이거나 비어 있으면 전과 같다. 계산·전망은 바뀌지 않는다.
"""

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.core.wb_upload import upload_rows  # noqa: E402
from test_collector import ISSUE, ISSUES, KEY, FakeApi, FakeClock  # noqa: E402
from test_s5a import _d  # noqa: E402
from test_water_protected import protected_dicts  # noqa: E402

WIN_START = dt.date(2026, 5, 27)  # ISSUE(6/10 02시)의 어제 6/9 포함 14일
WINDOW = [str(WIN_START + dt.timedelta(days=k)) for k in range(14)]
APPLE = "101-apple-main-k3-loam-2026"
CABBAGE = "101-kimchi_cabbage-summer-k1-loam-2026"
NONREP_APPLE = "301-apple-main-k3-loam-2026"
SHORT_LENS = (10, 10, 10, 10)  # 4/1 시작이면 5/10 까지 작기 안
OTHER_TABLES = (
    "weather_daily",
    "soil_water_daily",
    "water_outlook",
    "water_outlook_summary",
    "forecast_runs",
)


# ── 순수 함수 ───────────────────────────────────────────────────────────
def _rows(first: dt.date, last: dt.date, status="dormant"):
    n = (last - first).days + 1
    return [dict(date=first + dt.timedelta(days=k), status=status) for k in range(n)]


@pytest.mark.parametrize("window_only", [True, False])
def test_ended_season_keeps_only_the_window_whatever_the_station_kind(window_only):
    rows = _rows(dt.date(2026, 4, 1), dt.date(2026, 6, 9))
    for end in ("2026-05-26", dt.date(2026, 4, 30)):
        kept, dropped = upload_rows(rows, end, WIN_START, window_only)
        assert [str(r["date"]) for r in kept] == WINDOW and dropped == 0


@pytest.mark.parametrize("end", [None, "", "2026-05-27", "2026-06-08", "2026-06-09", "2026-10-27"])
def test_season_not_yet_outside_the_window_is_as_before(end):
    rows = _rows(dt.date(2026, 4, 1), dt.date(2026, 6, 9), status="need")
    assert upload_rows(rows, end, WIN_START, False) == (rows, 0)
    kept, dropped = upload_rows(rows, end, WIN_START, True)
    assert [str(r["date"]) for r in kept] == WINDOW and dropped == 0


def test_ended_season_drops_and_counts_non_dormant_window_rows():
    rows = _rows(dt.date(2026, 5, 20), dt.date(2026, 6, 1), "need") + _rows(
        dt.date(2026, 6, 2), dt.date(2026, 6, 9)
    )
    kept, dropped = upload_rows(rows, "2026-05-10", WIN_START, False)
    assert dropped == 6  # 5/27 ~ 6/1 (창 앞의 줄은 원래 안 올리므로 세지 않는다)
    assert [str(r["date"]) for r in kept] == WINDOW[6:]


# ── 한 회차 ─────────────────────────────────────────────────────────────
def _dicts_ended(end="2026-05-10", lens=SHORT_LENS, season_end_ids=(APPLE, NONREP_APPLE)):
    """101(대표) + 301(대표 밖 예보 지점, 시드됨). 사과 조건의 작기 끝을 end 로, 단계 일수를 lens 로."""
    d = _d()
    for s in d.scenarios:
        if s["scenario_id"] in season_end_ids:
            s["season_end"] = end
    if lens:
        d.seasons["apple-main"].update(zip(("l_ini_d", "l_dev_d", "l_mid_d", "l_late_d"), lens, strict=True))
    return d


def _run(d, soil_backfill=False):
    clock = FakeClock("2026-06-10 02:20")
    return W.run_slot(
        "아침", ISSUE, d, KEY, http=FakeApi(clock, ISSUES), clock=clock, stations=["101", "301"],
        nonrep_scen=True, soil_backfill=soil_backfill,
    )  # fmt: skip


def _of(r, sid):
    return [x for x in r.water_balance_daily if x["scenario_id"] == sid]


@pytest.mark.parametrize("sid", [APPLE, NONREP_APPLE])
def test_ended_season_uploads_only_dormant_window_rows(sid):
    """대표(①)·대표 밖(②) 모두: 작기 끝 이하 줄 0, 14일 창만."""
    r = _run(_dicts_ended())
    rows = _of(r, sid)
    assert [x["date"] for x in rows] == WINDOW
    assert all(x["status"] == "dormant" for x in rows)
    assert not r.failed and not r.problems and not [w for w in r.warnings if "작기" in w]


def test_boundary_window_start_is_still_running_and_one_day_earlier_is_ended():
    still = _of(_run(_dicts_ended(end="2026-05-27")), APPLE)
    ended = _of(_run(_dicts_ended(end="2026-05-26")), APPLE)
    assert still[0]["date"] == "2026-04-01" and still[-1]["date"] == "2026-06-09"
    assert [x["date"] for x in ended] == WINDOW


def test_only_the_selected_rows_change_not_the_values_or_other_tables():
    """③ 병합 전 동작(season_end 가 빈 사전)과 비교: 값·진행 중 조건·전망은 그대로."""
    before = _run(_dicts_ended(end=None))
    after = _run(_dicts_ended())
    for t in OTHER_TABLES:
        a, b = getattr(before, t), getattr(after, t)
        assert a and a == b, t
    full = {(x["scenario_id"], x["date"]): x for x in before.water_balance_daily}
    assert all(full[x["scenario_id"], x["date"]] == x for x in after.water_balance_daily)
    ended = {APPLE, NONREP_APPLE}
    assert [x for x in before.water_balance_daily if x["scenario_id"] not in ended] == [
        x for x in after.water_balance_daily if x["scenario_id"] not in ended
    ]
    assert len(_of(before, APPLE)) > len(_of(after, APPLE))


def test_empty_or_future_season_end_is_the_same_as_before():
    """③·⑥ season_end 가 비었거나(열이 없어도) 앞으로면 줄 목록과 값이 같다."""
    base = _run(_dicts_ended(end=None))
    future = _run(_dicts_ended(end="2026-10-27"))
    d = _dicts_ended(end=None)
    for s in d.scenarios:
        s.pop("season_end")
    missing = _run(d)
    assert base.water_balance_daily == future.water_balance_daily == missing.water_balance_daily
    assert _of(base, APPLE)[0]["date"] == "2026-04-01"
    assert _of(base, NONREP_APPLE)[0]["date"] == str(WIN_START)


def test_stages_longer_than_season_end_are_not_uploaded_and_are_warned():
    """④ 단계 일수 합이 season_end 보다 길면 비-dormant 줄은 안 올리고 경고. 다른 조건·지점은 정상."""
    # 4/1 부터 62일 = 6/1 까지 작기 안 → 창의 5/27~6/1 여섯 줄이 어긋난다
    r = _run(_dicts_ended(lens=(15, 15, 17, 15)))
    for sid in (APPLE, NONREP_APPLE):
        rows = _of(r, sid)
        assert [x["date"] for x in rows] == WINDOW[6:]
        assert all(x["status"] == "dormant" for x in rows)
        w = [w for w in r.warnings if sid in w]
        assert len(w) == 1 and "6줄" in w[0] and w[0].startswith(sid.split("-")[0])
    assert _of(r, CABBAGE)[0]["date"] == "2026-05-01"
    assert not r.problems and not r.failed
    assert APPLE in W.brief_text(r) and APPLE in W.summary_text(r)


def test_all_window_rows_non_dormant_means_no_rows_for_that_scenario():
    r = _run(_dicts_ended(lens=None))  # 사과 210일: 창 14줄 모두 작기 안
    assert not _of(r, APPLE) and _of(r, CABBAGE)
    assert any(APPLE in w and "14줄" in w for w in r.warnings)
    assert not r.problems and not r.failed
    assert W.check_contract(r, W.taw_map(_dicts_ended())) == []


def test_protected_etc_only_rows_after_the_end_follow_the_same_rule():
    d = protected_dicts()
    for s in d.scenarios:
        if s["season_type_id"] == "grape-short":
            s["season_end"] = "2026-05-10"
    d.seasons["grape-short"].update(l_ini_d=10, l_dev_d=10, l_mid_d=10, l_late_d=10)  # 5/1~6/9
    clock = FakeClock("2026-06-10 02:20")
    r = W.run_slot("아침", ISSUE, d, KEY, http=FakeApi(clock, ISSUES), clock=clock, stations=["101"])
    sid = "101-grape-short-k1-loam-2026"
    assert not _of(r, sid)
    assert any(sid in w and "14줄" in w for w in r.warnings)


def test_soil_backfill_does_not_rewrite_past_rows_of_an_ended_season():
    """⑤ --soil-backfill (창 없는 실행)에서도 같다."""
    r = _run(_dicts_ended(), soil_backfill=True)
    for sid in (APPLE, NONREP_APPLE):
        assert [x["date"] for x in _of(r, sid)] == WINDOW
    assert _of(r, "301-kimchi_cabbage-summer-k1-loam-2026")[0]["date"] == "2026-05-01"


def _wb_backfill(d):
    clock = FakeClock("2026-06-10 02:20")
    return W.run_wb_backfill("아침", ISSUE, d, KEY, ["301"], http=FakeApi(clock, ISSUES), clock=clock)


def test_wb_backfill_does_not_rewrite_past_rows_of_an_ended_season():
    """⑤ --wb-backfill 에서도 같다. 진행 중 조건은 작기 시작부터."""
    r = _wb_backfill(_dicts_ended())
    assert [x["date"] for x in _of(r, NONREP_APPLE)] == WINDOW
    assert _of(r, "301-kimchi_cabbage-summer-k1-loam-2026")[0]["date"] == "2026-05-01"
    long = _wb_backfill(_dicts_ended(lens=None))
    assert not _of(long, NONREP_APPLE)
    assert any(NONREP_APPLE in w and "14줄" in w for w in long.warnings)
    assert not long.problems
