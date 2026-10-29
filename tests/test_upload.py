"""올리기 전 계약 검사 (io/contract.py), Supabase 쓰기 (io/supabase_write.py),
회차 올리기 (collectors/water.publish).

가짜 Supabase로 돈다. 입력은 모두 가짜 값이다.
"""

import gzip
import json
import sys
import urllib.parse
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "data"))

from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.io.contract import SPECS, check_rows  # noqa: E402
from croplib_dp.io.supabase_write import SupabaseWriter  # noqa: E402
from test_collector import ISSUE, ISSUES, KEY, FakeApi, FakeClock, _dicts  # noqa: E402

TS = pd.Timestamp


class FakeSupabase:
    """PostgREST·Storage 흉내.
    forecast_runs 는 run_id 를 붙여 UTC 시각으로 돌려준다(실제 응답과 같은 모양)."""

    def __init__(self, fail_storage=False, fail_table=None):
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.tables: dict[str, list[dict]] = {}
        self.objects: dict[str, bytes] = {}
        self.fail_storage, self.fail_table = fail_storage, fail_table
        self.next_id = 100

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, headers, body))
        u = urllib.parse.urlparse(url)
        if u.path.startswith("/storage/v1/object/"):
            if self.fail_storage:
                return 500, b'{"error":"boom"}'
            self.objects[u.path.removeprefix("/storage/v1/object/")] = body
            return 200, b'{"Key":"ok"}'
        table = u.path.removeprefix("/rest/v1/")
        if table == self.fail_table:
            return 400, b'{"message":"violates check constraint"}'
        rows = json.loads(body)
        self.tables.setdefault(table, []).extend(rows)
        if "return=representation" in headers.get("Prefer", ""):
            out = []
            for r in rows:
                self.next_id += 1
                t = TS(r["run_at"]).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S+00:00")
                out.append(dict(r, run_id=self.next_id, run_at=t))
            return 201, json.dumps(out).encode()
        return 201, b""


def _slot():
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    return W.run_slot("아침", ISSUE, _dicts(), KEY, http=api, clock=clock, stations=["101", "216"])


def _taw():
    return W.taw_map(_dicts())


# ── 계약 검사 ───────────────────────────────────────────────────────────
def test_collector_rows_pass_contract():
    r = _slot()
    assert W.check_contract(r, _taw()) == []
    assert set(r.weather_daily[0]) == set(SPECS["weather_daily"].columns)
    assert set(r.water_balance_daily[0]) == set(SPECS["water_balance_daily"].columns)
    assert set(r.forecast_runs[0]) == set(SPECS["forecast_runs"].columns)
    assert set(r.water_outlook[0]) == (set(SPECS["water_outlook"].columns) - {"run_id"}) | set(W.RUN_KEY)
    s = r.water_outlook_summary[0]
    assert set(s) == (set(SPECS["water_outlook_summary"].columns) - {"run_id"}) | set(W.RUN_KEY)
    assert s["horizon_days"] == 3 and s["start_date"] == "2026-06-09"


def test_contract_catches_bad_values():
    r = _slot()
    w = dict(r.weather_daily[0], eto_mm=16.2)
    assert any("eto_mm=16.2 (범위 0~15)" in p for p in check_rows("weather_daily", [w]))
    b = dict(r.water_balance_daily[-1], status="wet", dr_mm=500.0)
    probs = check_rows("water_balance_daily", [b], _taw())
    assert len(probs) == 1 and "dr_mm=500.0 (범위 0~130.0)" in probs[0]  # 과습은 관측 물수지에서는 허용
    o = dict(r.water_outlook[0], status="wet", lead_day=5)
    p = check_rows("water_outlook", [o], _taw(), ignore=("run_id",), extra=W.RUN_KEY)[0]
    assert "status='wet'" in p and "lead_day=5 (범위 0~4)" in p  # 예보 날은 과습 없음, 선행일 0~4
    f = dict(r.forecast_runs[0], result="백업: 직전 발표 (S3)", attempts=0)
    p = check_rows("forecast_runs", [f])[0]
    assert "result=" in p and "attempts=0" in p
    x = dict(r.weather_daily[0], extra_col=1)
    del x["eto_mm"]
    p = check_rows("weather_daily", [x])[0]
    assert "빠진 열 ['eto_mm']" in p and "계약에 없는 열 ['extra_col']" in p
    assert any("같은 키가 두 번" in q for q in check_rows("weather_daily", [r.weather_daily[0]] * 2))
    many = [dict(w, rh_min_pct=120.0) for w in r.weather_daily[:30]]
    probs = check_rows("weather_daily", many)
    assert len(probs) == 11 and "모두 30개" in probs[-1]


# ── Supabase 쓰기 ───────────────────────────────────────────────────────
def test_writer_upsert_batches_and_headers():
    fake = FakeSupabase()
    w = SupabaseWriter("https://x.supabase.co/", "srv", http=fake, batch=2)
    w.upsert("weather_daily", [{"a": i} for i in range(5)], ("stn_id", "date"))
    assert len(fake.requests) == 3 and len(fake.tables["weather_daily"]) == 5
    method, url, headers, _ = fake.requests[0]
    assert method == "POST" and url == "https://x.supabase.co/rest/v1/weather_daily?on_conflict=stn_id,date"
    assert headers["apikey"] == "srv" and headers["Authorization"] == "Bearer srv"
    assert headers["Prefer"] == "resolution=merge-duplicates,return=minimal"
    w.upload("raw", "fcst/73_134/a.json.gz", b"xx")
    assert fake.requests[-1][1] == "https://x.supabase.co/storage/v1/object/raw/fcst/73_134/a.json.gz"
    assert fake.requests[-1][2]["x-upsert"] == "true"


def test_writer_raises_on_error():
    w = SupabaseWriter("https://x.supabase.co", "srv", http=FakeSupabase(fail_table="weather_daily"))
    with pytest.raises(RuntimeError, match="weather_daily 쓰기 실패.*HTTP 400"):
        w.upsert("weather_daily", [{"a": 1}], ("stn_id", "date"))


# ── 회차 올리기 ─────────────────────────────────────────────────────────
def test_publish_writes_all_tables_with_run_id():
    r = _slot()
    fake = FakeSupabase()
    W.publish(r, SupabaseWriter("https://x.supabase.co", "srv", http=fake), _taw())
    assert r.uploaded and not r.problems and not r.failed
    order = [urllib.parse.urlparse(u).path for _, u, _, _ in fake.requests]
    tables = [p.removeprefix("/rest/v1/") for p in order if p.startswith("/rest/")]
    assert tables[0] == "weather_daily" and tables[-2:] == ["water_outlook", "water_outlook_summary"]
    assert tables.index("forecast_runs") < tables.index("water_outlook")
    assert all(p.startswith("/storage/") for p in order[: len(r.raw_files)])  # 원자료를 먼저 올린다
    out = fake.tables["water_outlook"]
    assert {o["run_id"] for o in out} == {101} and not ({"run_at", "stn_id", "collector"} & set(out[0]))
    assert set(out[0]) == set(SPECS["water_outlook"].columns)
    summ = fake.tables["water_outlook_summary"]
    assert len(summ) == 3 and {x["run_id"] for x in summ} == {101}  # 춘천 조건 3개(사과 2, 배추 1)
    assert set(summ[0]) == set(SPECS["water_outlook_summary"].columns)
    assert len(fake.tables["water_balance_daily"]) == len(r.water_balance_daily)
    runs = fake.tables["forecast_runs"]
    assert runs[0]["raw_path"] == "fcst/73_134/2026/06/20260610_0200.json.gz"
    assert "raw/" + runs[0]["raw_path"] in fake.objects
    doc = json.loads(gzip.decompress(fake.objects["raw/" + runs[0]["raw_path"]]))
    assert doc["issue"] == "2026-06-10 02:00" and doc["grid"] == "73_134" and len(doc["pages"]) == 1
    assert len(fake.objects) == 9  # 서비스 발표 + 앞선 24시간 8개, 발표마다 한 파일
    assert all(KEY not in json.dumps(json.loads(b)) for _, _, _, b in fake.requests if b and b[:1] in b"[{")


def test_publish_blocks_on_contract_problem():
    r = _slot()
    r.weather_daily[3]["eto_mm"] = 99.0
    fake = FakeSupabase()
    W.publish(r, SupabaseWriter("https://x.supabase.co", "srv", http=fake), _taw())
    assert not r.uploaded and r.failed and "eto_mm=99.0" in r.problems[0]
    assert list(fake.tables) == ["forecast_runs"] and not fake.objects  # 회차 기록만, 계산 결과는 올리지 않음
    row = fake.tables["forecast_runs"][0]
    assert row["result"] == "실패" and row["error"].startswith("계약 검사: weather_daily 4번째 줄")
    assert "DB 올리기: 안 함" in W.summary_text(r) and "## 문제" in W.summary_text(r)


def test_publish_continues_when_storage_fails():
    r = _slot()
    fake = FakeSupabase(fail_storage=True)
    W.publish(r, SupabaseWriter("https://x.supabase.co", "srv", http=fake), _taw())
    assert r.uploaded and r.failed and "Storage raw/" in r.problems[0]
    assert fake.tables["forecast_runs"][0]["raw_path"] is None and fake.tables["water_outlook"]


def test_publish_raises_when_table_write_fails():
    r = _slot()
    with pytest.raises(RuntimeError, match="water_balance_daily 쓰기 실패"):
        W.publish(
            r,
            SupabaseWriter(
                "https://x.supabase.co", "srv", http=FakeSupabase(fail_table="water_balance_daily")
            ),
            _taw(),
        )


def test_rerun_same_slot_overwrites_not_duplicates():
    fake = FakeSupabase()
    for _ in range(2):
        W.publish(_slot(), SupabaseWriter("https://x.supabase.co", "srv", http=fake), _taw())
    urls = {u for _, u, _, _ in fake.requests if "/rest/" in u}
    assert "https://x.supabase.co/rest/v1/forecast_runs?on_conflict=run_at,stn_id,collector" in urls
    assert "https://x.supabase.co/rest/v1/water_outlook?on_conflict=run_id,scenario_id,target_date" in urls


def test_main_write_option(monkeypatch, tmp_path):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    fake = FakeSupabase()
    monkeypatch.setenv("DATA_GO_KR", KEY)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "srv")
    monkeypatch.setattr(W, "load_dictionaries", lambda url, key: _dicts())
    monkeypatch.setattr(W, "Clock", lambda: clock)
    real_run, real_writer = W.run_slot, SupabaseWriter
    monkeypatch.setattr(W, "run_slot", lambda *a, **k: real_run(*a, **{**k, "http": api}))
    import croplib_dp.io.supabase_write as SW

    monkeypatch.setattr(SW, "SupabaseWriter", lambda url, key: real_writer(url, key, http=fake))
    assert W.main(["--slot", "2026-06-10 02", "--out", str(tmp_path), "--write"]) == 0
    assert fake.tables["water_outlook"] and "DB 올리기: 완료" in (
        tmp_path / "20260610_0200" / "summary.md"
    ).read_text(encoding="utf-8")


# ── 예약을 여러 번 걸어 둔 실행: 이미 끝난 회차는 건너뛰기 ─────────────
def test_slot_done_only_when_on_time_success():
    rows = {"value": []}
    seen = []

    def db(url, headers):
        seen.append(url)
        return 200, json.dumps(rows["value"]).encode()

    d = _dicts()
    assert not W.slot_done("https://x.supabase.co", "srv", d, ISSUE, db)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(seen[0]).query))
    assert q["run_at"] == "eq.2026-06-10T02:00:00+09:00" and q["collector"] == "eq.actions"
    rows["value"] = [{"stn_id": "101", "result": "지연 수집"}]
    assert not W.slot_done("https://x.supabase.co", "srv", d, ISSUE, db)  # 지연·백업이면 다시 돈다
    rows["value"] = [{"stn_id": "101", "result": "정시 성공"}]
    assert W.slot_done("https://x.supabase.co", "srv", d, ISSUE, db)


def test_main_skip_if_done(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_GO_KR", KEY)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "srv")
    monkeypatch.setattr(W, "load_dictionaries", lambda url, key: _dicts())
    monkeypatch.setattr(W, "slot_done", lambda *a, **k: True)
    monkeypatch.setattr(W, "run_slot", lambda *a, **k: pytest.fail("이미 끝난 회차를 다시 돌림"))
    assert W.main(["--slot", "2026-06-10 02", "--out", str(tmp_path), "--write", "--skip-if-done"]) == 0


def test_contract_catches_bad_summary():
    r = _slot()
    s = dict(r.water_outlook_summary[0], need_center_in=-1, status_start="dry", dr_start_mm=999.0)
    p = check_rows("water_outlook_summary", [s], _taw(), ignore=("run_id",), extra=W.RUN_KEY)[0]
    assert "need_center_in=-1" in p and "status_start='dry'" in p and "dr_start_mm=999.0" in p


def test_soil_water_daily_contract_and_publish():
    r = _slot()
    assert r.soil_water_daily
    assert set(r.soil_water_daily[0]) == set(SPECS["soil_water_daily"].columns)
    bad = dict(r.soil_water_daily[-1], aw_pct=105.0, dr_mm=999.0, status="need")
    p = check_rows("soil_water_daily", [bad])[0]
    assert "aw_pct=105.0" in p and "dr_mm=999.0" in p and "status='need'" in p
    fake = FakeSupabase()
    W.publish(r, SupabaseWriter("https://x.supabase.co", "srv", http=fake), _taw())
    assert len(fake.tables["soil_water_daily"]) == len(r.soil_water_daily)
    urls = {u for _, u, _, _ in fake.requests if "/rest/" in u}
    assert "https://x.supabase.co/rest/v1/soil_water_daily?on_conflict=stn_id,soil_code,date" in urls
