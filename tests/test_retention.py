"""전망 표 보관·원문 90일 규칙의 순수 함수 (core/retention.py). 이슈 #61(#28 S3-a).

규칙(#28 계획서 D3·D4, 2026-11-08 승인):
  - water_outlook·water_outlook_summary: forecast_runs.run_at 의 한국 날짜가 오늘 − 14일보다 앞인 줄은 월 단위 csv.gz 로
    Storage 에 보관한 뒤 지운다. forecast_runs 는 지우지 않는다.
  - 예보 원문: 대표 밖 지점(`fcst-ext/`)만 발표 시각이 90일 지난 것을 지운다. 대표 10곳(`fcst/`)은 지금처럼 보관한다.
입력은 모두 가짜 값이다.
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import datetime as dt
import gzip
import hashlib
import lzma

import pandas as pd
import pytest

from croplib_dp.core import retention as R
from croplib_dp.io.contract import SPECS

NOW = pd.Timestamp("2026-11-08 10:00")  # 한국 시각


# ── 14일 기준 ───────────────────────────────────────────────────────────
def test_keep_window_is_14_days_and_uses_korean_date():
    assert R.OUTLOOK_KEEP_DAYS == 14
    assert R.keep_from_date(NOW) == dt.date(2026, 10, 25)  # 오늘(11/8) − 14일


def test_run_older_than_keep_from_date_is_expired_and_the_boundary_day_is_kept():
    assert R.is_expired("2026-10-24T23:50:00+09:00", NOW) is True  # 10/24 → 14일 넘음
    assert R.is_expired("2026-10-25T00:00:00+09:00", NOW) is False  # 10/25 는 남긴다(오늘 − 14일)
    assert R.is_expired("2026-11-08T02:00:00+09:00", NOW) is False


def test_db_utc_timestamps_are_converted_to_the_korean_date():
    """DB 는 UTC 로 돌려준다: 10/24 15:30Z = 한국 10/25 00:30 → 남김, 10/24 14:59Z = 한국 10/24 23:59 → 보관"""
    assert R.is_expired("2026-10-24T15:30:00+00:00", NOW) is False
    assert R.is_expired("2026-10-24T14:59:00+00:00", NOW) is True


def test_through_date_must_be_before_the_keep_window():
    assert R.max_through(NOW) == dt.date(2026, 10, 24)
    R.check_through(dt.date(2026, 10, 24), NOW)  # 허용
    for bad in (dt.date(2026, 10, 25), dt.date(2026, 11, 8)):
        with pytest.raises(ValueError, match="14일"):
            R.check_through(bad, NOW)


def test_through_end_is_the_next_korean_midnight_in_utc():
    assert R.through_end_utc(dt.date(2026, 10, 24)) == "2026-10-24T15:00:00+00:00"  # 한국 10/25 00:00


# ── 월 구분·경로 ────────────────────────────────────────────────────────
def test_month_is_by_korean_date_not_utc():
    assert R.archive_month("2026-10-31T16:00:00+00:00") == "2026-11"  # 한국 11/1 01:00
    assert R.archive_month("2026-10-31T14:00:00+00:00") == "2026-10"  # 한국 10/31 23:00


def test_archive_path_and_name_rule():
    assert (
        R.archive_path("water_outlook", "2026-10")
        == "archive/water_outlook/2026/water_outlook_2026-10.csv.gz"
    )
    assert (
        R.archive_path("water_outlook_summary", "2027-01")
        == "archive/water_outlook_summary/2027/water_outlook_summary_2027-01.csv.gz"
    )
    with pytest.raises(ValueError):
        R.archive_path("forecast_runs", "2026-10")  # forecast_runs 는 보관 대상이 아니다(지우지 않음)


def test_columns_are_prefix_plus_all_table_columns_matching_the_contract():
    assert R.ARCHIVE_PREFIX_COLS == ("run_at", "run_name", "stn_id", "collector")
    for table in ("water_outlook", "water_outlook_summary"):
        assert R.ARCHIVE_COLUMNS[table] == R.ARCHIVE_PREFIX_COLS + SPECS[table].columns
        assert R.ROW_KEY[table] == SPECS[table].key
    assert "run_id" in R.ARCHIVE_COLUMNS["water_outlook"]  # run_id 포함(복원 때 forecast_runs 와 이어짐)


# ── 줄 만들기·합치기 ───────────────────────────────────────────────────
RUN = dict(run_id=7, run_at="2026-10-20T17:00:00+09:00", run_name="저녁", stn_id="101", collector="actions")


def _outlook(run_id=7, scenario="S1", day="2026-10-21", lead=1, eto=3.5):
    return dict(
        run_id=run_id, scenario_id=scenario, target_date=day, lead_day=lead, eto_mm=eto, etc_mm=3.0,
        etc_err_mm=0.5, rain_fcst_mm=None, pop_pct=20, rain_expected_mm=0.4, dr_center_mm=10.0,
        dr_early_mm=9.0, dr_late_mm=11.0, status="normal", calc_version="v1.5",
    )  # fmt: skip


def test_flatten_puts_run_columns_first_and_keeps_run_id():
    row = R.flatten("water_outlook", _outlook(), RUN)
    assert list(row)[:5] == ["run_at", "run_name", "stn_id", "collector", "run_id"]
    assert row["run_at"] == "2026-10-20T17:00:00+09:00" and row["run_id"] == 7 and row["eto_mm"] == 3.5


def test_flatten_normalizes_run_at_to_korean_time_for_a_stable_file():
    run = dict(RUN, run_at="2026-10-20T08:00:00+00:00")
    assert R.flatten("water_outlook", _outlook(), run)["run_at"] == "2026-10-20T17:00:00+09:00"


def test_sort_order_is_run_at_station_scenario_target_date():
    rows = [
        R.flatten("water_outlook", _outlook(scenario="S2", day="2026-10-21"), RUN),
        R.flatten("water_outlook", _outlook(scenario="S1", day="2026-10-22"), RUN),
        R.flatten("water_outlook", _outlook(scenario="S1", day="2026-10-21"), RUN),
        R.flatten("water_outlook", _outlook(), dict(RUN, run_at="2026-10-19T17:00:00+09:00", run_id=6)),
    ]
    got = [(r["run_at"][:10], r["scenario_id"], r["target_date"]) for r in R.sort_rows("water_outlook", rows)]
    assert got == [
        ("2026-10-19", "S1", "2026-10-21"),
        ("2026-10-20", "S1", "2026-10-21"),
        ("2026-10-20", "S1", "2026-10-22"),
        ("2026-10-20", "S2", "2026-10-21"),
    ]


def test_merge_dedupes_by_table_key_with_new_rows_winning_and_is_repeatable():
    old = [R.flatten("water_outlook", _outlook(eto=3.5), RUN)]
    new = [
        R.flatten("water_outlook", _outlook(eto=4.0), RUN),
        R.flatten("water_outlook", _outlook(day="2026-10-22"), RUN),
    ]
    merged = R.merge_rows("water_outlook", old, new)
    assert len(merged) == 2 and merged[0]["eto_mm"] == 4.0
    assert R.merge_rows("water_outlook", merged, new) == merged  # 두 번 해도 같다


def test_summary_key_is_run_and_scenario_only():
    s = dict(
        run_id=7,
        scenario_id="S1",
        horizon_days=3,
        start_date="2026-10-21",
        status_start="normal",
        calc_version="v1.5",
    )
    a = R.flatten("water_outlook_summary", s, RUN)
    b = R.flatten("water_outlook_summary", dict(s, horizon_days=4), RUN)
    assert len(R.merge_rows("water_outlook_summary", [a], [b])) == 1


# ── 압축 파일(csv.gz) ──────────────────────────────────────────────────
def test_encode_is_deterministic_gzip_with_header_and_blank_for_none():
    rows = [R.flatten("water_outlook", _outlook(), RUN)]
    a, b = R.encode_archive("water_outlook", rows), R.encode_archive("water_outlook", rows)
    assert a == b and a[:2] == b"\x1f\x8b" and a[4:8] == b"\x00\x00\x00\x00"  # mtime = 0
    text = gzip.decompress(a).decode("utf-8")
    assert text.splitlines()[0] == ",".join(R.ARCHIVE_COLUMNS["water_outlook"])
    assert ",," in text.splitlines()[1]  # rain_fcst_mm = None → 빈 칸


def test_decode_restores_types_so_the_rows_can_be_upserted_again():
    rows = [R.flatten("water_outlook", _outlook(), RUN)]
    back = R.decode_archive("water_outlook", R.encode_archive("water_outlook", rows))
    assert back == rows  # 정수·실수·문자·None 모두 그대로
    assert isinstance(back[0]["run_id"], int) and isinstance(back[0]["eto_mm"], float)
    assert back[0]["rain_fcst_mm"] is None and back[0]["status"] == "normal"


def test_decode_rejects_a_file_with_unexpected_columns():
    bad = gzip.compress(b"a,b\n1,2\n", mtime=0)
    with pytest.raises(ValueError, match="열"):
        R.decode_archive("water_outlook", bad)


def test_db_row_extracts_only_table_columns_from_an_archive_row():
    row = R.flatten("water_outlook", _outlook(), RUN)
    assert R.db_row("water_outlook", row) == _outlook()


def test_sha256_helper():
    assert R.sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()


# ── 예보 원문 90일 규칙 ────────────────────────────────────────────────
def test_ext_prefix_is_never_the_representative_prefix():
    assert R.RAW_EXT_PREFIX == "fcst-ext/" and R.RAW_REP_PREFIX == "fcst/"
    assert R.raw_prefix(True) == "fcst" and R.raw_prefix(False) == "fcst-ext"


def test_raw_path_builder_matches_the_current_naming():
    issue = pd.Timestamp("2026-06-10 02:00")
    assert R.raw_path("fcst", "73_134", issue) == "fcst/73_134/2026/06/20260610_0200.json.gz"
    assert R.raw_path("fcst-ext", "60_127", issue) == "fcst-ext/60_127/2026/06/20260610_0200.json.gz"


def test_raw_issue_time_is_read_from_the_file_name_for_gz_and_xz():
    assert R.raw_issue_time("fcst-ext/60_127/2026/06/20260610_1700.json.gz") == pd.Timestamp(
        "2026-06-10 17:00"
    )
    assert R.raw_issue_time("fcst-ext/60_127/2026/06/20260610_1700.json.xz") == pd.Timestamp(
        "2026-06-10 17:00"
    )
    assert R.raw_issue_time("fcst-ext/60_127/readme.txt") is None


def test_raw_90_day_boundary_89_90_91():
    def path(days):
        t = NOW - pd.Timedelta(days=days)
        return R.raw_path("fcst-ext", "60_127", t.floor("h"))

    assert R.raw_expired(path(89), NOW) is False
    assert R.raw_expired(path(90), NOW) is False  # 정확히 90일은 남긴다(90일 보관)
    assert R.raw_expired(path(91), NOW) is True


def test_representative_raw_files_are_never_expired_however_old():
    assert R.raw_expired("fcst/73_134/2020/01/20200101_0200.json.gz", NOW) is False
    assert R.raw_expired("archive/water_outlook/2020/water_outlook_2020-01.csv.gz", NOW) is False
    assert R.raw_expired("text/OVR1/2020/01/1.pages.json.gz", NOW) is False


def test_raw_expired_ignores_unparseable_names_instead_of_guessing():
    assert R.raw_expired("fcst-ext/60_127/2026/06/notes.txt", NOW) is False


# ── 원문 읽기: gzip·xz 둘 다 ───────────────────────────────────────────
DOC = {"issue": "2026-06-10 02:00", "grid": "73_134", "pages": ['{"response":{}}', "second"]}


def _doc_bytes():
    import json

    return json.dumps(DOC, ensure_ascii=False).encode("utf-8")


def test_read_raw_doc_reads_gzip_and_xz_the_same_way():
    raw = _doc_bytes()
    assert R.read_raw_doc(gzip.compress(raw, mtime=0)) == DOC
    assert R.read_raw_doc(lzma.compress(raw, preset=9 | lzma.PRESET_EXTREME)) == DOC


def test_read_raw_doc_fails_loudly_on_unknown_format():
    with pytest.raises(ValueError, match="형식"):
        R.read_raw_doc(b"plain text")


def test_xz_estimate_shows_how_much_recompressing_would_save():
    import random

    rng = random.Random(1)
    body = "".join(
        f'{{"fcstTime":"{rng.randrange(2400):04d}","fcstValue":"{rng.randrange(40)}"}},' for _ in range(6000)
    )
    big = gzip.compress(body.encode(), mtime=0)
    est = R.xz_saving([big, big])
    assert est["n"] == 2 and est["gz_bytes"] == len(big) * 2 and est["xz_bytes"] > 0
    assert est["ratio"] == pytest.approx(est["xz_bytes"] / est["gz_bytes"])
    assert R.xz_saving([]) == dict(n=0, gz_bytes=0, xz_bytes=0, ratio=None)


# ── 용량 계산 (dry-run 보고용) ─────────────────────────────────────────
def test_db_bytes_freed_uses_the_measured_row_sizes():
    assert R.ROW_BYTES["water_outlook"] == 247 and R.ROW_BYTES["water_outlook_summary"] == 245
    assert R.db_bytes_freed({"water_outlook": 1000, "water_outlook_summary": 200}) == 1000 * 247 + 200 * 245


# ── 용량 경고선·DB 크기 추정 (S3-b) ────────────────────────────────────
def test_db_thresholds_exact_400_450_and_estimate_360_405():
    assert (R.DB_WARN_MB, R.DB_ERR_MB) == (400.0, 450.0)
    assert (R.DB_EST_WARN_MB, R.DB_EST_ERR_MB) == (360.0, 405.0)  # 추정은 오차 ±10% 여유로 일찍
    assert [R.db_level(mb, exact=True) for mb in (399.9, 400.0, 449.9, 450.0)] == [
        "ok",
        "warn",
        "warn",
        "error",
    ]
    assert [R.db_level(mb, exact=False) for mb in (359.9, 360.0, 404.9, 405.0)] == [
        "ok",
        "warn",
        "warn",
        "error",
    ]


def test_storage_thresholds_800_and_950():
    assert (R.STORAGE_WARN_MB, R.STORAGE_ERR_MB) == (800.0, 950.0)
    assert [R.storage_level(mb) for mb in (799.9, 800.0, 949.9, 950.0)] == ["ok", "warn", "warn", "error"]


E2_COUNTS = {
    "heat_daily": 114_975, "soil_water_daily": 37_200, "sun_daily": 26_280, "water_balance_daily": 10_140,
    "water_outlook": 5_640, "weather_daily": 3_187, "water_outlook_summary": 1_405, "forecast_runs": 282,
}  # fmt: skip


def test_estimate_reproduces_the_measured_57mb_from_the_e2_row_counts():
    assert set(R.COUNT_TABLES) == set(E2_COUNTS)  # 줄 수를 읽는 표 8개
    assert R.estimate_db_mb(E2_COUNTS) == pytest.approx(57.0, abs=1.0)  # E2 실측 전체 57MB


def test_estimate_grows_with_rows_and_has_a_floor_of_system_and_small_tables():
    empty = R.estimate_db_mb({t: 0 for t in R.COUNT_TABLES})
    assert empty == pytest.approx(R.SYSTEM_MB + R.EXTRA_MB)
    assert R.estimate_db_mb({**{t: 0 for t in R.COUNT_TABLES}, "heat_daily": 1_000_000}) == pytest.approx(
        empty + 1_000_000 * 219 / 1024 / 1024
    )
