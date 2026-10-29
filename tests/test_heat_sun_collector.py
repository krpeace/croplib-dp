"""적산온도·적산일사 수집기 (collectors/heat_sun.py).

가짜 ASOS·가짜 시계·가짜 DB 쓰기로 돈다. 입력은 모두 가짜 값이다.
"""

import datetime as dt
import json
import urllib.parse

import pandas as pd

from croplib_dp.collectors import heat_sun as H
from croplib_dp.io.asos_cache import AsosCache
from croplib_dp.io.contract import check_rows
from croplib_dp.io.dictionaries import Dictionaries

KEY = "FAKE+KEY/dummy-value=="
TODAY = pd.Timestamp("2026-10-25 09:20")


def _item(d: dt.date, gsr=True) -> dict:
    doy = d.timetuple().tm_yday
    warm = 12 + 12 * (1 - abs(doy - 200) / 200)  # 여름에 따뜻한 가짜 기온
    bump = 1.0 if d.year == 2026 else 0.0  # 올해는 평년보다 1도 따뜻
    return dict(
        tm=d.isoformat(),
        maxTa=f"{warm + 5 + bump:.1f}",
        minTa=f"{warm - 5 + bump:.1f}",
        avgTa=f"{warm + bump:.1f}",
        sumGsr=(f"{10 + doy % 7}" if gsr else ""),
    )


class FakeAsos:
    def __init__(self, missing=(), no_gsr=("216",), fail_stn=None, nodata_years=()):
        self.missing, self.no_gsr, self.fail_stn = set(missing), set(no_gsr), fail_stn
        self.nodata_years = set(nodata_years)  # 이 해는 관측이 없다 (nodata 응답)
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, url, timeout):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert q["serviceKey"] == KEY
        stn, s, e = q["stnIds"], q["startDt"], q["endDt"]
        self.calls.append((stn, s, e))
        if stn == self.fail_stn:
            return 500, b"server error"
        d0, d1 = (
            dt.date.fromisoformat(f"{s[:4]}-{s[4:6]}-{s[6:]}"),
            dt.date.fromisoformat(f"{e[:4]}-{e[4:6]}-{e[6:]}"),
        )
        items = [
            _item(d0 + dt.timedelta(days=k), gsr=stn not in self.no_gsr)
            for k in range((d1 - d0).days + 1)
            if d0 + dt.timedelta(days=k) not in self.missing and d0.year not in self.nodata_years
        ]
        page, rows = int(q["pageNo"]), int(q["numOfRows"])
        part = items[(page - 1) * rows : page * rows]
        body = {"items": {"item": part}, "totalCount": len(items)}
        return 200, json.dumps(
            {"response": {"header": {"resultCode": "00", "resultMsg": "OK"}, "body": body}}
        ).encode()


class FakeClock:
    def __init__(self, t):
        self.t = pd.Timestamp(t)
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept += s


def _dicts() -> Dictionaries:
    st = lambda i, n, act=True: dict(  # noqa: E731
        stn_id=i, name_ko=n, lat=37.0, lon=127.0, elev_m=50, wind_height_m=10, is_active=act
    )
    crops = [
        dict(crop_id="apple", name_ko="사과", t_base_c=4, gdd_stages=[500, 1000]),
        dict(crop_id="kimchi_cabbage", name_ko="배추", t_base_c=4, gdd_stages=[300]),
    ]
    return Dictionaries.from_rows(
        dict(
            stations=[st("101", "춘천"), st("216", "태백"), st("999", "폐지지점", act=False)],
            crops=crops,
            crop_seasons=[],
            crop_kc_sets=[],
            soil_types=[],
            scenarios=[],
        )
    )


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
