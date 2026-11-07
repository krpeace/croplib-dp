"""적산온도·적산일사 수집기 (collectors/heat_sun.py).

가짜 ASOS·가짜 시계·가짜 DB 쓰기로 돈다. 입력은 모두 가짜 값이다.
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
from croplib_dp.collectors.water import RunLog  # noqa: E402
from croplib_dp.io.asos_cache import AsosCache  # noqa: E402
from croplib_dp.io.contract import check_rows  # noqa: E402

TODAY = pd.Timestamp("2026-10-25 09:20")


def _run(**kw):
    api = FakeAsos(**kw)
    clock = FakeClock(TODAY)
    r = H.run(_dicts(), KEY, http=api, clock=clock)
    return r, api, clock


def test_fetches_ten_normal_years_and_this_year_by_year():
    r, api, _ = _run()
    by = [c for c in api.calls if c[0] == "101"]
    assert [(s, e) for _, s, e in by][:1] == [("20160101", "20161231")]
    assert by[-1][1:] == ("20260101", "20261024")  # 올해는 어제까지
    assert len(by) == 11  # 2016~2025 + 올해, 해마다 한 번
    assert (r.normal_from, r.normal_to) == (2016, 2025)


def test_heat_rows_whole_year_with_lead_and_contract():
    r, _, _ = _run()
    rows = [x for x in r.heat_daily if x["stn_id"] == "101" and x["crop_id"] == "apple"]
    assert len(rows) == 365 and rows[0]["date"] == "2026-01-01" and rows[-1]["date"] == "2026-12-31"
    today = next(x for x in rows if x["date"] == "2026-10-24")
    assert today["gdd_cum"] > today["gdd_normal_cum"] and today["lead_days"] > 0  # 1도 따뜻 → 빠름
    future = next(x for x in rows if x["date"] == "2026-10-25")
    assert future["gdd_cum"] is None and future["gdd_normal_cum"] is not None
    assert today["t_base_c"] == 4 and today["normal_from"] == 2016 and today["normal_to"] == 2025
    assert {x["crop_id"] for x in r.heat_daily} == {"apple", "kimchi_cabbage"}
    assert check_rows("heat_daily", r.heat_daily) == []
    assert check_rows("sun_daily", r.sun_daily) == []


def test_sun_rows_only_where_sunlight_measured():
    r, _, _ = _run()
    assert {x["stn_id"] for x in r.sun_daily} == {"101"}  # 태백은 일사를 재지 않음
    s = next(x for x in r.sun_daily if x["date"] == "2026-01-03")
    assert s["rs_day_mj"] == 10 + 3 % 7 and s["rs_cum_mj"] > s["rs_day_mj"]
    tb = next(x for x in r.stations if x["stn_id"] == "216")
    assert tb["sun"] == "자료 없음"


def test_missing_day_filled_with_normal():
    r, _, _ = _run(missing={dt.date(2026, 5, 5)})
    x = next(
        x
        for x in r.heat_daily
        if x["stn_id"] == "101" and x["crop_id"] == "apple" and x["date"] == "2026-05-05"
    )
    assert x["filled"] is True and x["gdd_day"] == x["gdd_normal_day"]


def test_station_failure_is_recorded_not_silent():
    r, _, clock = _run(fail_stn="216")
    assert r.failed and any("216" in p for p in r.problems)
    assert {x["stn_id"] for x in r.heat_daily} == {"101"}  # 다른 지점은 그대로
    assert clock.slept > 0  # 다시 시도했다


def test_publish_upserts_both_tables_and_brief():
    r, _, _ = _run()

    class W:
        def __init__(self):
            self.calls = []

        def upsert(self, table, rows, on_conflict, returning=False):
            self.calls.append((table, len(list(rows)), on_conflict))
            return []

    w = W()
    H.publish(r, w)
    assert w.calls == [
        ("heat_daily", 1460, ("stn_id", "crop_id", "date")),  # 2지점 × 2작물 × 365일
        ("sun_daily", 365, ("stn_id", "date")),
    ]
    assert r.uploaded
    text = H.brief_text(r)
    assert "101 춘천" in text and "평년보다" in text and "DB 올리기: 완료" in text


def test_publish_refuses_on_contract_problem():
    r, _, _ = _run()
    r.heat_daily[0]["gdd_day"] = -5.0

    class W:
        def upsert(self, *a, **k):
            raise AssertionError("계약 검사에 걸리면 올리지 않는다")

    H.publish(r, W())
    assert not r.uploaded and r.failed and any("heat_daily" in p for p in r.problems)


def test_common_crop_base_5c_included_in_heat_daily():
    d = _dicts()
    d.crops["common"] = dict(crop_id="common", name_ko="작물 공통", t_base_c=5, gdd_stages=[1000, 2000])
    r = H.run(d, KEY, http=FakeAsos(), clock=FakeClock(TODAY))
    common_rows = [x for x in r.heat_daily if x["stn_id"] == "101" and x["crop_id"] == "common"]
    assert len(common_rows) == 365
    assert common_rows[0]["date"] == "2026-01-01" and common_rows[-1]["date"] == "2026-12-31"
    assert all(x["t_base_c"] == 5.0 for x in common_rows)
    assert check_rows("heat_daily", r.heat_daily) == []
    assert "작물 공통" in H.brief_text(r)


# ── 대상 지점: 운영(is_active) 지점만 ───────────────────────────────────
def test_only_active_stations_run_and_closed_one_is_not_asked_nor_failed():
    r, api, _ = _run()
    assert {c[0] for c in api.calls} == {"101", "216"}  # 폐지 지점(999)은 묻지도 않는다
    assert not r.failed and r.problems == []  # 실패로도 남기지 않는다
    assert [s["stn_id"] for s in r.stations] == ["101", "216"]
    assert "999" not in H.brief_text(r)


def test_explicit_stations_override_the_default_target():
    api = FakeAsos()
    r = H.run(_dicts(), KEY, http=api, clock=FakeClock(TODAY), stations=["101"])
    assert {c[0] for c in api.calls} == {"101"} and [s["stn_id"] for s in r.stations] == ["101"]


def test_zero_active_stations_is_a_problem_not_silence():
    d = _dicts()
    for st in d.stations.values():
        st["is_active"] = False
    api = FakeAsos()
    r = H.run(d, KEY, http=api, clock=FakeClock(TODAY))
    assert api.calls == [] and r.failed and any("0곳" in p and "is_active" in p for p in r.problems)


# ── 지난 해 캐시 ────────────────────────────────────────────────────────
def _run_cached(cache_dir, **kw):
    api = FakeAsos(**kw)
    r = H.run(_dicts(), KEY, http=api, clock=FakeClock(TODAY), cache=AsosCache(cache_dir))
    return r, api


def test_empty_cache_fetches_all_eleven_years_and_saves_the_ten_past_ones(tmp_path):
    r, api = _run_cached(tmp_path)
    assert len([c for c in api.calls if c[0] == "101"]) == 11
    files = sorted(p.name for p in (tmp_path / "asos" / "101").glob("*.json"))
    assert files == [f"{y}.json" for y in range(2016, 2026)]  # 올해(2026)는 캐시하지 않는다
    assert not r.failed


def test_warm_cache_fetches_only_this_year(tmp_path):
    _run_cached(tmp_path)
    r, api = _run_cached(tmp_path)
    assert sorted(api.calls) == [("101", "20260101", "20261024"), ("216", "20260101", "20261024")]
    assert not r.failed


def test_broken_cache_file_refetches_only_that_year(tmp_path):
    _run_cached(tmp_path)
    (tmp_path / "asos" / "101" / "2019.json").write_text("{깨진 JSON", encoding="utf-8")
    r, api = _run_cached(tmp_path)
    by101 = sorted(c for c in api.calls if c[0] == "101")
    assert by101 == [("101", "20190101", "20191231"), ("101", "20260101", "20261024")]
    assert not r.failed
    assert json.loads((tmp_path / "asos" / "101" / "2019.json").read_text(encoding="utf-8"))  # 다시 채워짐


def test_nodata_year_is_cached_and_not_asked_again(tmp_path):
    _, api = _run_cached(tmp_path, nodata_years={2018})
    assert json.loads((tmp_path / "asos" / "101" / "2018.json").read_text(encoding="utf-8")) == []
    r, api = _run_cached(tmp_path, nodata_years={2018})
    assert not any(c[1].startswith("2018") for c in api.calls)  # 빈 해도 다시 묻지 않는다
    assert len(api.calls) == 2 and not r.failed


def test_year_without_last_day_is_not_cached_until_complete(tmp_path):
    """1/1 실행처럼 지난해 12/31 관측이 아직 안 올라왔으면 그해는 캐시하지 않고, 다음에 다시 받는다."""
    _, api = _run_cached(tmp_path, missing={dt.date(2025, 12, 31)})
    files = sorted(p.name for p in (tmp_path / "asos" / "101").glob("*.json"))
    assert files == [f"{y}.json" for y in range(2016, 2025)]  # 2025 는 빠짐
    r, api = _run_cached(tmp_path)  # 이번엔 12/31 이 올라와 있다
    assert ("101", "20250101", "20251231") in api.calls and len(api.calls) == 4  # 2025 + 올해, 두 지점
    assert (tmp_path / "asos" / "101" / "2025.json").is_file() and not r.failed


def test_cache_keeps_only_calculation_fields(tmp_path):
    _run_cached(tmp_path)
    items = json.loads((tmp_path / "asos" / "101" / "2020.json").read_text(encoding="utf-8"))
    assert items and all(set(it) <= {"tm", "avgTa", "maxTa", "minTa", "sumGsr"} for it in items)


def test_failed_fetch_is_never_cached(tmp_path):
    r, _ = _run_cached(tmp_path, fail_stn="216")
    assert r.failed and not (tmp_path / "asos" / "216").exists()  # 실패는 캐시에 남기지 않는다
    assert (tmp_path / "asos" / "101").is_dir()


def test_cache_result_equals_full_fetch_result(tmp_path):
    """캐시를 쓴 실행과 다 받은 실행의 heat_daily·sun_daily 는 똑같다."""
    full = H.run(_dicts(), KEY, http=FakeAsos(), clock=FakeClock(TODAY))
    _run_cached(tmp_path)  # 캐시 채우기
    cached, api = _run_cached(tmp_path)
    assert len(api.calls) == 2  # 정말 캐시에서 읽었다
    assert cached.heat_daily == full.heat_daily and cached.sun_daily == full.sun_daily
    assert cached.stations == full.stations


def test_no_cache_by_default_fetches_everything():
    _, api, _ = _run()
    assert len([c for c in api.calls if c[0] == "101"]) == 11  # --cache-dir 이 없으면 지금처럼 다 받기


def test_main_cache_dir_option(tmp_path, monkeypatch):
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "DATA_GO_KR"):
        monkeypatch.setenv(k, KEY if k == "DATA_GO_KR" else "x")
    monkeypatch.setattr(H, "load_dictionaries", lambda *a, **k: _dicts())
    api = FakeAsos()
    real_run = H.run
    monkeypatch.setattr(
        H, "run", lambda *a, **k: real_run(*a, http=api, **k)
    )  # 기본 http 는 정의 때 고정이라 끼워 넣는다
    monkeypatch.setattr(H, "Clock", lambda: FakeClock(TODAY))
    out = tmp_path / "out"
    assert H.main(["--out", str(out), "--cache-dir", str(tmp_path / "c")]) == 0
    assert (tmp_path / "c" / "asos" / "101" / "2025.json").is_file()
    assert "ASOS 캐시" in (out / "heat_sun_2026" / "brief.md").read_text(encoding="utf-8")


# ── 새해 1월 1~3일 (이슈 #40) ──────────────────────────────────────────
NEW_YEAR = "2027-01-01 09:20"


def _new_year(when=NEW_YEAR, cache=None, **kw):
    api = FakeAsos(**kw)
    r = H.run(dicts_with_common(), KEY, http=api, clock=FakeClock(when), cache=cache)
    return r, api


def _rows(table, year, **where):
    return [x for x in table if x["date"].startswith(f"{year}-") and all(x[k] == v for k, v in where.items())]


def test_new_year_day_closes_last_year_with_dec_31_values():
    """1) 1/1 09:20: 작년 12/31 줄에 적산온도(3작물)·적산일사가 채워진다."""
    r, _ = _new_year()
    for stn in ("101", "216"):
        for crop in ("common", "apple", "kimchi_cabbage"):
            rows = _rows(r.heat_daily, 2026, stn_id=stn, crop_id=crop)
            assert len(rows) == 365 and rows[-1]["date"] == "2026-12-31"
            assert rows[-1]["gdd_cum"] is not None, (stn, crop)
            assert (rows[-1]["normal_from"], rows[-1]["normal_to"]) == (2016, 2025)
    sun_1231 = [x for x in r.sun_daily if x["date"] == "2026-12-31"]
    assert [x["stn_id"] for x in sun_1231] == ["101"]  # 태백(216)은 일사를 재지 않는다
    assert sun_1231[0]["rs_cum_mj"] is not None


def test_new_year_day_makes_only_the_normal_line_for_the_new_year():
    """2) 같은 실행: 새해 365줄은 평년선만 있고 올해 관측은 비어 있다. 역전 요청은 없다."""
    r, api = _new_year()
    assert not r.failed and r.problems == []
    assert api.reversed_calls == []
    assert r.year == 2027 and (r.normal_from, r.normal_to) == (2017, 2026)
    for stn in ("101", "216"):
        for crop in ("common", "apple", "kimchi_cabbage"):
            rows = _rows(r.heat_daily, 2027, stn_id=stn, crop_id=crop)
            assert len(rows) == 365
            assert all(x["gdd_cum"] is None and x["gdd_day"] is None for x in rows)
            assert all(x["gdd_normal_cum"] is not None for x in rows)
            assert all((x["normal_from"], x["normal_to"]) == (2017, 2026) for x in rows)
    sun = _rows(r.sun_daily, 2027, stn_id="101")
    assert len(sun) == 365
    assert all(x["rs_cum_mj"] is None and x["rs_normal_cum_mj"] is not None for x in sun)
    assert check_rows("heat_daily", r.heat_daily) == [] and check_rows("sun_daily", r.sun_daily) == []


def test_dec_31_missing_on_jan_1_stays_empty_and_is_not_cached_then_filled_on_jan_2(tmp_path):
    """4) 1/1 에 12/31 관측이 아직 없으면 그날은 비워 두고(캐시도 안 함), 1/2 에 채운다."""
    r, _ = _new_year(cache=AsosCache(tmp_path), missing={dt.date(2026, 12, 31)})
    last = _rows(r.heat_daily, 2026, stn_id="101", crop_id="common")[-1]
    assert last["date"] == "2026-12-31" and last["gdd_cum"] is None
    assert not (tmp_path / "asos" / "101" / "2026.json").exists()
    r2, _ = _new_year("2027-01-02 09:20", cache=AsosCache(tmp_path))
    last2 = _rows(r2.heat_daily, 2026, stn_id="101", crop_id="common")[-1]
    assert last2["date"] == "2026-12-31" and last2["gdd_cum"] is not None
    assert (tmp_path / "asos" / "101" / "2026.json").is_file() and not r2.failed


@pytest.mark.parametrize("when,last_obs", [("2027-01-02 09:20", "01-01"), ("2027-01-03 09:20", "01-02")])
def test_jan_2_and_3_rewrite_last_year_and_fill_new_year_observations(when, last_obs):
    """5) 1/2·1/3: 작년 줄을 다시 쓰고, 새해 관측은 어제까지 채워진다."""
    r, api = _new_year(when)
    assert not r.failed and api.reversed_calls == []
    prev = _rows(r.heat_daily, 2026, stn_id="101", crop_id="common")
    assert len(prev) == 365 and prev[-1]["gdd_cum"] is not None
    new = _rows(r.heat_daily, 2027, stn_id="101", crop_id="common")
    filled = [x["date"] for x in new if x["gdd_cum"] is not None]
    assert filled[-1] == f"2027-{last_obs}" and len(filled) == int(last_obs[-2:])


def test_jan_4_only_this_year():
    """5) 1/4 부터는 올해만 (평소와 같다)."""
    r, api = _new_year("2027-01-04 09:20")
    assert not r.failed
    assert _rows(r.heat_daily, 2026) == [] and _rows(r.sun_daily, 2026) == []
    assert len(_rows(r.heat_daily, 2027, stn_id="101", crop_id="common")) == 365
    assert (r.normal_from, r.normal_to) == (2017, 2026)


def test_fetch_year_skips_reversed_range_without_calling():
    """6) 시작일 > 종료일: 실패(None)가 아니라 빈 목록, 요청 0건, 로그 한 줄."""
    api, log, clock = FakeAsos(), RunLog(), FakeClock(NEW_YEAR)
    got = H.fetch_year(KEY, "101", dt.date(2027, 1, 1), dt.date(2026, 12, 31), api, clock, log)
    assert got == [] and api.calls == []
    assert len(log.entries) == 1 and "역전 구간 — 호출 생략" in log.entries[0]["note"]


def test_new_year_with_warm_cache_asks_only_last_year(tmp_path):
    """7) 지난 해 캐시가 따뜻한 1/1: 새로 묻는 것은 작년 한 해뿐이다 (역전 요청 없음)."""
    _run_cached(tmp_path)  # 12월 평일처럼 평년 10년을 캐시에 채운다 (작년·올해는 아직 캐시 안 됨)
    r, api = _new_year(cache=AsosCache(tmp_path))
    assert sorted(api.calls) == [("101", "20260101", "20261231"), ("216", "20260101", "20261231")]
    assert not r.failed


def test_new_year_brief_keeps_station_lines_and_adds_one_new_year_line():
    """지점 줄은 늘지 않는다(알림 4KB 제한). 작년 마감 기준 줄 + 새해 한 줄."""
    r, _ = _new_year()
    text = H.brief_text(r)
    assert text.startswith("# 적산온도·적산일사 2026 (평년 2016–2025")
    station_lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    weekday, _ = _new_year("2027-01-04 09:20")
    weekday_lines = [ln for ln in H.brief_text(weekday).splitlines() if ln.startswith("- ")]
    assert len(station_lines) == len(weekday_lines) == 2  # 지점 줄 수는 평일과 같다 (101, 216)
    assert "새해 2027: 평년선만 생성 (heat 2190줄 · sun 365줄)" in text
    r2, _ = _new_year("2027-01-02 09:20")
    assert "새해 2027: 관측 ~01-01까지 (heat 2190줄 · sun 365줄)" in H.brief_text(r2)


# ── 일사 무효 처리 (이슈 #44, 결정 0020) ────────────────────────────────────
def test_station_with_only_zero_radiation_overwrites_old_rows_with_empty_values():
    """일사가 늘 0.0 인 지점: 평년선이 비어도 줄을 만들어, DB 의 옛 줄("100%")을 빈 값으로 덮어쓴다"""
    r, _, _ = _run(zero_gsr=("101",))
    rows = [x for x in r.sun_daily if x["stn_id"] == "101"]
    assert len(rows) == 365 and rows[0]["date"] == "2026-01-01" and rows[-1]["date"] == "2026-12-31"
    for x in rows:
        assert x["rs_day_mj"] is None and x["rs_cum_mj"] is None
        assert x["rs_normal_day_mj"] is None and x["rs_normal_cum_mj"] is None
        assert x["filled"] is True and x["normal_from"] == 2016 and x["normal_to"] == 2025
    assert check_rows("sun_daily", rows) == []
    assert next(x for x in r.stations if x["stn_id"] == "101")["sun"] == "자료 없음"


def test_station_that_never_reports_radiation_still_gets_no_rows():
    """일사 칸이 한 번도 없던 지점은 지금처럼 줄을 만들지 않는다 (덮어쓸 옛 줄이 없다)"""
    r, _, _ = _run(zero_gsr=("101",))  # 태백(216)은 no_gsr 기본값
    assert not [x for x in r.sun_daily if x["stn_id"] == "216"]


def test_empty_overwrite_rows_are_upserted_not_deleted():
    r, _, _ = _run(zero_gsr=("101",))
    calls = []

    class W:
        def upsert(self, table, rows, on_conflict):
            calls.append((table, len(rows)))

    H.publish(r, W())
    assert r.uploaded and ("sun_daily", 365) in calls
    assert not hasattr(W, "delete")


def test_invalid_month_this_year_is_filled_with_normal_not_zero():
    """올해 5월만 일사가 0.0 이면 그 달 날들은 무효 → 평년 값으로 채우고(filled), 0.0 을 쌓지 않는다"""
    may = [dt.date(2026, 5, d) for d in range(1, 32)]
    r, _, _ = _run(zero_days=may)
    rows = {x["date"]: x for x in r.sun_daily if x["stn_id"] == "101"}
    x = rows["2026-05-10"]
    assert x["filled"] is True and x["rs_day_mj"] == x["rs_normal_day_mj"] and x["rs_day_mj"] > 0
    ok = rows["2026-04-10"]
    assert ok["filled"] is False and ok["rs_day_mj"] == 10 + dt.date(2026, 4, 10).timetuple().tm_yday % 7


def test_healthy_station_sun_rows_are_unchanged_by_the_rule():
    """정상 지점: 규칙이 닿지 않아 값이 지금과 같다 (일사 10~16 MJ 는 모든 날 하한 위)"""
    r, _, _ = _run()
    rows = [x for x in r.sun_daily if x["stn_id"] == "101" and x["date"] <= "2026-10-24"]
    assert all(x["filled"] is False for x in rows)
    assert all(x["rs_day_mj"] == 10 + dt.date.fromisoformat(x["date"]).timetuple().tm_yday % 7 for x in rows)
