"""보관·삭제·복원 수집기 (collectors/retention.py, io/storage.py, io/db_stats.py). 이슈 #61(#28 S3-a).

가짜 Supabase(표 + Storage)로 돈다. 입력은 모두 가짜 값이다.
기본값은 읽기만 하는 dry-run 이고, 실제 삭제는 `--through` 와 `--confirm-rows` 가 모두 맞아야 한다.
"""

# ruff: noqa: E501

import gzip
import inspect
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent / "data"))

from retention_fakes import FakeSupabase  # noqa: E402

from croplib_dp.collectors import retention as RT  # noqa: E402
from croplib_dp.core import retention as R  # noqa: E402

NOW = pd.Timestamp(
    "2026-11-08 10:00"
)  # 한국 시각. 14일 기준 → 10/25 부터 남김, --through 가능한 가장 늦은 날 10/24
THROUGH = "2026-10-24"
OUT_PATH = R.archive_path("water_outlook", "2026-10")
SEP_PATH = R.archive_path("water_outlook", "2026-09")


def _env(fake):
    return RT.Env("https://x.supabase.co", "SERVICE-KEY", fake.request, fake.rest_get, fake.head)


def _fake():
    f = FakeSupabase()
    # 보관 대상: 9/30 밤(9월), 10/20 아침·저녁, 10/24 저녁(10월, --through 마지막 날)
    for run_id, at in [
        (6, "2026-09-30T23:00:00+09:00"),
        (1, "2026-10-20T02:00:00+09:00"),
        (2, "2026-10-20T17:00:00+09:00"),
        (3, "2026-10-24T17:00:00+09:00"),
        (4, "2026-10-25T02:00:00+09:00"),  # 남김 (오늘 − 14일)
        (5, "2026-11-07T17:00:00+09:00"),  # 남김
    ]:
        f.add_run(run_id, at, run_name="저녁" if "17:00" in at or "23:00" in at else "아침")
        f.add_outlook(run_id)
        f.add_summary(run_id)
    return f


def _rows(f, table):
    return sorted(f.tables[table], key=lambda r: (r["run_id"], r["scenario_id"], r.get("target_date", "")))


# ── dry-run (plan) ─────────────────────────────────────────────────────
def test_plan_reads_only_and_never_writes():
    f = _fake()
    RT.build_plan(_env(f), NOW)
    assert f.writes == []  # 업로드·삭제·수정 호출이 하나도 없다


def test_plan_reports_rows_runs_months_and_default_through():
    f = _fake()
    p = RT.build_plan(_env(f), NOW)
    assert p.through.isoformat() == THROUGH
    assert p.rows == {
        "water_outlook": 12,
        "water_outlook_summary": 4,
    }  # 대상 4회차(9/30, 10/20 ×2, 10/24) × (3줄 / 1줄)
    assert p.runs == {"water_outlook": 4, "water_outlook_summary": 4}
    assert p.total_rows == 16
    months = {(m.table, m.month): m for m in p.months}
    assert set(months) == {
        ("water_outlook", "2026-09"),
        ("water_outlook", "2026-10"),
        ("water_outlook_summary", "2026-09"),
        ("water_outlook_summary", "2026-10"),
    }
    m = months[("water_outlook", "2026-10")]
    assert m.path == OUT_PATH and m.new_rows == 9 and m.existing_rows == 0 and m.merged_rows == 9
    assert m.gz_bytes > 0 and m.csv_bytes > m.gz_bytes and len(m.sha256) == 64
    assert p.db_bytes_freed == 12 * 247 + 4 * 245


def test_plan_counts_forecast_runs_and_says_none_will_be_deleted():
    f = _fake()
    p = RT.build_plan(_env(f), NOW)
    assert p.forecast_runs_rows == 6
    md = RT.report_markdown(p)
    assert "forecast_runs" in md and "삭제 0" in md


def test_plan_falls_back_to_two_reads_when_the_join_query_is_not_supported_and_gives_the_same_result():
    """조인(forecast_runs!inner) 조회가 400 으로 실패하는 DB 에서도 회차를 먼저 읽고 줄을 따로 읽어 같은 계획이 나온다"""
    a, b = _fake(), _fake()
    b.no_embed = True
    pa, pb = RT.build_plan(_env(a), NOW), RT.build_plan(_env(b), NOW)
    assert pb.rows == pa.rows == {"water_outlook": 12, "water_outlook_summary": 4}
    assert [(m.path, m.sha256) for m in pb.months] == [(m.path, m.sha256) for m in pa.months]
    assert b.writes == []


def test_plan_does_not_hide_other_read_errors_behind_the_fallback():
    f = _fake()

    def broken(url, headers):
        return 500, b"server error"

    env = RT.Env("https://x.supabase.co", "K", f.request, broken, f.head)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        RT.build_plan(env, NOW)


def test_plan_rejects_a_through_date_inside_the_14_day_window():
    with pytest.raises(ValueError, match="14일"):
        RT.build_plan(_env(_fake()), NOW, through=pd.Timestamp("2026-10-25").date())


def test_plan_with_an_earlier_through_only_counts_rows_up_to_that_day():
    f = _fake()
    p = RT.build_plan(_env(f), NOW, through=pd.Timestamp("2026-10-20").date())
    assert p.rows["water_outlook"] == 9 and p.total_rows == 12  # 9/30 + 10/20 두 회차


def test_plan_merges_with_an_existing_month_file_and_counts_both():
    f = _fake()
    first = R.flatten(
        "water_outlook", f.tables["water_outlook"][3], f.tables["forecast_runs"][1]
    )  # run 1, 10/20 아침 한 줄
    f.storage[("raw", OUT_PATH)] = R.encode_archive("water_outlook", [first])
    p = RT.build_plan(_env(f), NOW)
    m = next(m for m in p.months if m.table == "water_outlook" and m.month == "2026-10")
    assert m.existing_rows == 1 and m.merged_rows == 9  # 같은 키는 한 줄로 합쳐진다(1 + 9 − 1)


def test_plan_reports_storage_usage_by_prefix_and_expired_ext_files():
    f = _fake()
    f.storage[("raw", "fcst/73_134/2026/10/20261024_0200.json.gz")] = b"a" * 100
    f.storage[("raw", "fcst/73_134/2020/01/20200101_0200.json.gz")] = b"b" * 50
    f.storage[("raw", "fcst-ext/60_127/2026/06/20260601_0200.json.gz")] = b"c" * 40  # 90일 넘음
    f.storage[("raw", "fcst-ext/60_127/2026/11/20261107_1700.json.gz")] = b"d" * 30
    f.storage[("raw", R.archive_path("water_outlook", "2026-08"))] = (
        b"e" * 20
    )  # 이번에 건드리지 않는 달의 보관 파일
    p = RT.build_plan(_env(f), NOW)
    assert p.storage_usage["fcst/"] == dict(files=2, bytes=150)
    assert p.storage_usage["fcst-ext/"] == dict(files=2, bytes=70)
    assert p.storage_usage["archive/"] == dict(files=1, bytes=20)
    assert p.raw_expired == ["fcst-ext/60_127/2026/06/20260601_0200.json.gz"]
    assert p.raw_expired_bytes == 40


def test_plan_with_no_extra_raw_files_has_zero_expired_and_nothing_to_delete():
    p = RT.build_plan(_env(_fake()), NOW)
    assert p.raw_expired == [] and p.raw_expired_bytes == 0


def _doc_gz(n=3000):
    import random

    rng = random.Random(2)
    pages = [
        "".join(
            f'{{"fcstTime":"{rng.randrange(2400):04d}","fcstValue":"{rng.randrange(40)}"}},' for _ in range(n)
        )
    ]
    return gzip.compress(
        json.dumps(dict(issue="2026-10-24 17:00", grid="73_134", pages=pages)).encode(), mtime=0
    )


def test_plan_estimates_xz_saving_from_a_sample_and_extrapolates_without_writing():
    f = _fake()
    for h in range(5):
        f.storage[("raw", f"fcst/73_134/2026/10/2026102{h}_0200.json.gz")] = _doc_gz()
    p = RT.build_plan(_env(f), NOW, xz_sample=3)
    assert p.xz["n"] == 3 and p.xz["gz_bytes"] > 0 and p.xz["xz_bytes"] > 0
    assert p.xz_total_saving_bytes == pytest.approx(
        p.storage_usage["fcst/"]["bytes"] * (1 - p.xz["ratio"]), rel=0.01
    )
    assert f.writes == []


def test_plan_report_has_the_numbers_needed_for_approval():
    f = _fake()
    md = RT.report_markdown(RT.build_plan(_env(f), NOW))
    for needle in (
        "2026-10-24",
        "water_outlook",
        "water_outlook_summary",
        "archive/water_outlook/2026/water_outlook_2026-10.csv.gz",
        "--confirm-rows 16",
        "16",
    ):
        assert needle in md
    d = RT.report_dict(RT.build_plan(_env(f), NOW))
    assert d["total_rows"] == 16 and d["forecast_runs_deleted"] == 0 and d["through"] == THROUGH


# ── apply: 승인 확인 ───────────────────────────────────────────────────
def test_apply_requires_through_and_matching_confirm_rows_and_deletes_nothing_otherwise():
    f = _fake()
    before = {t: len(v) for t, v in f.tables.items()}
    for through, confirm in [(None, 16), ("2026-10-24", None), ("2026-10-24", 15), ("2026-10-24", 17)]:
        with pytest.raises(RT.RetentionError):
            RT.apply(
                _env(f),
                NOW,
                through=None if through is None else pd.Timestamp(through).date(),
                confirm_rows=confirm,
            )
    assert f.writes == [] and {t: len(v) for t, v in f.tables.items()} == before


def test_apply_refuses_a_through_date_inside_the_14_day_window():
    f = _fake()
    with pytest.raises(ValueError, match="14일"):
        RT.apply(_env(f), NOW, through=pd.Timestamp("2026-10-25").date(), confirm_rows=16)
    assert f.writes == []


# ── apply: 정상 흐름 ───────────────────────────────────────────────────
def _apply(f, through=THROUGH, confirm=16, **kw):
    return RT.apply(_env(f), NOW, through=pd.Timestamp(through).date(), confirm_rows=confirm, **kw)


def test_apply_archives_verifies_then_deletes_and_never_touches_forecast_runs():
    f = _fake()
    originals = {t: [dict(r) for r in _rows(f, t)] for t in ("water_outlook", "water_outlook_summary")}
    res = _apply(f)
    assert res.deleted == {"water_outlook": 12, "water_outlook_summary": 4}
    assert [r["run_id"] for r in _rows(f, "water_outlook")] == [4, 4, 4, 5, 5, 5]  # 14일 안의 줄만 남는다
    assert len(f.tables["forecast_runs"]) == 6  # forecast_runs 는 그대로
    assert not [w for w in f.writes if w[0] == "DELETE" and w[1].endswith("/forecast_runs")]
    # 보관 파일 4개: 9월·10월 × 두 표
    paths = {p for (b, p) in f.storage if p.startswith("archive/")}
    assert paths == {
        SEP_PATH,
        OUT_PATH,
        R.archive_path("water_outlook_summary", "2026-09"),
        R.archive_path("water_outlook_summary", "2026-10"),
    }
    # 보관 파일에서 지운 줄을 그대로 되살릴 수 있다
    back = R.decode_archive("water_outlook", f.storage[("raw", OUT_PATH)])
    got = sorted((R.db_row("water_outlook", r) for r in back), key=lambda r: (r["run_id"], r["target_date"]))
    want = sorted(
        (r for r in originals["water_outlook"] if r["run_id"] in (1, 2, 3)),
        key=lambda r: (r["run_id"], r["target_date"]),
    )
    assert got == want


def test_apply_twice_is_harmless_and_the_second_run_finds_nothing():
    f = _fake()
    _apply(f)
    snapshot = dict(f.storage)
    with pytest.raises(RT.RetentionError):  # 이제 대상이 0줄이라 승인한 16줄과 다르다
        _apply(f)
    res = _apply(f, confirm=0)
    assert res.deleted == {"water_outlook": 0, "water_outlook_summary": 0} and f.storage == snapshot


def test_apply_merges_into_the_same_month_file_across_two_runs():
    f = _fake()
    _apply(f, through="2026-10-20", confirm=12)  # 9/30 + 10/20 (두 회차)
    first = R.decode_archive("water_outlook", f.storage[("raw", OUT_PATH)])
    assert len(first) == 6
    # 10/24 회차가 나중에 14일을 넘는다(날짜가 흘러 같은 달 파일에 합쳐진다)
    res = RT.apply(_env(f), NOW, through=pd.Timestamp(THROUGH).date(), confirm_rows=4)
    assert res.deleted["water_outlook"] == 3
    merged = R.decode_archive("water_outlook", f.storage[("raw", OUT_PATH)])
    assert len(merged) == 9 and merged == R.sort_rows("water_outlook", merged)
    assert len({R.row_key("water_outlook", r) for r in merged}) == 9


# ── apply: 하나라도 어긋나면 지우지 않는다 ─────────────────────────────
def test_a_broken_existing_archive_file_is_never_overwritten():
    f = _fake()
    f.storage[("raw", OUT_PATH)] = b"not a gzip file"
    with pytest.raises(RT.RetentionError, match="기존 보관 파일"):
        RT.build_plan(_env(f), NOW)
    assert f.storage[("raw", OUT_PATH)] == b"not a gzip file" and f.writes == []


def test_corrupted_download_stops_everything_and_deletes_nothing():
    f = _fake()
    f.tamper_on_download = {SEP_PATH}  # 가장 먼저 처리되는 9월 파일을 내려받으면 깨진 바이트
    before = {t: len(v) for t, v in f.tables.items()}
    with pytest.raises(RT.RetentionError, match="내려받"):
        _apply(f)
    assert not [w for w in f.writes if w[0] == "DELETE" and "/rest/v1/" in w[1]]
    assert {t: len(v) for t, v in f.tables.items()} == before


def test_later_month_failure_keeps_the_earlier_verified_month_deleted_and_the_failed_one_intact():
    f = _fake()
    f.tamper_on_download = {OUT_PATH}
    with pytest.raises(RT.RetentionError):
        _apply(f)
    assert {r["run_id"] for r in f.tables["water_outlook"]} >= {1, 2, 3, 4, 5}  # 10월 회차는 그대로
    assert 6 not in {r["run_id"] for r in f.tables["water_outlook"]}  # 확인을 통과한 9월은 이미 지워짐


def test_upload_failure_stops_before_any_delete():
    f = _fake()
    f.upload_fail = {SEP_PATH}
    with pytest.raises(RuntimeError):
        _apply(f)
    assert not [w for w in f.writes if w[0] == "DELETE"]


def test_a_row_added_after_archiving_is_not_deleted_unarchived():
    """보관 파일을 올린 뒤 같은 회차에 새 줄이 생기면(늦은 재시도) 그 줄은 파일에 없으므로 지우지 않고 멈춘다"""
    f = _fake()
    orig = f.request

    def hook(method, url, headers, body):
        out = orig(method, url, headers, body)
        if method == "POST" and url.endswith(OUT_PATH):
            f.add_outlook(1, scenario="S9")  # 이미 올린 파일에는 없는 줄
        return out

    env = RT.Env("https://x.supabase.co", "K", hook, f.rest_get, f.head)
    with pytest.raises(RT.RetentionError, match="보관 파일에 없는"):
        RT.apply(env, NOW, through=pd.Timestamp(THROUGH).date(), confirm_rows=16)
    assert any(r["scenario_id"] == "S9" for r in f.tables["water_outlook"])
    assert {r["run_id"] for r in f.tables["water_outlook"]} >= {1, 2, 3}  # 10월 회차는 지우지 않았다


# ── 복원 ───────────────────────────────────────────────────────────────
def test_restore_dry_run_reports_and_writes_nothing():
    f = _fake()
    _apply(f)
    before = f.writes[:]
    r = RT.restore(_env(f), "water_outlook", "2026-10")
    assert r.rows == 9 and r.missing_runs == [] and r.applied is False
    assert f.writes == before  # dry-run 은 쓰지 않는다


def test_restore_apply_puts_the_same_rows_back():
    f = _fake()
    originals = [dict(r) for r in _rows(f, "water_outlook") if r["run_id"] in (1, 2, 3)]
    _apply(f)
    assert not any(r["run_id"] in (1, 2, 3) for r in f.tables["water_outlook"])
    r = RT.restore(_env(f), "water_outlook", "2026-10", apply=True)
    assert r.applied and r.rows == 9
    got = [dict(x) for x in _rows(f, "water_outlook") if x["run_id"] in (1, 2, 3)]
    assert sorted(got, key=lambda x: (x["run_id"], x["target_date"])) == sorted(
        originals, key=lambda x: (x["run_id"], x["target_date"])
    )


def test_restore_refuses_when_forecast_runs_row_is_missing():
    f = _fake()
    _apply(f)
    f.tables["forecast_runs"] = [r for r in f.tables["forecast_runs"] if r["run_id"] != 2]
    with pytest.raises(RT.RetentionError, match="forecast_runs"):
        RT.restore(_env(f), "water_outlook", "2026-10", apply=True)
    assert not any(r["run_id"] == 2 for r in f.tables["water_outlook"])


def test_restore_of_a_month_without_archive_fails_loudly():
    with pytest.raises(RT.RetentionError, match="보관 파일이 없"):
        RT.restore(_env(_fake()), "water_outlook", "2020-01")


# ── 예보 원문 90일 규칙 ────────────────────────────────────────────────
def _raw_fake():
    f = _fake()
    ext_old = "fcst-ext/60_127/2026/08/20260801_0200.json.gz"  # 99일 → 지움
    ext_edge = "fcst-ext/60_127/2026/08/20260810_1000.json.gz"  # 정확히 90일 → 남김
    ext_new = "fcst-ext/60_127/2026/11/20261107_1700.json.gz"
    rep_old = "fcst/73_134/2020/01/20200101_0200.json.gz"  # 대표 10곳은 아무리 오래돼도 남김
    for p in (ext_old, ext_edge, ext_new, rep_old):
        f.storage[("raw", p)] = _doc_gz(50)
    f.tables["forecast_runs"][0]["raw_path"] = ext_old
    f.tables["forecast_runs"][1]["raw_path"] = rep_old
    f.tables["forecast_runs"][2]["raw_path"] = ext_new
    return f, ext_old, ext_edge, ext_new, rep_old


def test_raw_cleanup_deletes_only_expired_ext_files_and_blanks_their_raw_path():
    f, ext_old, ext_edge, ext_new, rep_old = _raw_fake()
    p = RT.build_plan(_env(f), NOW)
    assert p.raw_expired == [ext_old]
    res = _apply(f, confirm=16, confirm_raw_files=1)
    assert res.raw_deleted == 1
    left = {p for (b, p) in f.storage if b == "raw"}
    assert ext_old not in left and {ext_edge, ext_new, rep_old} <= left
    runs = {r["run_id"]: r["raw_path"] for r in f.tables["forecast_runs"]}
    assert runs[6] is None and runs[1] == rep_old and runs[2] == ext_new  # 지운 파일의 raw_path 만 비운다
    assert len(f.tables["forecast_runs"]) == 6  # 줄은 그대로


def test_raw_cleanup_needs_the_matching_file_count_too():
    f, *_ = _raw_fake()
    with pytest.raises(RT.RetentionError, match="원문"):
        _apply(f, confirm=16, confirm_raw_files=0)
    assert f.writes == []


def test_raw_cleanup_never_touches_representative_or_archive_or_krei_paths():
    f = _fake()
    keep = [
        ("raw", "fcst/73_134/2019/01/20190101_0200.json.gz"),
        ("raw", R.archive_path("water_outlook", "2026-08")),
        ("krei-raw", "text/OVR1/2019/01/1.pages.json.gz"),
    ]
    for k in keep:
        f.storage[k] = b"x"
    res = _apply(f, confirm=16, confirm_raw_files=0)
    assert res.raw_deleted == 0 and all(k in f.storage or k[1] == SEP_PATH for k in keep)
    assert ("raw", "fcst/73_134/2019/01/20190101_0200.json.gz") in f.storage and (
        "krei-raw",
        "text/OVR1/2019/01/1.pages.json.gz",
    ) in f.storage


# ── forecast_runs 는 지우지 않는다 ─────────────────────────────────────
def test_no_code_path_deletes_from_forecast_runs():
    src = inspect.getsource(RT)
    assert '.delete("forecast_runs"' not in src and "delete('forecast_runs'" not in src
    f = _fake()
    _apply(f)
    assert len(f.tables["forecast_runs"]) == 6


# ── CLI ────────────────────────────────────────────────────────────────
def test_cli_default_mode_is_plan_and_writes_report_files_but_no_data(tmp_path):
    f = _fake()
    code = RT.main(["--out", str(tmp_path)], env=_env(f))
    assert code == 0 and f.writes == []
    assert (tmp_path / "retention" / "plan.md").exists() and (tmp_path / "retention" / "plan.json").exists()


def test_cli_apply_without_confirmation_stops_and_deletes_nothing(tmp_path):
    f = _fake()
    with pytest.raises(SystemExit):
        RT.main(["apply", "--through", THROUGH, "--out", str(tmp_path)], env=_env(f))
    assert f.writes == []
    with pytest.raises(SystemExit):
        RT.main(["apply", "--through", THROUGH, "--confirm-rows", "5", "--out", str(tmp_path)], env=_env(f))
    assert f.writes == [] and len(f.tables["water_outlook"]) == 18


def test_cli_apply_with_matching_confirmation_runs_and_writes_a_result_report(tmp_path):
    f = _fake()
    code = RT.main(
        ["apply", "--through", THROUGH, "--confirm-rows", "16", "--out", str(tmp_path)], env=_env(f)
    )
    assert code == 0 and len(f.tables["water_outlook"]) == 6
    assert "16" in (tmp_path / "retention" / "apply.md").read_text(encoding="utf-8")


def test_cli_restore_defaults_to_dry_run(tmp_path):
    f = _fake()
    _apply(f)
    before = f.writes[:]
    RT.main(
        ["restore", "--table", "water_outlook", "--month", "2026-10", "--out", str(tmp_path)], env=_env(f)
    )
    assert f.writes == before


def test_main_reads_keys_only_from_the_environment(monkeypatch, tmp_path):
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        RT.main(["--out", str(tmp_path)])
    src = inspect.getsource(RT)
    assert "SERVICE-KEY" not in src


# ── 보관 파일이 오차표 재집계에 쓸 만한가 ─────────────────────────────
def test_archive_can_rebuild_cum3_forecast_error_with_observed_eto():
    """보관 파일(발표별 예보 ETo) + 관측 weather_daily.eto_mm → 발표별 처음 3개 대상일 합의 오차(cropwater cum3 방식)"""
    f = _fake()
    _apply(f)
    rows = R.decode_archive("water_outlook", f.storage[("raw", OUT_PATH)])
    df = pd.DataFrame(rows).drop_duplicates(
        ["run_at", "stn_id", "target_date"]
    )  # ETo 는 시나리오와 무관 → 지점·날짜당 한 줄
    obs = {f"2026-10-{d}": 3.0 for d in range(20, 23)}  # 가짜 관측 ETo
    df["obs"] = df["target_date"].map(obs)
    g = df[df.lead_day.isin([0, 1, 2])].groupby(["run_name", "run_at"])
    cum = g.agg(fcst=("eto_mm", "sum"), obs=("obs", "sum"), n=("eto_mm", "size"))
    cum = cum[cum.n == 3]
    assert len(cum) >= 1
    rmse = float(((cum.fcst - cum.obs) ** 2).mean() ** 0.5)
    assert rmse == pytest.approx(0.3, abs=1e-9)  # 예보 3.0·3.1·3.2 합 9.3 대 관측 9.0
