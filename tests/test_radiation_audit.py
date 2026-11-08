"""일사 점검: 분류·실측 kRs·ETo 오차·경계 후보 비교·의심 일조 (core/·collectors/ radiation_audit.py).

이슈 #48 (#28 S0-b, PR-A). 입력은 모두 가짜 값이다. DB 쓰기 경로가 없어야 한다.
"""

import datetime as dt
import inspect
import json
import math
import urllib.parse

import pandas as pd
import pytest

from croplib_dp.collectors import radiation_audit as RA
from croplib_dp.core.daily_weather import StationMeta
from croplib_dp.core.fao56 import extra_radiation
from croplib_dp.core.radiation_audit import (
    CLASS_ALL_MISSING,
    CLASS_NO_RS_SUNSHINE,
    CLASS_NORMAL,
    CLASS_ZERO_INVALID,
    DEFAULT_CANDIDATES_KM,
    MIN_SEASON_DAYS,
    MIN_VALID_DAYS,
    audit_station,
    compare_boundaries,
)
from croplib_dp.core.solar import ra_rso, rs_s1
from croplib_dp.io.dictionaries import Dictionaries

LAT, ELEV = 37.5, 50.0
STN = StationMeta(lat=LAT, elev_m=ELEV, wind_height_m=10.0)
KEY = "FAKE+KEY/dummy-value=="


def _doy(d):
    return d.timetuple().tm_yday


def _days(start, n):
    return [start + dt.timedelta(days=k) for k in range(n)]


def _item(day, gsr="", ss="8.0", rain="", dT=10.0):
    """ASOS 하루 가짜 값. dT = 일교차(℃). 그 밖의 기상값은 고정."""
    warm = 12 + 12 * (1 - abs(_doy(day) - 200) / 200)
    return dict(
        tm=day.isoformat(),
        maxTa=f"{warm + dT / 2:.1f}",
        minTa=f"{warm - dT / 2:.1f}",
        avgTa=f"{warm:.1f}",
        avgRhm="60.0",
        minRhm="35.0",
        avgWs="2.0",
        avgPv="12.0",
        avgTd="9.0",
        avgPa="1000.0",
        sumGsr=gsr,
        sumSsHr=ss,
        sumRn=rain,
    )


def _ra(day):
    return extra_radiation(LAT, _doy(day))


def _krs_gsr(day, krs, dT=10.0):
    """식50 식 그대로(자르지 않음)의 일사 문자열"""
    return f"{krs * math.sqrt(dT) * _ra(day):.6f}"


def _clipped_gsr(day, krs, dT=10.0):
    """식50 + 하한·상한 자르기(core/solar.rs_s1)와 똑같은 값 → (나)·(다) 방식과 같은 일사"""
    item = _item(day, dT=dT)
    ra, rso = ra_rso(LAT, ELEV, _doy(day))
    v = rs_s1(float(item["maxTa"]), float(item["minTa"]), ra, rso, krs)[0]
    return f"{float(v):.9f}"


YEAR = _days(dt.date(2025, 11, 8), 365)  # 2025-11-08 ~ 2026-11-07 (지난 365일)


# ── 분류 ────────────────────────────────────────────────────────────────
def test_constants_follow_the_approved_plan():
    assert MIN_VALID_DAYS == 100 and MIN_SEASON_DAYS == 20
    assert DEFAULT_CANDIDATES_KM == (10, 20, 30, 50)  # 5 km 는 해상도 한계로 비교하지 않는다


def _classify(items):
    return audit_station(items, STN)


def test_normal_station_has_100_or_more_valid_days():
    days = _days(dt.date(2026, 3, 1), 120)
    a = _classify([_item(d, gsr="15.0") for d in days])
    assert a["klass"] == CLASS_NORMAL and a["rs_valid"] == 120 and a["rs_values"] == 120
    assert a["days_total"] == 120 and a["rs_zero"] == 0 and a["rs_sparse"] is False


def test_boundary_99_valid_days_is_not_normal_100_is():
    days = _days(dt.date(2026, 3, 1), 120)
    items99 = [_item(d, gsr=("15.0" if i < 99 else ""), ss="") for i, d in enumerate(days)]
    items100 = [_item(d, gsr=("15.0" if i < 100 else ""), ss="") for i, d in enumerate(days)]
    assert _classify(items99)["klass"] != CLASS_NORMAL
    assert _classify(items100)["klass"] == CLASS_NORMAL


def test_zero_valued_radiation_is_zero_invalid_class():
    """일사 값은 100일 넘게 있는데 0.0 이라 유효가 0일 → 0.0·무효"""
    days = _days(dt.date(2026, 3, 1), 120)
    a = _classify([_item(d, gsr="0.0") for d in days])
    assert a["klass"] == CLASS_ZERO_INVALID
    assert a["rs_values"] == 120 and a["rs_zero"] == 120 and a["rs_valid"] == 0


def test_below_floor_and_month_invalid_days_are_counted_apart():
    days = _days(dt.date(2026, 3, 1), 31)  # 3월 한 달
    items = [_item(d, gsr="0.0") for d in days[:28]]  # 0.0 28일
    items += [_item(days[28], gsr="0.02"), _item(days[29], gsr="15.0"), _item(days[30], gsr="15.0")]
    a = _classify(items)
    assert a["rs_zero"] == 28 and a["rs_below_floor"] == 1
    # 3월은 자료 31일 중 무효 29일(94%) → 월 규칙으로 정상처럼 보이는 2일도 무효
    assert a["rs_month_invalid"] == 2 and a["rs_valid"] == 0


def test_no_radiation_but_sunshine_is_its_own_class():
    days = _days(dt.date(2026, 3, 1), 120)
    a = _classify([_item(d, gsr="", ss="8.0") for d in days])
    assert a["klass"] == CLASS_NO_RS_SUNSHINE and a["ss_days"] == 120 and a["rs_values"] == 0


def test_nothing_at_all_is_all_missing_and_temperature_days_are_kept_apart():
    days = _days(dt.date(2026, 3, 1), 120)
    items = [_item(d, gsr="", ss="") for d in days]
    items[0]["maxTa"] = ""
    a = _classify(items)
    assert a["klass"] == CLASS_ALL_MISSING and a["ss_days"] == 0 and a["temp_days"] == 119


def test_sparse_radiation_is_flagged_and_not_treated_as_normal():
    """영주형: 일사 값이 1~2일뿐. '없음' 쪽(일조 있으면 일조 있음)으로 분류하고 sparse 표시"""
    days = _days(dt.date(2026, 3, 1), 120)
    items = [_item(d, gsr="", ss="8.0") for d in days]
    items[5]["sumGsr"] = "1.0"
    a = _classify(items)
    assert a["rs_sparse"] is True and a["rs_values"] == 1
    assert a["klass"] == CLASS_NO_RS_SUNSHINE


# ── 실측 kRs ────────────────────────────────────────────────────────────
def _season_krs(day):
    m = day.month
    return 0.15 if 3 <= m <= 5 else 0.17 if 6 <= m <= 8 else 0.18 if 9 <= m <= 11 else 0.16


def test_measured_krs_median_and_quartiles_recover_known_value():
    a = _classify([_item(d, gsr=_krs_gsr(d, 0.17)) for d in YEAR])
    assert a["klass"] == CLASS_NORMAL
    assert a["krs_n"] == 365
    assert a["krs_median"] == pytest.approx(0.17, abs=1e-4)
    assert a["krs_q1"] == pytest.approx(0.17, abs=1e-4) and a["krs_q3"] == pytest.approx(0.17, abs=1e-4)


def test_measured_krs_quartiles_spread_with_varying_values():
    days = YEAR[:364]  # 4로 나누어떨어지는 일수 → 중앙값 = 0.16 과 0.18 의 가운데
    vals = {
        d: (0.14 if i % 4 == 0 else 0.16 if i % 4 == 1 else 0.18 if i % 4 == 2 else 0.20)
        for i, d in enumerate(days)
    }
    a = _classify([_item(d, gsr=_krs_gsr(d, vals[d])) for d in days])
    assert a["krs_q1"] < a["krs_median"] < a["krs_q3"]
    assert a["krs_median"] == pytest.approx(0.17, abs=1e-4)


def test_seasonal_medians_split_by_season():
    a = _classify([_item(d, gsr=_krs_gsr(d, _season_krs(d))) for d in YEAR])
    s = a["krs_season"]
    assert list(s) == ["3~5", "6~8", "9~11", "12~2"]
    assert s["3~5"] == pytest.approx(0.15, abs=1e-4)
    assert s["6~8"] == pytest.approx(0.17, abs=1e-4)
    assert s["9~11"] == pytest.approx(0.18, abs=1e-4)
    assert s["12~2"] == pytest.approx(0.16, abs=1e-4)


def test_season_with_fewer_than_20_valid_days_is_left_blank():
    items = []
    for d in YEAR:
        keep = not (d.month in (6, 7, 8) and d.day > 6)  # 여름 유효일을 18일로 줄임(6~8월 각 6일)
        items.append(_item(d, gsr=(_krs_gsr(d, 0.17) if keep else ""), ss=""))
    a = _classify(items)
    assert a["klass"] == CLASS_NORMAL
    assert a["krs_season"]["6~8"] is None  # 18일 < 20일
    assert a["krs_season"]["3~5"] is not None


def test_season_with_exactly_20_valid_days_is_kept():
    items = []
    for d in YEAR:
        summer = d.month in (6, 7, 8)
        keep = (
            (not summer) or (d.day <= 6) or (d.month == 6 and d.day <= 8)
        )  # 6월 8일 + 7월 6일 + 8월 6일 = 20일
        items.append(_item(d, gsr=(_krs_gsr(d, 0.17) if keep else ""), ss=""))
    a = _classify(items)
    assert a["krs_season"]["6~8"] == pytest.approx(0.17, abs=1e-4)


def test_non_normal_station_has_no_krs_and_no_eto_errors():
    days = _days(dt.date(2026, 3, 1), 120)
    a = _classify([_item(d, gsr="0.0") for d in days])
    assert a["krs_median"] is None and a["eto"] is None


# ── ETo 오차 (가: 관측 일사, 나: 기온차 kRs 0.16, 다: kRs 0.19) ──────────────────
def test_eto_error_is_zero_for_0_16_and_positive_for_0_19_when_radiation_follows_the_equation():
    a = _classify([_item(d, gsr=_clipped_gsr(d, 0.16)) for d in YEAR])
    e = a["eto"]
    assert e["n"] == 365
    assert e["mae_016"] == pytest.approx(0.0, abs=1e-6) and e["bias_016"] == pytest.approx(0.0, abs=1e-6)
    assert e["bias_019"] > 0.0 and e["mae_019"] == pytest.approx(e["bias_019"], abs=1e-9)
    assert e["best_krs"] == 0.16


def test_eto_error_prefers_0_19_when_radiation_follows_0_19():
    a = _classify([_item(d, gsr=_clipped_gsr(d, 0.19)) for d in YEAR])
    e = a["eto"]
    assert e["mae_019"] == pytest.approx(0.0, abs=1e-6) and e["bias_016"] < 0.0
    assert e["best_krs"] == 0.19


def test_eto_error_halves_use_calendar_months_1_to_6_and_7_to_12():
    a = _classify([_item(d, gsr=_clipped_gsr(d, 0.16)) for d in YEAR])
    h1, h2 = a["eto"]["h1"], a["eto"]["h2"]
    n1 = sum(1 for d in YEAR if d.month <= 6)
    assert h1["n"] == n1 and h2["n"] == 365 - n1
    assert h1["mae_019"] > 0 and h2["mae_019"] > 0


def test_eto_error_skips_days_that_are_not_comparable():
    """관측 일사가 무효인 날·기온이 없는 날은 비교에서 빠진다"""
    items = [_item(d, gsr=_clipped_gsr(d, 0.16)) for d in YEAR]
    for it in items[:10]:
        it["sumGsr"] = "0.0"
    for it in items[10:15]:
        it["maxTa"] = ""
    a = _classify(items)
    assert a["eto"]["n"] < 365 - 10  # 무효 10일(월 규칙 영향 포함)과 기온 없는 5일은 제외


# ── 의심 일조 ───────────────────────────────────────────────────────────
def test_suspect_sunshine_counts_zero_hours_without_rain_and_with_unusable_radiation():
    d = _days(dt.date(2026, 5, 1), 8)
    items = [
        _item(d[0], gsr="", ss="0.0", rain=""),  # 센다 (일사 없음, 강수 없음)
        _item(d[1], gsr="0.0", ss="0.0", rain=""),  # 센다 (일사 0.0 무효)
        _item(d[2], gsr="", ss="0.0", rain="0.0"),  # 센다 (강수 0.0 = 없음)
        _item(d[3], gsr="", ss="0.0", rain="5.5"),  # 안 센다: 강수 있음
        _item(d[4], gsr="15.0", ss="0.0", rain=""),  # 안 센다: 일사 유효
        _item(d[5], gsr="", ss="3.0", rain=""),  # 안 센다: 일조 0 아님
        _item(d[6], gsr="", ss="", rain=""),  # 안 센다: 일조 없음(빈 값)
        _item(d[7], gsr="0.0", ss="0.0", rain="1.0"),  # 안 센다: 강수 있음
    ]
    assert _classify(items)["suspect_sunshine_days"] == 3


# ── 경계 후보 비교 ──────────────────────────────────────────────────────
def _s(stn, km, mae16, mae19, n=100, island=False, elev=50.0, h1=None, h2=None):
    h1 = h1 or dict(n=n // 2, mae_016=mae16, mae_019=mae19)
    h2 = h2 or dict(n=n - n // 2, mae_016=mae16, mae_019=mae19)
    return dict(
        stn_id=stn,
        coast_km=km,
        island=island,
        elev_m=elev,
        eto=dict(n=n, mae_016=mae16, mae_019=mae19, best_krs=0.19 if mae19 < mae16 else 0.16, h1=h1, h2=h2),
    )


def _four():
    return [
        _s("A", 5.0, 0.5, 0.2, island=True),
        _s("B", 15.0, 0.4, 0.3, island=True),
        _s("C", 50.0, 0.1, 0.4),
        _s("D", 80.0, 0.1, 0.5),
    ]


def _row(res, label):
    return next(r for r in res["rows"] if r["label"] == label)


def test_boundary_table_matches_hand_calculation():
    res = compare_boundaries(_four())
    assert _row(res, "전부 0.16")["mean_mae"] == pytest.approx((0.5 + 0.4 + 0.1 + 0.1) / 4)
    assert _row(res, "전부 0.19")["mean_mae"] == pytest.approx((0.2 + 0.3 + 0.4 + 0.5) / 4)
    assert _row(res, "거리 ≤ 10 km")["mean_mae"] == pytest.approx((0.2 + 0.4 + 0.1 + 0.1) / 4)
    assert _row(res, "거리 ≤ 20 km")["mean_mae"] == pytest.approx((0.2 + 0.3 + 0.1 + 0.1) / 4)
    assert _row(res, "지점별 최적(이론값)")["mean_mae"] == pytest.approx((0.2 + 0.3 + 0.1 + 0.1) / 4)
    assert _row(res, "거리 ≤ 20 km")["n_high"] == 2 and _row(res, "거리 ≤ 20 km")["n"] == 4


def test_distance_equal_to_candidate_counts_as_coastal():
    res = compare_boundaries(_four())
    assert _row(res, "거리 ≤ 50 km")["n_high"] == 3  # C(50 km)는 경계 포함
    assert _row(res, "거리 ≤ 50 km")["mean_mae"] == pytest.approx((0.2 + 0.3 + 0.4 + 0.1) / 4)
    assert _row(res, "거리 ≤ 10 km")["n_high"] == 1


def test_island_and_elevation_criteria_are_compared_too():
    res = compare_boundaries(_four())
    assert _row(res, "섬만")["n_high"] == 2
    assert _row(res, "섬만")["mean_mae"] == pytest.approx((0.2 + 0.3 + 0.1 + 0.1) / 4)
    assert _row(res, "거리 ≤ 10 km 또는 섬")["n_high"] == 2
    assert {"고도 ≤ 100 m", "고도 ≤ 200 m", "고도 ≤ 300 m"} <= {r["label"] for r in res["rows"]}


def test_best_criterion_is_the_one_with_smallest_error_and_oracle_is_not_a_candidate():
    res = compare_boundaries(_four())
    assert res["best"]["label"] in ("거리 ≤ 20 km", "섬만", "거리 ≤ 10 km 또는 섬", "거리 ≤ 30 km")
    assert res["best"]["mean_mae"] == pytest.approx((0.2 + 0.3 + 0.1 + 0.1) / 4)
    assert res["best"]["label"] != "지점별 최적(이론값)"
    assert res["best"]["gain_vs_all_016"] == pytest.approx(0.275 - 0.175)
    assert res["meaningless"] is False


def test_day_weighted_error_uses_day_counts():
    s = [_s("A", 5.0, 0.5, 0.2, n=300), _s("B", 80.0, 0.1, 0.5, n=100)]
    res = compare_boundaries(s)
    assert _row(res, "전부 0.16")["weighted_mae"] == pytest.approx((0.5 * 300 + 0.1 * 100) / 400)
    assert _row(res, "전부 0.16")["mean_mae"] == pytest.approx((0.5 + 0.1) / 2)


def test_halves_are_reported_and_best_per_half_is_found():
    a = _s(
        "A", 5.0, 0.5, 0.2, h1=dict(n=50, mae_016=0.5, mae_019=0.2), h2=dict(n=50, mae_016=0.1, mae_019=0.4)
    )
    b = _s(
        "B", 80.0, 0.1, 0.5, h1=dict(n=50, mae_016=0.1, mae_019=0.5), h2=dict(n=50, mae_016=0.1, mae_019=0.5)
    )
    res = compare_boundaries([a, b])
    r = _row(res, "거리 ≤ 10 km")
    assert r["mean_mae_h1"] == pytest.approx((0.2 + 0.1) / 2) and r["mean_mae_h2"] == pytest.approx(
        (0.4 + 0.1) / 2
    )
    assert res["best_h1"]["label"] == "거리 ≤ 10 km" and res["best_h2"]["label"] == "전부 0.16"


def test_no_difference_between_criteria_is_called_meaningless():
    s = [_s(f"S{i}", 10.0 * (i + 1), 0.300, 0.304) for i in range(6)]
    res = compare_boundaries(s)
    assert res["meaningless"] is True  # 가장 좋은 기준과 "전부 0.16"의 차이가 0.01 mm/d 미만


def test_stations_without_eto_or_distance_are_left_out():
    s = _four() + [dict(stn_id="E", coast_km=None, island=False, elev_m=1.0, eto=None)]
    s.append(dict(stn_id="F", coast_km=3.0, island=False, elev_m=1.0, eto=None))
    res = compare_boundaries(s)
    assert _row(res, "전부 0.16")["n"] == 4


# ── 수집기 ──────────────────────────────────────────────────────────────
class FakeClock:
    def __init__(self, t):
        self.t = pd.Timestamp(t)
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept += s


class FakeAsos:
    """지점 종류별 가짜 ASOS: normal(0.17 식 일사) / zero(일사 0.0) / nogsr(일사·일조 없음)"""

    def __init__(self, kinds, fail_stn=None):
        self.kinds, self.fail_stn = kinds, fail_stn
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, url, timeout):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert q["serviceKey"] == KEY
        stn, s, e = q["stnIds"], q["startDt"], q["endDt"]
        self.calls.append((stn, s, e))
        if stn == self.fail_stn:
            return 500, b"server error"
        d0 = dt.date.fromisoformat(f"{s[:4]}-{s[4:6]}-{s[6:]}")
        d1 = dt.date.fromisoformat(f"{e[:4]}-{e[4:6]}-{e[6:]}")
        kind = self.kinds[stn]
        items = []
        for d in _days(d0, (d1 - d0).days + 1):
            if kind == "normal":
                items.append(_item(d, gsr=_krs_gsr(d, 0.17)))
            elif kind == "zero":
                items.append(_item(d, gsr="0.0"))
            else:
                items.append(_item(d, gsr="", ss=""))
        page, rows = int(q["pageNo"]), int(q["numOfRows"])
        body = {"items": {"item": items[(page - 1) * rows : page * rows]}, "totalCount": len(items)}
        return 200, json.dumps(
            {"response": {"header": {"resultCode": "00", "resultMsg": "OK"}, "body": body}}
        ).encode()


def _dicts():
    def st(i, n, lat=LAT, act=True):
        return dict(
            stn_id=i, name_ko=n, lat=lat, lon=127.0, elev_m=ELEV, wind_height_m=10, is_active=act,
            measures_sunlight=True,
        )  # fmt: skip

    return Dictionaries.from_rows(
        dict(
            stations=[st("101", "정상"), st("102", "영점"), st("103", "없음"), st("999", "폐지", act=False)],
            crops=[],
            crop_seasons=[],
            crop_kc_sets=[],
            soil_types=[],
            scenarios=[],
        )
    )


NOW = "2026-11-08 10:00"
KINDS = {"101": "normal", "102": "zero", "103": "nogsr", "999": "normal"}


def _run(**kw):
    api = FakeAsos(KINDS, fail_stn=kw.pop("fail_stn", None))
    clock = FakeClock(NOW)
    r = RA.run(_dicts(), KEY, http=api, clock=clock, **kw)
    return r, api


def test_run_fetches_last_365_days_in_two_calendar_year_pieces_per_station():
    r, api = _run()
    mine = [c for c in api.calls if c[0] == "101"]
    assert mine == [("101", "20251108", "20251231"), ("101", "20260101", "20261107")]
    assert {c[0] for c in api.calls} == {"101", "102", "103"}  # 폐지 지점(999)은 묻지 않는다
    assert len(api.calls) == 6
    assert (r.start, r.end) == (dt.date(2025, 11, 8), dt.date(2026, 11, 7))


def test_run_days_option_within_one_year_makes_one_call_per_station():
    r, api = _run(days=30)
    assert len(api.calls) == 3 and api.calls[0] == ("101", "20261009", "20261107")


def test_run_classifies_each_station_and_keeps_station_info():
    r, _ = _run()
    by = {s["stn_id"]: s for s in r.stations}
    assert by["101"]["klass"] == CLASS_NORMAL and by["101"]["krs_median"] == pytest.approx(0.17, abs=1e-4)
    assert by["102"]["klass"] == CLASS_ZERO_INVALID and by["103"]["klass"] == CLASS_ALL_MISSING
    assert (by["101"]["name"], by["101"]["lat"], by["101"]["lon"], by["101"]["elev_m"]) == (
        "정상",
        LAT,
        127.0,
        ELEV,
    )
    assert by["101"]["eto"]["n"] > 300 and by["102"]["eto"] is None
    assert not r.failed and r.problems == []


def test_run_one_station_failure_is_recorded_and_others_continue():
    r, _ = _run(fail_stn="102")
    by = {s["stn_id"]: s for s in r.stations}
    assert by["102"]["error"] == "ASOS 실패" and by["101"]["klass"] == CLASS_NORMAL
    assert r.failed and any("102" in p for p in r.problems)  # 조용히 넘어가지 않는다


def test_run_with_explicit_stations_only_asks_those():
    r, api = _run(stations=["103"])
    assert {c[0] for c in api.calls} == {"103"} and [s["stn_id"] for s in r.stations] == ["103"]


def test_run_with_no_target_station_is_a_problem_not_silence():
    d = Dictionaries.from_rows(
        dict(stations=[], crops=[], crop_seasons=[], crop_kc_sets=[], soil_types=[], scenarios=[])
    )
    r = RA.run(d, KEY, http=FakeAsos({}), clock=FakeClock(NOW))
    assert r.failed and any("0곳" in p for p in r.problems)


def test_outputs_have_station_file_brief_and_no_key(tmp_path):
    r, _ = _run()
    out = RA.write_outputs(r, tmp_path)
    st = json.loads((out / "stations.json").read_text(encoding="utf-8"))
    assert {s["stn_id"] for s in st} == {"101", "102", "103"}
    text = "".join(p.read_text(encoding="utf-8") for p in out.iterdir() if p.is_file())
    assert KEY not in text and "serviceKey" not in text
    brief = (out / "brief.md").read_text(encoding="utf-8")
    assert "정상" in brief and "호출" in brief and len(brief.encode("utf-8")) < 3500


def test_collector_has_no_database_write_path():
    src = inspect.getsource(RA)
    assert "SupabaseWriter" not in src and "supabase_write" not in src and "upsert" not in src
    assert "write" not in inspect.signature(RA.run).parameters


def test_main_reads_key_only_from_environment(monkeypatch, tmp_path):
    for k in ("DATA_GO_KR", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):  # 키가 없으면 조용히 넘어가지 않고 멈춘다
        RA.main(["--out", str(tmp_path)])
