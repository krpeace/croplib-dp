"""적산온도·적산일사 수집기 (collectors/heat_sun.py).

가짜 ASOS·가짜 시계·가짜 DB 쓰기로 돈다. 입력은 모두 가짜 값이다.
"""

import datetime as dt
import json
import urllib.parse

import pandas as pd

from croplib_dp.collectors import heat_sun as H
from croplib_dp.io.contract import check_rows
from croplib_dp.io.dictionaries import Dictionaries

KEY = "SECRET+KEY/123=="
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
    def __init__(self, missing=(), no_gsr=("216",), fail_stn=None):
        self.missing, self.no_gsr, self.fail_stn = set(missing), set(no_gsr), fail_stn
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
            if d0 + dt.timedelta(days=k) not in self.missing
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
    st = lambda i, n: dict(stn_id=i, name_ko=n, lat=37.0, lon=127.0, elev_m=50, wind_height_m=10)  # noqa: E731
    crops = [
        dict(crop_id="apple", name_ko="사과", t_base_c=4, gdd_stages=[500, 1000]),
        dict(crop_id="kimchi_cabbage", name_ko="배추", t_base_c=4, gdd_stages=[300]),
    ]
    return Dictionaries.from_rows(
        dict(
            stations=[st("101", "춘천"), st("216", "태백")],
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
