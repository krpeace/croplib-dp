"""해안 목록(coastal_stations.csv)과 지점별 kRs 연결 (io/coastal.py, station_meta). 이슈 #48 PR-B.

승인(2026-11-08): 해안선 10 km 이하 → kRs 0.19, 아니면 0.16. **관측 결측일 대체(식50)에만** 적용한다.
예보용 kRs(io/coefficients.py, core/solar.KRS_FAO)는 그대로다. 입력은 가짜 값이다.
"""

import csv
import datetime as dt
import inspect
from importlib import resources

import pytest

from croplib_dp.core import forecast_eto
from croplib_dp.core.daily_weather import daily_weather
from croplib_dp.core.radiation import COASTAL_MAX_KM, KRS_COASTAL, KRS_INLAND
from croplib_dp.core.solar import KRS_FAO
from croplib_dp.io import coastal, coefficients
from croplib_dp.io.dictionaries import station_meta


def _rows():
    with resources.files("croplib_dp.data").joinpath("coastal_stations.csv").open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_constants_follow_the_approved_boundary():
    assert COASTAL_MAX_KM == 10.0 and KRS_COASTAL == 0.19 and KRS_INLAND == 0.16


def test_csv_columns_and_unique_stations():
    rows = _rows()
    assert list(rows[0].keys()) == [
        "stn_id",
        "name_ko",
        "coast_km",
        "island",
        "resolution_corrected",
        "krs",
        "basis",
    ]
    ids = [r["stn_id"] for r in rows]
    assert len(ids) == len(set(ids)) and len(ids) >= 90


def test_krs_follows_the_distance_rule_for_every_row_with_ten_km_inclusive():
    for r in _rows():
        expect = 0.19 if float(r["coast_km"]) <= 10.0 else 0.16
        assert float(r["krs"]) == expect, r["stn_id"]


def test_the_approved_result_40_coastal_stations_and_known_ones():
    by = {r["stn_id"]: float(r["krs"]) for r in _rows()}
    assert sum(1 for v in by.values() if v == 0.19) == 40  # 정상 30곳 + 일사 없는 10곳(PR 본문)
    for coastal_stn in (
        "185",
        "115",
        "189",
        "184",
        "165",
        "140",
    ):  # 고산·울릉도·서귀포·제주·목포·군산(보정 포함)
        assert by[coastal_stn] == 0.19, coastal_stn
    for inland_stn in ("101", "272", "216", "273"):  # 춘천·영주·태백·문경
        assert by[inland_stn] == 0.16, inland_stn


def test_resolution_corrected_stations_are_zero_km_and_flagged():
    corrected = {r["stn_id"]: r for r in _rows() if r["resolution_corrected"] == "true"}
    assert set(corrected) == {"185", "140", "165"}  # 고산·군산·목포
    assert all(float(r["coast_km"]) == 0.0 for r in corrected.values())


def test_island_column_matches_the_approved_twelve():
    assert {r["stn_id"] for r in _rows() if r["island"] == "true"} == {
        "189", "201", "294", "185", "295", "102", "188", "170", "115", "184", "268", "169"
    }  # fmt: skip


# ── station_meta 연결 ───────────────────────────────────────────────────
def _st(stn_id):
    return dict(stn_id=stn_id, lat=36.0, elev_m=50.0, wind_height_m=10.0)


def test_station_meta_reads_krs_from_the_coastal_list():
    assert station_meta(_st("185")).krs == 0.19  # 고산(해안)
    assert station_meta(_st("101")).krs == 0.16  # 춘천(내륙)


def test_station_meta_accepts_integer_station_ids_and_defaults_to_inland_for_unknown():
    assert station_meta(_st(185)).krs == 0.19
    assert station_meta(_st("99999")).krs == 0.16  # 목록에 없는 지점
    assert station_meta(dict(lat=36.0, elev_m=50.0, wind_height_m=10.0)).krs == 0.16  # stn_id 없음


def test_krs_lookup_reads_the_list_only_once():
    coastal._load.cache_clear()
    coastal.coastal_krs("185")
    coastal.coastal_krs("101")
    assert coastal._load.cache_info().misses == 1  # 목록 전체를 한 번 읽어 둔다


def _items(day, gsr="", ss=""):
    return dict(
        tm=day.isoformat(), maxTa="26.0", minTa="14.0", avgTa="20.0", avgRhm="60.0", minRhm="35.0",
        avgWs="2.0", avgPv="12.0", avgTd="9.0", avgPa="1000.0", sumGsr=gsr, sumSsHr=ss, sumRn="",
    )  # fmt: skip


def test_coastal_station_uses_0_19_only_on_temperature_range_days():
    days = [dt.date(2026, 6, 1) + dt.timedelta(days=k) for k in range(3)]
    items = [_items(days[0], gsr="20.0"), _items(days[1], ss="8.0"), _items(days[2])]  # 관측 / 일조 / 기온차
    coast = daily_weather(items, station_meta(_st("185")))
    inland = daily_weather(items, station_meta(_st("101")))
    assert [r["rs_source"] for r in coast] == ["observed", "sunshine", "temp_range"]
    assert coast[0]["rs_mj"] == inland[0]["rs_mj"] == 20.0 and coast[0]["eto_mm"] == inland[0]["eto_mm"]
    assert coast[1]["rs_mj"] == inland[1]["rs_mj"]  # 일조시간식은 kRs 와 무관
    assert coast[2]["rs_mj"] > inland[2]["rs_mj"] and coast[2]["eto_mm"] > inland[2]["eto_mm"]


# ── 예보용 kRs 는 그대로 ────────────────────────────────────────────────
def test_forecast_krs_defaults_are_unchanged():
    assert KRS_FAO == 0.16
    assert coefficients.load_s3_coef("0")["krs"] == 0.16
    assert coefficients.load_s3_coef("185")["krs"] == 0.16  # 해안 지점이어도 예보용 값은 그대로
    assert "coastal" not in inspect.getsource(forecast_eto) and "station_meta" not in inspect.getsource(
        forecast_eto
    )


def test_coefficient_file_is_not_touched_by_the_coastal_list():
    src = inspect.getsource(coefficients)
    assert "coastal" not in src and "krs=0.16, a=None" in src  # io/coefficients.py:31 의 기본값 줄 그대로


@pytest.mark.parametrize("stn", ["185", "165", "140"])
def test_corrected_stations_get_the_coastal_value(stn):
    assert station_meta(_st(stn)).krs == 0.19
