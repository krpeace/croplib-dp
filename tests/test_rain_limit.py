"""하루 강수량(rain_mm) 계약 상한 1,000 mm (결정 0027).

2026-08-17 294 거제에 하루 654.3 mm(전날 273.3 mm, 이틀 합 927.6 mm)가 실제로 관측됐는데 옛 상한 500 mm 에
걸려 소급 5묶음 쓰기가 막혔다. 국내 일강수 최고 기록은 870.5 mm(2002-08-31 강릉)라 1,000 mm 로 올린다.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.io.contract import DAILY_RAIN_MAX_MM, SPECS, check_rows  # noqa: E402
from test_upload import _slot, _taw  # noqa: E402

# 하루 강수량을 담는 모든 열 (표, 열)
DAILY_RAIN_COLUMNS = [
    ("weather_daily", "rain_mm"),
    ("soil_water_daily", "rain_mm"),
    ("water_balance_daily", "rain_mm"),
    ("water_outlook", "rain_fcst_mm"),
    ("water_outlook", "rain_expected_mm"),
]


def _soil_row(rain_mm):
    """2026-08-17 거제 극한호우 날의 토양 줄 (가짜 값, 모양만 같음)."""
    return dict(
        stn_id="294",
        soil_code="loam",
        date="2026-08-17",
        eto_mm=3.0,
        rain_mm=rain_mm,
        eto_7d_mm=20.0,
        rain_7d_mm=968.0,
        dp_mm=550.0,
        dr_mm=0.0,
        taw_mm=90.0,
        raw_mm=45.0,
        aw_pct=100.0,
        status="pool",
        days_to_dry=None,
        filled=False,
        calc_version="test",
    )


def test_the_limit_is_1000_mm_and_every_daily_rain_column_uses_it():
    assert DAILY_RAIN_MAX_MM == 1000
    for table, col in DAILY_RAIN_COLUMNS:
        assert SPECS[table].ranges[col] == (0, DAILY_RAIN_MAX_MM), (table, col)


def test_real_record_rain_654_3_mm_passes_in_weather_and_soil_and_balance():
    r = _slot()
    w = dict(r.weather_daily[0], rain_mm=654.3)
    assert check_rows("weather_daily", [w]) == []
    assert check_rows("soil_water_daily", [_soil_row(654.3)]) == []
    b = dict(r.water_balance_daily[-1], rain_mm=654.3)
    assert check_rows("water_balance_daily", [b], _taw()) == []


def test_forecast_rain_columns_follow_the_same_limit():
    r = _slot()
    o = dict(r.water_outlook[0], rain_fcst_mm=654.3, rain_expected_mm=327.0)
    assert check_rows("water_outlook", [o], _taw(), ignore=("run_id",), extra=W.RUN_KEY) == []


def test_exactly_1000_passes_but_more_than_1000_is_blocked():
    r = _slot()
    assert check_rows("weather_daily", [dict(r.weather_daily[0], rain_mm=1000.0)]) == []
    assert check_rows("soil_water_daily", [_soil_row(1000.0)]) == []
    over_w = check_rows("weather_daily", [dict(r.weather_daily[0], rain_mm=1000.1)])
    assert len(over_w) == 1 and "rain_mm=1000.1 (범위 0~1000)" in over_w[0]
    over_s = check_rows("soil_water_daily", [_soil_row(1500.0)])
    assert len(over_s) == 1 and "rain_mm=1500.0 (범위 0~1000)" in over_s[0]
    over_b = check_rows("water_balance_daily", [dict(r.water_balance_daily[-1], rain_mm=1200.0)], _taw())
    assert len(over_b) == 1 and "rain_mm=1200.0 (범위 0~1000)" in over_b[0]


@pytest.mark.parametrize("table,col", DAILY_RAIN_COLUMNS[:2])
def test_negative_rain_is_still_blocked(table, col):
    r = _slot()
    row = dict(r.weather_daily[0], rain_mm=-0.1) if table == "weather_daily" else _soil_row(-0.1)
    probs = check_rows(table, [row])
    assert len(probs) == 1 and "rain_mm=-0.1" in probs[0]


def test_other_ranges_were_not_loosened():
    """상한은 강수량만 올렸다. 관수 순량(irr_net_mm)·유효수분(taw_mm) 등은 그대로."""
    assert SPECS["water_balance_daily"].ranges["irr_net_mm"] == (0, 500)
    assert SPECS["soil_water_daily"].ranges["taw_mm"] == (1, 500)
    assert SPECS["soil_water_daily"].ranges["rain_7d_mm"] == (0, 2000)
    assert SPECS["weather_daily"].ranges["eto_mm"] == (0, 15)
