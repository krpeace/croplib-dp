"""적산 계열 (이슈 #127 · #135, 결정 0038 · 0041): heat_series_daily · sun_series_daily 에만 쓰기.

값을 가르는 것은 작물이 아니라 기준온도와 누적 시작 월-일 둘뿐이다(적산 계열).
뷰 `crop_accum_series`에서 계열 목록을 읽고 새 표 둘에 쓴다.
3단계(#135)부터 옛 표(heat_daily·sun_daily)에는 쓰지 않는다.
가짜 ASOS·가짜 시계·가짜 DB 쓰기로 돈다. 입력은 모두 가짜 값이다.
"""

import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent / "data"))

from heat_sun_fakes import KEY, FakeAsos, FakeClock, _dicts, dicts_with_common  # noqa: E402

from croplib_dp.collectors import heat_sun as H  # noqa: E402
from croplib_dp.core import heat_sun as C  # noqa: E402
from croplib_dp.io.contract import SPECS, check_rows  # noqa: E402

TODAY = pd.Timestamp("2026-10-25 09:20")  # 어제 = 2026-10-24
Y = dt.date(2026, 10, 24)


def view(crop, season, tb, hstart, sstart):
    return dict(
        crop_id=crop,
        season_type_id=season,
        heat_t_base_c=tb,
        heat_cum_start_mmdd=hstart,
        sun_cum_start_mmdd=sstart,
    )


# 운영 뷰 crop_accum_series 5줄 (CropMon v2.2). 기준온도 5 와 5.0 이 섞여 온다(같은 계열).
VIEW_ROWS = [
    view("apple", "apple-main", 4, "01-01", "01-01"),
    view("common", None, 5, "01-01", "01-01"),
    view("grape", "grape-heated", None, None, "01-01"),  # 시설가온: 적산온도를 내지 않는다
    view("grape", "grape-rainshelter", 10, "04-20", "04-20"),
    view("kimchi_cabbage", "kimchi_cabbage-summer", 5.0, "01-01", "01-01"),
]


def _series(rows=VIEW_ROWS):
    s, probs = H.build_series(rows)
    assert probs == []
    return s


def _run(series=None, when=TODAY, d=None, **kw):
    api = FakeAsos(**kw)
    r = H.run(d or dicts_with_common(), KEY, http=api, clock=FakeClock(when), series=series)
    return r, api


@pytest.fixture(scope="module")
def base():
    return _run(series=_series())[0]


def _sel(rows, year=None, **where):
    return [
        x
        for x in rows
        if (year is None or x["date"].startswith(f"{year}-")) and all(x[k] == v for k, v in where.items())
    ]


# ── ① · ② year_table 의 누적 시작 ───────────────────────────────────────
def _flat(per_day, jan_to_apr19=None):
    """평년: 모든 날 per_day. jan_to_apr19 를 주면 1/1~4/19 는 그 값(시작이 정말 4/20 인지 가르는 용도)"""
    out = {}
    d = dt.date(2026, 1, 1)
    while d.year == 2026:
        out[(d.month, d.day)] = (
            jan_to_apr19 if jan_to_apr19 is not None and d < dt.date(2026, 4, 20) else per_day
        )
        d += dt.timedelta(days=1)
    return out


def test_start_jan_1_is_the_same_as_the_default_and_keeps_the_whole_year():
    obs = {dt.date(2026, 1, 1) + dt.timedelta(days=k): 3.0 for k in range(100)}
    normal = _flat(2.0)
    a = C.year_table(2026, obs, normal, dt.date(2026, 4, 10))
    b = C.year_table(2026, obs, normal, dt.date(2026, 4, 10), start=(1, 1))
    assert a == b
    assert len(a) == 365 and a[0]["date"] == dt.date(2026, 1, 1) and a[-1]["date"] == dt.date(2026, 12, 31)
    assert a[0]["cum"] == 3.0 and a[9]["cum"] == 30.0 and a[9]["normal_cum"] == 20.0


def test_start_apr_20_first_row_is_apr_20_and_cum_starts_from_the_start_day():
    normal = _flat(2.0, jan_to_apr19=10.0)  # 4/20 이전 평년이 커서, 시작이 틀리면 평년 누적이 크게 어긋난다
    obs = {dt.date(2026, 4, 20) + dt.timedelta(days=k): 4.0 for k in range(10)}
    rows = C.year_table(2026, obs, normal, dt.date(2026, 4, 29), start=(4, 20))
    assert rows[0]["date"] == dt.date(2026, 4, 20) and rows[-1]["date"] == dt.date(2026, 12, 31)
    assert len(rows) == 256  # 4/20 ~ 12/31
    first = rows[0]
    assert first["cum"] == first["day"] == 4.0  # 첫 줄 누적 = 그날 일값
    assert first["normal_cum"] == 2.0  # 평년 누적도 4/20 부터
    assert rows[9]["cum"] == 40.0 and rows[9]["normal_cum"] == 20.0
    assert all(r["date"] >= dt.date(2026, 4, 20) for r in rows)


def test_start_apr_20_lead_days_is_counted_from_apr_20():
    # 올해 하루 4, 평년 하루 2 → 4/20 부터 세면 첫날 누적 4 에 평년이 이틀째(인덱스 1)에 닿아 +1일 빠름.
    # 1/1 부터 평년 누적을 세면(평년 1~4월이 아주 크므로) 평년이 올해보다 앞서 −로 나온다.
    normal = _flat(2.0, jan_to_apr19=10.0)
    obs = {dt.date(2026, 4, 20) + dt.timedelta(days=k): 4.0 for k in range(5)}
    rows = C.year_table(2026, obs, normal, dt.date(2026, 4, 24), start=(4, 20))
    assert [r["lead"] for r in rows[:5]] == [1, 2, 3, 4, 5]
    assert rows[5]["cum"] is None and rows[5]["lead"] is None  # 마지막 관측 뒤는 올해 값이 비어 있다


def test_start_after_last_obs_makes_only_the_normal_line():
    normal = _flat(2.0)
    rows = C.year_table(2026, {}, normal, dt.date(2026, 4, 1), start=(4, 20))
    assert len(rows) == 256
    assert all(r["cum"] is None and r["day"] is None and r["lead"] is None for r in rows)
    assert rows[0]["normal_cum"] == 2.0 and rows[-1]["normal_cum"] == 2.0 * 256


def test_start_apr_20_missing_day_is_filled_with_the_normal():
    normal = _flat(2.0)
    obs = {dt.date(2026, 4, 20): 4.0, dt.date(2026, 4, 22): 4.0}  # 4/21 결측
    rows = C.year_table(2026, obs, normal, dt.date(2026, 4, 22), start=(4, 20))
    assert rows[1]["filled"] is True and rows[1]["day"] == 2.0 and rows[2]["cum"] == 10.0


# ── MM-DD 읽기 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("s,want", [("01-01", (1, 1)), ("04-20", (4, 20)), ("12-31", (12, 31))])
def test_parse_mmdd_accepts_month_day_text(s, want):
    assert C.parse_mmdd(s) == want


@pytest.mark.parametrize("bad", ["4-20", "13-01", "00-10", "04-31", "02-29", "0420", "", None, 420, "04/20"])
def test_parse_mmdd_rejects_anything_else(bad):
    with pytest.raises(ValueError):
        C.parse_mmdd(bad)


# ── ③ 계열 묶기: 4 와 4.0 은 한 계열 ───────────────────────────────────────
def test_group_heat_series_merges_4_and_4_point_0_and_decimal():
    rows = [
        view("a", "a-1", 4, "01-01", "01-01"),
        view("b", "b-1", 4.0, "01-01", "01-01"),
        view("c", "c-1", Decimal("4.00"), "01-01", "01-01"),
        view("d", "d-1", 4.0, "04-20", "04-20"),  # 기준온도는 같아도 시작이 다르면 다른 계열
    ]
    series, problems = C.group_heat_series(rows)
    assert problems == []
    assert [(float(t), m) for t, m in series] == [(4.0, "01-01"), (4.0, "04-20")]


def test_group_heat_series_keeps_the_value_received_from_the_view():
    series, _ = C.group_heat_series(
        [view("a", "a-1", 4, "01-01", "01-01"), view("b", "b-1", 4.0, "01-01", None)]
    )
    assert series == [(4, "01-01")] and isinstance(
        series[0][0], int
    )  # 처음 받은 값 그대로 (4.0 으로 바꾸지 않음)
    series, _ = C.group_heat_series([view("k", "k-1", 5.0, "01-01", "01-01")])
    assert series == [(5.0, "01-01")] and isinstance(series[0][0], float)


def test_group_heat_series_reads_numeric_text_as_a_number():
    series, problems = C.group_heat_series([view("g", "g-1", "10", "04-20", "04-20")])
    assert problems == [] and series == [(10.0, "04-20")]
    assert json.dumps(series)  # JSON 으로 보낼 수 있다


def test_group_heat_series_sorted_and_deterministic():
    rows = list(reversed(VIEW_ROWS))
    series, problems = C.group_heat_series(rows)
    assert problems == []
    assert [(float(t), m) for t, m in series] == [(4.0, "01-01"), (5.0, "01-01"), (10.0, "04-20")]


# ── ④ 시설가온: heat_* 가 빈 줄은 적산온도 계열을 만들지 않는다 ─────────────────
def test_heated_season_makes_no_heat_series_but_keeps_its_sun_series():
    rows = [view("grape", "grape-heated", None, None, "01-01")]
    assert C.group_heat_series(rows) == ([], [])
    assert C.group_sun_series(rows) == (["01-01"], [])


def test_group_sun_series_distinct_sorted():
    sun, problems = C.group_sun_series(VIEW_ROWS)
    assert problems == [] and sun == ["01-01", "04-20"]


def test_malformed_start_is_reported_and_skipped_not_silent():
    rows = [
        view("x", "x-1", 4, "4-20", "13-40"),
        view("y", "y-1", 4, "01-01", "01-01"),
    ]
    heat, hp = C.group_heat_series(rows)
    sun, sp = C.group_sun_series(rows)
    assert heat == [(4, "01-01")] and sun == ["01-01"]
    assert any("crop_accum_series" in p and "4-20" in p for p in hp)
    assert any("crop_accum_series" in p and "13-40" in p for p in sp)


def test_build_series_from_the_operating_view():
    s = _series()
    assert [(float(t), m) for t, m in s.heat] == [(4.0, "01-01"), (5.0, "01-01"), (10.0, "04-20")]
    assert s.sun == ["01-01", "04-20"]


def test_build_series_empty_view_is_a_problem():
    s, probs = H.build_series([])
    assert s is None and any("crop_accum_series" in p and "비어" in p for p in probs)


def test_build_series_with_no_usable_series_is_a_problem():
    s, probs = H.build_series([view("x", "x-1", None, None, None)])
    assert s is None and any("crop_accum_series" in p for p in probs)


# ── 새 표 줄 만들기 ───────────────────────────────────────────────────────
def test_series_rows_per_station_and_series(base):
    assert not base.failed, base.problems
    # 운영 지점 2곳(101·216) × (4,01-01)·(5,01-01)·(10,04-20) = 365 + 365 + 256
    assert len(base.heat_series_daily) == 2 * (365 + 365 + 256)
    for stn in ("101", "216"):
        assert len(_sel(base.heat_series_daily, stn_id=stn, t_base_c=4, cum_start_mmdd="01-01")) == 365
        assert len(_sel(base.heat_series_daily, stn_id=stn, t_base_c=5, cum_start_mmdd="01-01")) == 365
        assert len(_sel(base.heat_series_daily, stn_id=stn, t_base_c=10, cum_start_mmdd="04-20")) == 256
    assert all("crop_id" not in x for x in base.heat_series_daily)
    assert {x["cum_start_mmdd"] for x in base.heat_series_daily} == {"01-01", "04-20"}


def test_apr_20_heat_series_starts_on_apr_20_and_cum_starts_there(base):
    rows = _sel(base.heat_series_daily, stn_id="101", t_base_c=10, cum_start_mmdd="04-20")
    assert rows[0]["date"] == "2026-04-20" and rows[-1]["date"] == "2026-12-31"
    assert min(x["date"] for x in rows) == "2026-04-20"
    assert rows[0]["gdd_cum"] == rows[0]["gdd_day"]  # 첫 줄 누적 = 그날 일값
    assert rows[0]["gdd_normal_cum"] == pytest.approx(
        rows[0]["gdd_normal_day"], abs=0.05
    )  # 저장 반올림(0.1·0.01)
    last_obs = next(x for x in rows if x["date"] == "2026-10-24")
    assert (
        last_obs["gdd_cum"] > last_obs["gdd_normal_cum"] and last_obs["lead_days"] > 0
    )  # 올해가 1도 따뜻 → 빠름
    assert next(x for x in rows if x["date"] == "2026-10-25")["gdd_cum"] is None


def test_daily_values_do_not_depend_on_the_start(base):
    """같은 기준온도·같은 날: 일값·평년 일값은 시작이 달라도 같다. 누적만 다르다."""
    jan = {
        x["date"]: x for x in _sel(base.heat_series_daily, stn_id="101", t_base_c=5, cum_start_mmdd="01-01")
    }
    # 기준온도 5 의 4/20 계열은 뷰에 없으므로 따로 만든다
    s = H.build_series([view("z", "z-1", 5, "04-20", "04-20")])[0]
    r, _ = _run(series=s)
    apr = {x["date"]: x for x in _sel(r.heat_series_daily, stn_id="101", t_base_c=5, cum_start_mmdd="04-20")}
    assert apr and min(apr) == "2026-04-20"
    for d, x in apr.items():
        assert x["gdd_day"] == jan[d]["gdd_day"] and x["gdd_normal_day"] == jan[d]["gdd_normal_day"]
    assert apr["2026-06-01"]["gdd_cum"] < jan["2026-06-01"]["gdd_cum"]  # 누적은 4/20 부터라 더 작다


def test_sun_series_rows_only_where_sunlight_is_measured(base):
    assert {x["stn_id"] for x in base.sun_series_daily} == {"101"}  # 태백(216)은 일사를 재지 않는다
    assert len(_sel(base.sun_series_daily, cum_start_mmdd="01-01")) == 365
    apr = _sel(base.sun_series_daily, cum_start_mmdd="04-20")
    assert len(apr) == 256 and apr[0]["date"] == "2026-04-20"
    assert apr[0]["rs_cum_mj"] == apr[0]["rs_day_mj"]  # 첫 줄 누적 = 그날 일값
    assert apr[0]["rs_normal_cum_mj"] == apr[0]["rs_normal_day_mj"]
    assert all("crop_id" not in x for x in base.sun_series_daily)


# ── ⑤ (5, 01-01) 새 줄 = crop_id=common 옛 줄, 일사 01-01 = sun_daily ──────────────
def test_t_base_5_jan_1_series_equals_the_common_crop_old_rows(base):
    for stn in ("101", "216"):
        old = {x["date"]: x for x in _sel(base.heat_daily, stn_id=stn, crop_id="common")}
        new = {
            x["date"]: x for x in _sel(base.heat_series_daily, stn_id=stn, t_base_c=5, cum_start_mmdd="01-01")
        }
        assert old and set(old) == set(new)
        for d, o in old.items():
            n = dict(new[d])
            assert n.pop("cum_start_mmdd") == "01-01"
            o = dict(o)
            o.pop("crop_id")
            assert n == o  # 값 열 전부 (t_base_c 5 == 5.0 포함)


def test_sun_series_jan_1_equals_sun_daily(base):
    old = {(x["stn_id"], x["date"]): x for x in base.sun_daily}
    new = {(x["stn_id"], x["date"]): x for x in _sel(base.sun_series_daily, cum_start_mmdd="01-01")}
    assert old and set(old) == set(new)
    for k, o in old.items():
        n = dict(new[k])
        assert n.pop("cum_start_mmdd") == "01-01"
        assert n == o


def test_old_tables_are_unchanged_by_the_series(base):
    """옛 줄은 계열을 넘겨도 안 넘겨도 똑같다(옛 표 쓰기는 그대로)"""
    without, _ = _run(series=None)
    assert base.heat_daily == without.heat_daily and base.sun_daily == without.sun_daily
    assert without.heat_series_daily == [] and without.sun_series_daily == []
    assert base.stations == without.stations


def test_same_t_base_daily_values_are_calculated_once(monkeypatch):
    """규칙 4: 같은 기준온도의 일값은 한 번만 계산한다 (사과·배추가 같은 4℃, 시리즈에 4℃도 있음)"""
    calls = []
    real = H.calc_gdd
    monkeypatch.setattr(H, "calc_gdd", lambda *a, **k: calls.append(a[3]) or real(*a, **k))
    n_days = (Y - dt.date(2016, 1, 1)).days + 1  # 지점마다 받는 일자료 날 수
    _run(series=None, d=_dicts())  # 4℃ 두 작물
    assert len(calls) == 2 * n_days  # 운영 2지점 × 기준온도 4℃ 한 번 (옛 방식은 사과·배추로 두 번)
    calls.clear()
    _run(series=_series(), d=_dicts())  # 4℃ · 5℃ · 10℃
    assert len(calls) == 2 * 3 * n_days  # 2지점 × 기준온도 3개


def test_series_rows_pass_the_contract_and_serialize_to_json(base):
    assert check_rows("heat_series_daily", base.heat_series_daily) == []
    assert check_rows("sun_series_daily", base.sun_series_daily) == []
    assert H.check(base) == []
    dumped = json.loads(
        json.dumps(base.heat_series_daily + base.sun_series_daily)
    )  # Decimal 등 직렬화 불가가 없다
    assert len(dumped) == len(base.heat_series_daily) + len(base.sun_series_daily)
    tb = {x["t_base_c"] for x in base.heat_series_daily}
    assert tb == {4, 5, 10} and all(isinstance(t, int | float) for t in tb)
    assert {type(x["t_base_c"]) for x in _sel(base.heat_series_daily, cum_start_mmdd="04-20")} == {
        int
    }  # 뷰 값 그대로


def test_contract_specs_have_the_new_tables_with_new_keys():
    h, s = SPECS["heat_series_daily"], SPECS["sun_series_daily"]
    assert h.key == ("stn_id", "t_base_c", "cum_start_mmdd", "date")
    assert s.key == ("stn_id", "cum_start_mmdd", "date")
    assert "crop_id" not in h.columns and "cum_start_mmdd" in h.columns and "cum_start_mmdd" in s.columns
    assert "cum_start_mmdd" in h.required and "cum_start_mmdd" in s.required
    old_h, old_s = SPECS["heat_daily"], SPECS["sun_daily"]
    assert [c for c in h.columns if c not in ("t_base_c", "cum_start_mmdd")] == [
        c for c in old_h.columns if c not in ("crop_id", "t_base_c")
    ]
    assert [c for c in s.columns if c != "cum_start_mmdd"] == list(old_s.columns)
    assert h.ranges == old_h.ranges and s.ranges == old_s.ranges


def test_contract_catches_duplicate_keys_and_bad_values_in_series_rows(base):
    rows = [dict(x) for x in base.heat_series_daily[:3]]
    rows.append(dict(rows[0], t_base_c=float(rows[0]["t_base_c"])))  # 4 와 4.0 은 같은 키
    rows[1]["gdd_day"] = -5.0
    probs = check_rows("heat_series_daily", rows)
    assert any("같은 키" in p for p in probs) and any("gdd_day" in p for p in probs)
    del rows[2]["cum_start_mmdd"]
    assert any("cum_start_mmdd" in p for p in check_rows("heat_series_daily", rows))
    assert any(
        "cum_start_mmdd" in p
        for p in check_rows(
            "sun_series_daily", [{k: v for k, v in base.sun_series_daily[0].items() if k != "cum_start_mmdd"}]
        )
    )


# ── ⑥ 빈 줄 날 · 새해 · 일사 미측정이 새 표에도 같게 ─────────────────────────────
BLANK = ("maxTa", "minTa", "avgTa", "avgRhm", "avgWs", "sumGsr", "sumRn")


class BlankDaysAsos(FakeAsos):
    """지정한 날은 날짜만 있고 값이 모두 빈 줄 (기상청이 값을 거둬들인 날)"""

    def __init__(self, blank_days=(), **kw):
        super().__init__(**kw)
        self.blank_days = {d.isoformat() for d in blank_days}

    def __call__(self, url, timeout):
        code, body = super().__call__(url, timeout)
        doc = json.loads(body)
        items = doc.get("response", {}).get("body", {}).get("items", {}).get("item")
        for it in items or []:
            if it["tm"] in self.blank_days:
                it.update({k: "" for k in BLANK})
        return code, json.dumps(doc).encode()


def _blank_run(blank, when=TODAY):
    return H.run(
        dicts_with_common(),
        KEY,
        http=BlankDaysAsos(blank_days=blank),
        clock=FakeClock(when),
        series=_series(),
    )


def test_yesterday_blank_row_is_not_written_to_the_series_tables_either():
    r = _blank_run([Y])
    heat_days = {x["date"] for x in _sel(r.heat_series_daily, stn_id="101")}
    sun_days = {x["date"] for x in r.sun_series_daily}
    assert "2026-10-24" not in heat_days and "2026-10-24" not in sun_days
    assert "2026-10-23" in heat_days and "2026-10-23" in sun_days
    assert "2026-10-24" not in {x["date"] for x in _sel(r.heat_daily, stn_id="101")}  # 옛 표와 같음


def test_blank_day_in_the_middle_is_a_normal_filled_row_in_the_series_tables():
    mid = dt.date(2026, 6, 10)
    r = _blank_run([mid])
    apr = {x["date"]: x for x in _sel(r.heat_series_daily, stn_id="101", t_base_c=10, cum_start_mmdd="04-20")}
    x = apr["2026-06-10"]
    assert x["filled"] is True and x["gdd_day"] == x["gdd_normal_day"]
    sun = {x["date"]: x for x in _sel(r.sun_series_daily, cum_start_mmdd="04-20")}
    assert sun["2026-06-10"]["filled"] is True
    assert all(v["gdd_cum"] is not None for d, v in apr.items() if d <= "2026-10-23")


def test_new_year_day_closes_last_year_and_makes_only_the_normal_line_for_the_new_year():
    r, api = _run(series=_series(), when="2027-01-01 09:20")
    assert not r.failed and api.reversed_calls == []
    last = _sel(r.heat_series_daily, 2026, stn_id="101", t_base_c=10, cum_start_mmdd="04-20")
    assert len(last) == 256 and last[-1]["date"] == "2026-12-31" and last[-1]["gdd_cum"] is not None
    new = _sel(r.heat_series_daily, 2027, stn_id="101", t_base_c=10, cum_start_mmdd="04-20")
    assert len(new) == 256 and new[0]["date"] == "2027-04-20"
    assert all(x["gdd_cum"] is None and x["gdd_normal_cum"] is not None for x in new)
    assert all((x["normal_from"], x["normal_to"]) == (2017, 2026) for x in new)
    sun_new = _sel(r.sun_series_daily, 2027, stn_id="101", cum_start_mmdd="04-20")
    assert len(sun_new) == 256 and all(x["rs_cum_mj"] is None for x in sun_new)
    assert H.check(r) == []


@pytest.mark.parametrize("when,last_obs", [("2027-01-02 09:20", "01-01"), ("2027-01-03 09:20", "01-02")])
def test_jan_2_and_3_new_year_series_has_no_observation_before_the_start(when, last_obs):
    r, _ = _run(series=_series(), when=when)
    jan = _sel(r.heat_series_daily, 2027, stn_id="101", t_base_c=5, cum_start_mmdd="01-01")
    assert [x["date"] for x in jan if x["gdd_cum"] is not None][-1] == f"2027-{last_obs}"
    apr = _sel(r.heat_series_daily, 2027, stn_id="101", t_base_c=10, cum_start_mmdd="04-20")
    assert len(apr) == 256 and all(x["gdd_cum"] is None for x in apr)  # 4/20 전이라 아직 쌓을 것이 없다


def test_jan_4_only_this_year_in_the_series_tables():
    r, _ = _run(series=_series(), when="2027-01-04 09:20")
    assert _sel(r.heat_series_daily, 2026) == [] and _sel(r.sun_series_daily, 2026) == []


def test_station_with_only_zero_radiation_gets_empty_series_rows_for_every_start():
    r, _ = _run(series=_series(), zero_gsr=("101",))
    for mmdd, n in (("01-01", 365), ("04-20", 256)):
        rows = _sel(r.sun_series_daily, stn_id="101", cum_start_mmdd=mmdd)
        assert len(rows) == n
        for x in rows:
            assert x["rs_day_mj"] is None and x["rs_cum_mj"] is None and x["filled"] is True
            assert x["rs_normal_day_mj"] is None and x["rs_normal_cum_mj"] is None
    assert _sel(r.sun_series_daily, stn_id="101", cum_start_mmdd="04-20")[0]["date"] == "2026-04-20"
    assert check_rows("sun_series_daily", r.sun_series_daily) == []
    # 옛 표의 빈 값 줄과 같은 줄이다 (01-01)
    assert len(_sel(r.sun_daily, stn_id="101")) == 365


def test_station_that_never_reports_radiation_gets_no_series_rows_either():
    r, _ = _run(series=_series(), zero_gsr=("101",))
    assert not [x for x in r.sun_series_daily if x["stn_id"] == "216"]


# ── 쓰기 순서 · 실패 ─────────────────────────────────────────────────────
class W:
    def __init__(self, fail_on=()):
        self.calls, self.fail_on = [], set(fail_on)

    def upsert(self, table, rows, on_conflict, returning=False):
        rows = list(rows)
        if table in self.fail_on:
            raise RuntimeError(f"HTTP 500 {table}")
        self.calls.append((table, len(rows), tuple(on_conflict)))
        return []


def test_publish_writes_only_the_new_tables():
    r = _run(series=_series())[0]
    w = W()
    H.publish(r, w)
    assert [c[0] for c in w.calls] == ["heat_series_daily", "sun_series_daily"]  # 옛 표 이름은 없다
    keys = {t: k for t, _, k in w.calls}
    assert keys["heat_series_daily"] == ("stn_id", "t_base_c", "cum_start_mmdd", "date")
    assert keys["sun_series_daily"] == ("stn_id", "cum_start_mmdd", "date")
    assert r.uploaded and not r.failed
    counts = {t: n for t, n, _ in w.calls}
    assert counts["heat_series_daily"] == 2 * 986 and counts["sun_series_daily"] == 365 + 256


def test_publish_without_series_writes_nothing_and_fails_loudly():
    r = _run(series=None)[0]
    w = W()
    H.publish(r, w)
    assert w.calls == [] and not r.uploaded and r.failed
    assert any("올릴 줄이 없음" in p for p in r.problems)


def test_new_table_write_failure_is_recorded():
    r = _run(series=_series())[0]
    w = W(fail_on=("heat_series_daily",))
    H.publish(r, w)
    assert w.calls == [] and not r.uploaded
    assert r.failed and any("heat_series_daily" in p and "실패" in p for p in r.problems)


def test_second_new_table_write_failure_is_recorded():
    r = _run(series=_series())[0]
    w = W(fail_on=("sun_series_daily",))
    H.publish(r, w)
    assert [c[0] for c in w.calls] == ["heat_series_daily"] and not r.uploaded
    assert r.failed and any("sun_series_daily" in p and "실패" in p for p in r.problems)


def test_new_table_contract_problem_uploads_nothing_and_is_recorded():
    r = _run(series=_series())[0]
    r.heat_series_daily[0]["gdd_day"] = -5.0
    w = W()
    H.publish(r, w)
    assert w.calls == [] and not r.uploaded  # 아무것도 올리지 않는다
    assert r.failed and any("heat_series_daily" in p for p in r.problems)


def test_old_tables_are_never_checked_or_written_even_if_their_rows_are_bad():
    """옛 줄 모양 계산 결과는 메모리에만 있다. 그 값이 어긋나도 DB 로 가지 않으므로 올리기에 영향이 없다."""
    r = _run(series=_series())[0]
    r.heat_daily[0]["gdd_day"] = -5.0
    w = W()
    H.publish(r, w)
    assert [c[0] for c in w.calls] == ["heat_series_daily", "sun_series_daily"] and not r.failed


# ── 뷰 읽기 · main ──────────────────────────────────────────────────────
def test_read_series_rows_asks_the_view_with_only_needed_columns():
    seen = []

    def fake_read_table(url, key, table, http=None, query="select=*", schema=None):
        seen.append((table, query))
        return VIEW_ROWS

    rows = H.read_series_rows("https://x", "k", read=fake_read_table)
    assert rows == VIEW_ROWS
    table, query = seen[0]
    assert (
        table == "crop_accum_series" and "heat_t_base_c" in query and "limit=" in query and "*" not in query
    )


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

    class Writer:
        def __init__(self, *a):
            pass

        def upsert(self, table, rows, on_conflict, returning=False):
            writes.append((table, len(list(rows))))
            return []

    import croplib_dp.io.supabase_write as sw

    monkeypatch.setattr(sw, "SupabaseWriter", Writer)
    return dict(writes=writes, out=tmp_path / "out", monkeypatch=monkeypatch)


def _brief(app):
    return (app["out"] / "heat_sun_2026" / "brief.md").read_text(encoding="utf-8")


def test_main_writes_only_new_tables_and_reports_counts(app):
    app["monkeypatch"].setattr(H, "read_series_rows", lambda *a, **k: VIEW_ROWS)
    assert H.main(["--out", str(app["out"]), "--write"]) == 0
    assert [t for t, _ in app["writes"]] == ["heat_series_daily", "sun_series_daily"]
    text = _brief(app)
    assert "DB 올리기: 완료 · heat_series 1972줄 · sun_series 621줄" in text
    assert "heat 2190줄" not in text and "새 표" not in text  # 옛 표 줄 수는 더 내지 않는다
    for name in ("heat_series_daily", "sun_series_daily"):
        assert (app["out"] / "heat_sun_2026" / f"{name}.json").is_file()
    for name in ("heat_daily", "sun_daily"):  # 올리지 않는 옛 표 JSON 은 쓰지 않는다
        assert not (app["out"] / "heat_sun_2026" / f"{name}.json").exists()


def test_main_dry_run_checks_the_new_tables_without_writing(app):
    app["monkeypatch"].setattr(H, "read_series_rows", lambda *a, **k: VIEW_ROWS)
    assert H.main(["--out", str(app["out"])]) == 0
    assert app["writes"] == []


def test_main_view_read_failure_writes_nothing_and_fails_with_the_cause(app):
    def boom(*a, **k):
        raise RuntimeError("Supabase crop_accum_series 읽기 실패: HTTP 503")

    app["monkeypatch"].setattr(H, "read_series_rows", boom)
    assert H.main(["--out", str(app["out"]), "--write"]) == 1  # 조용히 넘기지 않는다
    assert app["writes"] == []
    text = _brief(app)
    assert "뷰 crop_accum_series" in text and "읽기 실패" in text and "올릴 것이 없어 회차 실패" in text


def test_main_empty_view_writes_nothing_and_fails_with_the_cause(app):
    app["monkeypatch"].setattr(H, "read_series_rows", lambda *a, **k: [])
    assert H.main(["--out", str(app["out"]), "--write"]) == 1
    assert app["writes"] == []
    text = _brief(app)
    assert "뷰 crop_accum_series" in text and "비어" in text and "올릴 것이 없음" in text


def test_main_dry_run_without_series_also_fails(app):
    app["monkeypatch"].setattr(H, "read_series_rows", lambda *a, **k: [])
    assert H.main(["--out", str(app["out"])]) == 1


# ── 병합 전후 같은 날 값: 알림 지점 줄은 글자까지 같다 (이슈 #135) ──────────────
# 옛 줄을 올리던 main(a1ff7b5) 에서 같은 가짜 입력(_series(), 가짜 ASOS)으로 떠 둔 지점 줄이다.
GOLDEN_STATION_LINES = {
    "2026-10-25 09:20": [
        "- 101 춘천: 관측 ~10-24 · 사과 4758/4462℃·일 평년보다 22일 빠름"
        " · 배추 4758/4462℃·일 평년보다 22일 빠름"
        " · 작물 공통 4461/4165℃·일 평년보다 24일 빠름 · 햇빛 100%",
        "- 216 태백: 관측 ~10-24 · 사과 4758/4462℃·일 평년보다 22일 빠름"
        " · 배추 4758/4462℃·일 평년보다 22일 빠름"
        " · 작물 공통 4461/4165℃·일 평년보다 24일 빠름 · 햇빛 자료 없음",
    ],
    # 1/1~3 경계: 작년 마감 기준 지점 줄 + 새해 줄
    "2027-01-02 09:20": [
        "- 101 춘천: 관측 ~12-31 · 사과 5649/5284℃·일 평년 미도달 · 배추 5649/5284℃·일 평년 미도달"
        " · 작물 공통 5284/4919℃·일 평년 미도달 · 햇빛 100%",
        "- 216 태백: 관측 ~12-31 · 사과 5649/5284℃·일 평년 미도달 · 배추 5649/5284℃·일 평년 미도달"
        " · 작물 공통 5284/4919℃·일 평년 미도달 · 햇빛 자료 없음",
    ],
}


@pytest.mark.parametrize("when", sorted(GOLDEN_STATION_LINES))
def test_brief_station_lines_are_unchanged_from_before_the_old_tables_stopped(when):
    r, _ = _run(series=_series(), when=pd.Timestamp(when))
    lines = [x for x in H.brief_text(r).splitlines() if x.startswith("- ")]
    assert lines == GOLDEN_STATION_LINES[when]


def test_brief_db_line_and_new_year_line_count_the_new_tables():
    r, _ = _run(series=_series())
    assert H.brief_text(r).splitlines()[-1] == "DB 올리기: 안 함 · heat_series 1972줄 · sun_series 621줄"
    r2, _ = _run(series=_series(), when=pd.Timestamp("2027-01-02 09:20"))
    text = H.brief_text(r2)
    assert text.splitlines()[-1] == "DB 올리기: 안 함 · heat_series 3944줄 · sun_series 1242줄"
    new_year = [x for x in text.splitlines() if x.startswith("새해 2027")]
    assert len(new_year) == 1
    heat = sum(1 for x in r2.heat_series_daily if x["date"].startswith("2027-"))
    sun = sum(1 for x in r2.sun_series_daily if x["date"].startswith("2027-"))
    assert new_year[0] == f"새해 2027: 관측 ~01-01까지 (heat {heat}줄 · sun {sun}줄)"
    assert heat == 1972 and sun == 621


# ── 알림 문구: 포도 ─────────────────────────────────────────────────────
def test_grape_is_shown_in_korean_in_the_brief():
    d = dicts_with_common()
    d.crops["grape"] = dict(crop_id="grape", name_ko="포도", t_base_c=10, gdd_stages=[370, 1040, 2050])
    r = H.run(d, KEY, http=FakeAsos(), clock=FakeClock(TODAY), series=_series())
    text = H.brief_text(r)
    assert "포도" in text and "grape " not in text
    assert H.CROP_KO["grape"] == "포도"
