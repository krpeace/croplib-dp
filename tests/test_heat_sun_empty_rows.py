"""적산온도·적산일사: 빈 줄 날은 쓰지 않기, 이미 끝난 날 건너뛰기 (이슈 #104).

2026-11-11 일에 기상청 ASOS 가 어제 줄을 값 없이(날짜만) 돌려주자, 늦은 예약 실행이 이미 올라간
값을 비운 줄(gdd_cum 비어 있음)로 덮어썼다. 입력은 모두 가짜 값이다.
"""

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent / "data"))

from heat_sun_fakes import KEY, FakeAsos, FakeClock, _dicts, dicts_with_common  # noqa: E402

from croplib_dp.collectors import heat_sun as H  # noqa: E402
from croplib_dp.core import heat_sun as C  # noqa: E402
from croplib_dp.io.contract import check_rows  # noqa: E402

TODAY = pd.Timestamp("2026-10-25 09:20")  # 어제 = 2026-10-24
BLANK = ("maxTa", "minTa", "avgTa", "avgRhm", "avgWs", "sumGsr", "sumRn")


class BlankDaysAsos(FakeAsos):
    """지정한 날은 날짜만 있고 값이 모두 빈 줄로 돌려준다 (기상청이 값을 거둬들인 날)"""

    def __init__(self, blank_days=(), **kw):
        super().__init__(**kw)
        self.blank_days = {d.isoformat() for d in blank_days}

    def __call__(self, url, timeout):
        code, body = super().__call__(url, timeout)
        if code != 200:
            return code, body
        doc = json.loads(body)
        items = doc.get("response", {}).get("body", {}).get("items", {}).get("item")
        for it in items or []:
            if it["tm"] in self.blank_days:
                it.update({k: "" for k in BLANK})
        return code, json.dumps(doc).encode()


def _run(blank=(), when=TODAY, **kw):
    api = BlankDaysAsos(blank_days=blank, **kw)
    r = H.run(dicts_with_common(), KEY, http=api, clock=FakeClock(when))
    return r


def _dates(rows, **where):
    return {x["date"] for x in rows if all(x[k] == v for k, v in where.items())}


# ── 빈 줄 날은 쓰지 않는다 ─────────────────────────────────────────────
def test_yesterday_blank_row_is_not_written_for_heat_and_sun():
    """오늘 사고 재현: 어제 줄이 빈 줄이면 그 날 줄을 올리지 않는다 (옛 값을 null 로 덮지 않음)."""
    r = _run(blank=[dt.date(2026, 10, 24)])
    for crop in ("common", "apple", "kimchi_cabbage"):
        d = _dates(r.heat_daily, stn_id="101", crop_id=crop)
        assert "2026-10-24" not in d, crop
        assert "2026-10-23" in d  # 그 전날까지는 그대로
    assert "2026-10-24" not in _dates(r.sun_daily, stn_id="101")
    assert "2026-10-23" in _dates(r.sun_daily, stn_id="101")
    assert "2026-10-24" not in _dates(r.heat_daily, stn_id="216")  # 일사 없는 지점도 같다


def test_no_row_with_a_date_up_to_yesterday_has_null_cum_after_a_blank_day():
    """빈 줄 날을 뺀 나머지에서, 관측 날(어제까지)의 줄은 모두 값이 있다."""
    r = _run(blank=[dt.date(2026, 10, 24)])
    ok = [x for x in r.heat_daily if x["date"] <= "2026-10-23"]
    assert ok and all(x["gdd_cum"] is not None for x in ok)
    assert check_rows("heat_daily", r.heat_daily) == [] and check_rows("sun_daily", r.sun_daily) == []


def test_blank_day_is_reported_not_silent():
    r = _run(blank=[dt.date(2026, 10, 24)])
    text = H.brief_text(r)
    assert "빈 줄" in text and "2026-10-24" in text and "2곳" in text  # 운영 2지점
    assert not r.failed  # 실패는 아니다 (다음 실행이 채운다)


def test_blank_day_in_the_middle_is_also_not_written_but_cumulative_goes_on():
    """중간에 낀 빈 줄 날도 올리지 않는다. 쌓는 값은 평년으로 이어 간다 (뒤 날 줄은 정상)."""
    r = _run(blank=[dt.date(2026, 5, 5)])
    d = _dates(r.heat_daily, stn_id="101", crop_id="common")
    assert "2026-05-05" not in d and {"2026-05-04", "2026-05-06"} <= d
    after = next(x for x in r.heat_daily if x["stn_id"] == "101" and x["date"] == "2026-10-24")
    assert after["gdd_cum"] is not None


def test_normal_days_are_all_written_when_nothing_is_blank():
    """빈 줄이 없으면 줄 수는 전과 같다 (2지점 × 3작물 × 365일, sun 365)."""
    r = _run()
    assert len(r.heat_daily) == 2 * 3 * 365 and len(r.sun_daily) == 365
    assert "빈 줄" not in H.brief_text(r)


def test_row_with_only_rain_blank_is_a_normal_row():
    """비 안 온 날은 강수 칸만 빈다. 기온 등이 있으면 정상 줄이다."""

    class DryAsos(FakeAsos):
        def __call__(self, url, timeout):
            code, body = super().__call__(url, timeout)
            doc = json.loads(body)
            for it in doc["response"]["body"]["items"]["item"]:
                it["sumRn"] = ""
            return code, json.dumps(doc).encode()

    r = H.run(dicts_with_common(), KEY, http=DryAsos(), clock=FakeClock(TODAY))
    assert "2026-10-24" in _dates(r.heat_daily, stn_id="101", crop_id="common")


def test_absent_row_is_still_written_as_before():
    """줄 자체가 없는 날(빈 줄이 아님)은 지금처럼 쓴다 — 이번 수정은 '날짜만 있는 빈 줄'에만 해당."""
    r = _run(missing={dt.date(2026, 5, 5)})
    assert "2026-05-05" in _dates(r.heat_daily, stn_id="101", crop_id="common")


def test_jan_1_blank_dec_31_is_not_written_then_filled_on_jan_2():
    r = H.run(
        dicts_with_common(),
        KEY,
        http=BlankDaysAsos(blank_days=[dt.date(2026, 12, 31)]),
        clock=FakeClock("2027-01-01 09:20"),
    )
    d26 = _dates(r.heat_daily, stn_id="101", crop_id="common")
    assert "2026-12-31" not in d26 and "2026-12-30" in d26
    r2 = H.run(dicts_with_common(), KEY, http=FakeAsos(), clock=FakeClock("2027-01-02 09:20"))
    assert "2026-12-31" in _dates(r2.heat_daily, stn_id="101", crop_id="common")


# ── 건너뛰기: 필요한 날과 아직 안 채운 지점 (순수 함수) ───────────────────────
D = dt.date


def test_required_dates_is_yesterday_and_in_january_also_last_dec_31():
    assert C.required_dates(D(2026, 10, 25)) == [D(2026, 10, 24)]
    assert C.required_dates(D(2027, 1, 1)) == [D(2026, 12, 31)]
    assert C.required_dates(D(2027, 1, 2)) == [D(2027, 1, 1), D(2026, 12, 31)]
    assert C.required_dates(D(2027, 1, 3)) == [D(2027, 1, 2), D(2026, 12, 31)]
    assert C.required_dates(D(2027, 1, 4)) == [D(2027, 1, 3)]


def test_pending_none_when_every_station_has_every_required_value():
    y = D(2026, 10, 24)
    heat = {("101", y), ("216", y)}
    sun = {("101", y), ("101", y - dt.timedelta(days=1))}
    assert C.pending_stations(["101", "216"], [y], heat, sun) == []


def test_pending_lists_station_without_heat_value():
    y = D(2026, 10, 24)
    heat = {("101", y)}  # 216 은 아침 실행이 못 채웠다
    assert C.pending_stations(["101", "216"], [y], heat, set()) == ["216"]


def test_pending_lists_station_whose_sunlight_was_there_the_day_before_but_not_yesterday():
    y, p = D(2026, 10, 24), D(2026, 10, 23)
    heat = {("101", y), ("216", y)}
    sun = {("101", p)}  # 101 은 전날 일사가 있었는데 어제는 없다
    assert C.pending_stations(["101", "216"], [y], heat, sun) == ["101"]


def test_pending_station_that_never_had_sunlight_is_not_expected_to_have_it():
    y = D(2026, 10, 24)
    assert C.pending_stations(["216"], [y], {("216", y)}, set()) == []


def test_pending_checks_last_dec_31_too_in_early_january():
    j2, d31 = D(2027, 1, 2), D(2026, 12, 31)
    heat = {("101", j2)}  # 1/1 은 있는데 12/31 이 비었다
    assert C.pending_stations(["101"], [j2, d31], heat, set()) == ["101"]


def test_pending_with_no_operating_station_is_empty_list_and_caller_must_not_skip():
    assert C.pending_stations([], [D(2026, 10, 24)], set(), set()) == []


# ── main --skip-if-done ───────────────────────────────────────────────
class Db:
    """읽기 전용 가짜 DB: heat_daily·sun_daily 에서 이미 있는 (지점, 날짜) 를 돌려준다"""

    def __init__(self, heat=(), sun=(), fail=False):
        self.heat, self.sun, self.fail, self.queries = set(heat), set(sun), fail, []

    def __call__(self, table, dates):
        self.queries.append((table, tuple(dates)))
        if self.fail:
            raise RuntimeError("Supabase heat_daily 읽기 실패: HTTP 503")
        return self.heat if table == "heat_daily" else self.sun


@pytest.fixture
def app(tmp_path, monkeypatch):
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "DATA_GO_KR"):
        monkeypatch.setenv(k, KEY if k == "DATA_GO_KR" else "x")
    monkeypatch.setattr(H, "load_dictionaries", lambda *a, **k: _dicts())
    api = FakeAsos()
    real_run = H.run
    monkeypatch.setattr(H, "run", lambda *a, **k: real_run(*a, http=api, **k))
    monkeypatch.setattr(H, "Clock", lambda: FakeClock(TODAY))
    writes = []

    class W:
        def __init__(self, *a):
            pass

        def upsert(self, table, rows, on_conflict, returning=False):
            writes.append((table, len(list(rows))))
            return []

    import croplib_dp.io.supabase_write as sw

    monkeypatch.setattr(sw, "SupabaseWriter", W)
    return dict(api=api, writes=writes, out=tmp_path / "out", monkeypatch=monkeypatch)


Y = D(2026, 10, 24)
ALL_DONE = dict(heat={("101", Y), ("216", Y)}, sun={("101", Y), ("101", Y - dt.timedelta(days=1))})


def _main(app, db, *extra):
    app["monkeypatch"].setattr(H, "read_done", db)
    return H.main(["--out", str(app["out"]), "--write", *extra])


def test_skip_if_done_skips_when_everything_is_already_there(app):
    db = Db(**ALL_DONE)
    assert _main(app, db, "--skip-if-done") == 0
    assert app["api"].calls == [] and app["writes"] == []  # 받지도 쓰지도 않는다
    text = (app["out"] / "heat_sun_2026" / "brief.md").read_text(encoding="utf-8")
    assert "건너뜀" in text and "2026-10-24" in text


def test_skip_if_done_runs_when_a_station_is_still_missing(app):
    db = Db(heat={("101", Y)}, sun={("101", Y), ("101", Y - dt.timedelta(days=1))})
    assert _main(app, db, "--skip-if-done") == 0
    assert app["api"].calls and app["writes"]  # 늦은 실행이 못 채운 지점을 채운다
    assert "건너뜀" not in (app["out"] / "heat_sun_2026" / "brief.md").read_text(encoding="utf-8")


def test_skip_if_done_runs_when_database_read_fails_and_says_so(app):
    assert _main(app, Db(fail=True), "--skip-if-done") == 0
    assert app["api"].calls and app["writes"]  # 확인을 못 하면 평소처럼 돈다 (안전한 쪽)
    assert "건너뛸지 확인 못 함" in (app["out"] / "heat_sun_2026" / "brief.md").read_text(encoding="utf-8")


def test_without_the_flag_it_always_runs_and_never_reads_the_database(app):
    db = Db(**ALL_DONE)
    assert _main(app, db) == 0
    assert app["api"].calls and app["writes"] and db.queries == []


def test_skip_if_done_never_skips_a_dry_run_without_write(app):
    db = Db(**ALL_DONE)
    app["monkeypatch"].setattr(H, "read_done", db)
    assert H.main(["--out", str(app["out"]), "--skip-if-done"]) == 0  # --write 없음
    assert app["api"].calls and db.queries == []


def test_skip_if_done_with_explicit_stations_is_not_skipped(app):
    db = Db(**ALL_DONE)
    assert _main(app, db, "--skip-if-done", "--stations", "101") == 0
    assert app["api"].calls and db.queries == []
