"""물 수집기 (collectors/water.py)와 기상청 API 조회 (io/kma.py), 사전 자료 읽기 (io/dictionaries.py).

cropwater `tests/test_ops.py`(feat/cycle02-forecast)의 응답 해석·재시도·백업·인증 오류 테스트를 가져와,
서버 수집기(파일 대신 회차마다 새로 받음)에 맞게 바꿨다. 가짜 API와 가짜 시계로 돈다. 입력은 모두 가짜 값이다.
"""

import datetime as dt
import json
import sys
import urllib.parse
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent / "data"))

from forecast_fixture import issue_items  # noqa: E402

from croplib_dp.collectors import water as W  # noqa: E402
from croplib_dp.io import kma  # noqa: E402
from croplib_dp.io.dictionaries import Dictionaries, crop_params, load_dictionaries, read_table  # noqa: E402

TS = pd.Timestamp
KEY = "SECRET+KEY/123=="


# ── 가짜 API ────────────────────────────────────────────────────────────
def _asos_item(d: dt.date, **kw) -> dict:
    i = d.toordinal()
    it = dict(
        tm=d.isoformat(),
        maxTa=f"{24 + i % 5}",
        minTa=f"{13 + i % 3}",
        avgTa="18.5",
        avgRhm=f"{60 + i % 7}",
        minRhm=f"{38 + i % 9}",
        avgWs=f"{1.5 + (i % 4) * 0.3:.1f}",
        avgPv="14.0",
        avgTd="11.0",
        avgPa="1002.0",
        sumGsr=f"{16 + i % 6}",
        sumSsHr="7.5",
        sumRn="25.0" if i % 17 == 0 else "",
    )
    it.update(kw)
    return it


class FakeApi:
    """단기예보: 발표마다 '나오는 시각'을 정할 수 있다. ASOS: asos_last 날까지만 있다."""

    def __init__(self, clock, issues, ready=None, asos_last=dt.date(2026, 6, 9), fatal=False, sky_missing=()):
        self.clock, self.issues = clock, set(issues)
        self.ready = ready or {}
        self.asos_last, self.fatal, self.sky_missing = asos_last, fatal, set(sky_missing)
        self.calls: list[str] = []

    def _resp(self, code="00", items=(), total=None, msg="NORMAL_SERVICE"):
        body = {"items": {"item": list(items)}, "totalCount": len(items) if total is None else total}
        return 200, json.dumps(
            {"response": {"header": {"resultCode": code, "resultMsg": msg}, "body": body}}
        ).encode()

    def __call__(self, url, timeout):
        self.calls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert q["serviceKey"] == KEY  # 디코딩 키는 URL 인코딩되어 갔다가 그대로 돌아온다
        if self.fatal:
            return 200, b"<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode>" + (
                b"<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg></cmmMsgHeader></OpenAPI_ServiceResponse>"
            )
        page, rows = int(q["pageNo"]), int(q["numOfRows"])
        if "base_date" in q:
            iss = TS(q["base_date"] + q["base_time"])
            if iss not in self.issues or self.clock.now() < self.ready.get(
                iss, iss + pd.Timedelta(minutes=10)
            ):
                return self._resp("03", msg="NO_DATA")
            items = issue_items(iss)
            if iss in self.sky_missing:
                items = [x for x in items if x["category"] not in ("SKY", "POP")]
        else:
            s, e = TS(q["startDt"]).date(), min(TS(q["endDt"]).date(), self.asos_last)
            items = [_asos_item(s + dt.timedelta(days=k)) for k in range((e - s).days + 1)]
        part = items[(page - 1) * rows : page * rows]
        return self._resp(items=part, total=len(items))


class FakeClock:
    def __init__(self, t):
        self.t = TS(t)

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += pd.Timedelta(seconds=sec)


def _dicts() -> Dictionaries:
    stations = [
        dict(
            stn_id="101",
            name_ko="춘천",
            lat=37.90262,
            lon=127.7357,
            elev_m=75.82,
            wind_height_m=10.0,
            grid_nx=73,
            grid_ny=134,
            has_forecast=True,
            sort_order=1,
        ),
        dict(
            stn_id="216",
            name_ko="태백",
            lat=37.17038,
            lon=128.98929,
            elev_m=714.45,
            wind_height_m=16.0,
            grid_nx=95,
            grid_ny=119,
            has_forecast=False,
            sort_order=2,
        ),
    ]
    crops = [
        dict(crop_id="apple", name_ko="사과", kc_ini=0.5, kc_mid=1.2, kc_end=0.95, zr_m=1.0, p=0.5, h_m=3.2),
        dict(
            crop_id="kimchi_cabbage",
            name_ko="배추",
            kc_ini=0.7,
            kc_mid=1.05,
            kc_end=0.95,
            zr_m=0.5,
            p=0.45,
            h_m=0.4,
        ),
    ]
    seasons = [
        dict(
            season_type_id="apple-main",
            crop_id="apple",
            start_mmdd="04-01",
            l_ini_d=20,
            l_dev_d=70,
            l_mid_d=90,
            l_late_d=30,
            is_default=True,
        ),
        dict(
            season_type_id="kimchi_cabbage-summer",
            crop_id="kimchi_cabbage",
            start_mmdd="05-01",
            l_ini_d=25,
            l_dev_d=30,
            l_mid_d=25,
            l_late_d=10,
            is_default=True,
        ),
    ]
    kc = [
        dict(
            kc_set_id="apple-k3",
            crop_id="apple",
            set_no=3,
            kc_ini=0.5,
            kc_mid=1.2,
            kc_end=0.95,
            is_default=True,
        ),
        dict(
            kc_set_id="apple-k1",
            crop_id="apple",
            set_no=1,
            kc_ini=0.45,
            kc_mid=0.95,
            kc_end=0.7,
            is_default=False,
        ),
        dict(
            kc_set_id="kimchi_cabbage-k1",
            crop_id="kimchi_cabbage",
            set_no=1,
            kc_ini=0.7,
            kc_mid=1.05,
            kc_end=0.95,
            is_default=True,
        ),
    ]
    soils = [dict(soil_code="loam", fc=0.25, wp=0.12, is_default=True)]

    def sc(stn, crop, season, k, start, end, taw, raw, default):
        return dict(
            scenario_id=f"{stn}-{season}-k{k}-loam-2026",
            stn_id=stn,
            crop_id=crop,
            season_type_id=season,
            kc_set_id=f"{crop}-k{k}",
            soil_code="loam",
            season_year=2026,
            season_start=start,
            season_end=end,
            dr0_mm=0,
            taw_mm=taw,
            raw_mm=raw,
            is_default=default,
        )

    scenarios = [
        sc("101", "apple", "apple-main", 3, "2026-04-01", "2026-10-27", 130, 65, True),
        sc("101", "apple", "apple-main", 1, "2026-04-01", "2026-10-27", 130, 65, False),
        sc("101", "kimchi_cabbage", "kimchi_cabbage-summer", 1, "2026-05-01", "2026-07-29", 65, 29.25, True),
        sc("216", "apple", "apple-main", 3, "2026-04-01", "2026-10-27", 130, 65, True),
    ]
    return Dictionaries.from_rows(
        dict(
            stations=stations,
            crops=crops,
            crop_seasons=seasons,
            crop_kc_sets=kc,
            soil_types=soils,
            scenarios=scenarios,
        )
    )


ISSUES = [TS("2026-06-09 02:00") + pd.Timedelta(hours=3 * k) for k in range(9)]  # 6/9 02시 ~ 6/10 02시


ISSUE = TS("2026-06-10 02:00")


def _run(start, issues=ISSUES, issue=ISSUE, name="아침", wait=True, **kw):
    clock = FakeClock(start)
    api = FakeApi(clock, issues, **kw)
    r = W.run_slot(name, issue, _dicts(), KEY, http=api, clock=clock, wait=wait)
    return r, api, clock


def _all_text(r) -> str:
    return json.dumps(
        [
            r.forecast_runs,
            r.weather_daily,
            r.water_balance_daily,
            r.water_outlook,
            r.log.entries,
            W.summary_text(r),
        ],
        ensure_ascii=False,
        default=str,
    )


# ── 응답 해석 ───────────────────────────────────────────────────────────
def test_parse_and_classify():
    ok = json.dumps(
        {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "OK"},
                "body": {"items": {"item": {"a": 1}}, "totalCount": 1},
            }
        }
    ).encode()
    p = kma.parse_response(ok)
    assert p["code"] == "00" and p["items"] == [{"a": 1}] and p["total"] == 1  # 항목 하나는 dict로 온다
    xml = (
        b"<x><returnReasonCode>30</returnReasonCode>"
        b"<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED</returnAuthMsg></x>"
    )
    assert kma.parse_response(xml)["code"] == "30"
    assert kma.classify(200, "00") == "ok" and kma.classify(200, "03") == "nodata"
    assert kma.classify(200, "30") == "fatal" and kma.classify(200, 22) == "retry"
    assert (
        kma.classify(401, "") == "fatal"
        and kma.classify(502, "") == "retry"
        and kma.classify(None, "") == "retry"
    )


def test_service_key_encoding_and_mask():
    assert kma.service_key("ab+c/d==") == "ab%2Bc%2Fd%3D%3D"  # 디코딩 키 → 인코딩
    assert kma.service_key("ab%2Bc") == "ab%2Bc"  # 인코딩 키는 그대로
    url = kma.vilage_url(KEY, TS("2026-06-10 02:00"), 73, 134)
    assert "base_date=20260610&base_time=0200&nx=73&ny=134" in url
    assert kma.mask_key(url).count("serviceKey=***") == 1 and "SECRET" not in kma.mask_key(url)
    assert "SECRET" not in kma.mask_key(f"ConnectionError: HTTPSConnectionPool url: {url} (timeout)")


def test_fetch_pages_follows_total():
    items = [{"i": k} for k in range(25)]

    def http(url, timeout):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        page, rows = int(q["pageNo"]), int(q["numOfRows"])
        body = {"items": {"item": items[(page - 1) * rows : page * rows]}, "totalCount": 25}
        return 200, json.dumps({"response": {"header": {"resultCode": "00"}, "body": body}}).encode()

    res = kma.fetch_pages(lambda p: f"http://x/?serviceKey=k&pageNo={p}&numOfRows=10", http, rows=10)
    assert res["status"] == "ok" and len(res["items"]) == 25 and len(res["bodies"]) == 3

    def boom(url, timeout):
        raise TimeoutError(f"read timed out: {url}")

    res = kma.fetch_pages(lambda p: f"http://x/?serviceKey={KEY}&pageNo={p}", boom)
    assert res["status"] == "retry" and "SECRET" not in res["error"]


def test_obs_row_complete():
    assert kma.obs_row_complete(_asos_item(dt.date(2026, 6, 1)))
    assert not kma.obs_row_complete(_asos_item(dt.date(2026, 6, 1), sumGsr=""))
    assert kma.obs_row_complete(_asos_item(dt.date(2026, 6, 1), avgPv="", avgTd=""))  # 평균습도만 있어도 됨


# ── 시계 ────────────────────────────────────────────────────────────────
def test_current_slot_and_parse():
    assert W.current_slot(TS("2026-06-10 02:09")) == (
        "아침",
        TS("2026-06-10 02:00"),
    )  # 1분 일찍 불러도 같은 회차
    assert W.current_slot(TS("2026-06-10 02:05")) == ("저녁", TS("2026-06-09 17:00"))
    assert W.current_slot(TS("2026-06-10 16:00")) == ("아침", TS("2026-06-10 02:00"))
    assert W.current_slot(TS("2026-06-10 18:00")) == ("저녁", TS("2026-06-10 17:00"))
    assert W.parse_slot("2026-06-10 17") == ("저녁", TS("2026-06-10 17:00"))
    with pytest.raises(ValueError):
        W.parse_slot("2026-06-10 05")
    assert W.slot_times(TS("2026-06-10 02:00")) == (TS("2026-06-10 02:10"), TS("2026-06-10 03:30"))
    assert (W.kst_now() - TS.now("UTC").tz_localize(None)).total_seconds() == pytest.approx(9 * 3600, abs=5)


# ── 한 회차 ─────────────────────────────────────────────────────────────
def test_on_time_success():
    r, api, clock = _run("2026-06-10 02:20")
    fr = {x["stn_id"]: x for x in r.forecast_runs}
    assert list(fr) == ["101"]  # 예보 지점만 회차 기록
    x = fr["101"]
    assert x["result"] == "정시 성공" and x["on_time"] and x["attempts"] == 1 and x["rs_method"] == "S4"
    assert x["src_run_at"] is None and x["collector"] == "actions" and x["error"] is None
    assert x["run_at"] == "2026-06-10T02:00:00+09:00" and x["obs_prev_date"] == "2026-06-09"
    fc = [e for e in r.log.entries if e["kind"] == "fcst"]
    assert len(fc) == 9 and fc[0]["target"] == "2026-06-10 02:00"  # 서비스 발표 + 앞선 24시간 8개
    assert {w["stn_id"] for w in r.weather_daily} == {"101", "216"}
    assert max(w["date"] for w in r.weather_daily) == "2026-06-09"
    out = [o for o in r.water_outlook if o["scenario_id"] == "101-apple-main-k3-loam-2026"]
    assert [o["lead_day"] for o in out] == [0, 1, 2, 3]
    assert {o["scenario_id"] for o in r.water_outlook} == {
        "101-apple-main-k3-loam-2026",
        "101-apple-main-k1-loam-2026",
        "101-kimchi_cabbage-summer-k1-loam-2026",
    }
    k1 = [o for o in r.water_outlook if o["scenario_id"] == "101-apple-main-k1-loam-2026"]
    assert [a["eto_mm"] for a in out] == pytest.approx([b["eto_mm"] for b in k1])  # 같은 예보 ETo, Kc만 다름
    assert all(a["etc_mm"] > b["etc_mm"] for a, b in zip(out, k1, strict=True))  # 초생 재배 Kc가 더 큼
    assert all(o["status"] in ("need", "caution", "normal", "dormant") for o in r.water_outlook)  # 과습 없음
    assert not r.failed and clock.now() == TS("2026-06-10 02:20")


def test_water_balance_rows_contract_and_kc_adjust():
    r, _, _ = _run("2026-06-10 02:20")
    wb = [x for x in r.water_balance_daily if x["scenario_id"] == "101-apple-main-k3-loam-2026"]
    assert wb[0]["date"] == "2026-04-01" and wb[-1]["date"] == "2026-06-09"
    assert set(wb[0]) == {"scenario_id", "date", *W.WB_COLS}
    assert wb[0]["kc"] == 0.5 and all(x["calc_version"] == W.CALC_VERSION for x in wb)
    tb = [x for x in r.water_balance_daily if x["scenario_id"] == "216-apple-main-k3-loam-2026"]
    assert tb[-1]["date"] == "2026-06-09"  # 예보가 없는 지점도 관측 물수지는 쓴다
    s = next(s for s in r.stations if s["stn_id"] == "101")
    c = next(c for c in s["scenarios"] if c["scenario_id"] == "101-apple-main-k3-loam-2026")
    assert c["kc_mid"] != 1.2  # 중기가 시작 전이라 기본값(1.5 m/s, 55%)으로 기후보정
    from croplib_dp.core.fao56 import kc_climate_adjust

    assert c["kc_mid"] == pytest.approx(kc_climate_adjust(1.2, 1.5, 55.0, 3.2), abs=1e-4)


def test_delay_then_success_is_on_time():
    ready = {TS("2026-06-10 02:00"): TS("2026-06-10 02:28")}  # 18분 늦게 나옴
    r, api, clock = _run("2026-06-10 02:10", ready=ready)
    x = r.forecast_runs[0]
    assert x["result"] == "정시 성공" and x["on_time"] and x["attempts"] == 5  # 0·5·10·15 실패, 20분에 받음
    assert clock.now() == TS("2026-06-10 02:30")


def test_service_never_comes_uses_previous_issue():
    r, api, clock = _run("2026-06-10 02:10", issues=ISSUES[:-1])  # 02시 발표 없음
    x = r.forecast_runs[0]
    assert x["result"] == "백업: 직전 발표" and not x["on_time"] and x["attempts"] == 9
    assert x["src_run_at"] == "2026-06-09T23:00:00+09:00"
    assert clock.now() == TS("2026-06-10 03:30")  # 마감까지 기다림
    assert r.water_outlook and not r.failed


def test_sky_missing_is_s3_backup():
    r, _, _ = _run("2026-06-10 02:20", sky_missing=[TS("2026-06-10 02:00")])
    x = r.forecast_runs[0]
    assert x["rs_method"] == "S3" and x["result"] == "백업: S3" and not x["on_time"]


def test_fatal_key_error_stops_and_hides_key():
    r, api, clock = _run("2026-06-10 02:20", fatal=True)
    x = r.forecast_runs[0]
    assert x["result"] == "실패" and "30" in x["error"] and x["attempts"] == 1
    assert len([c for c in api.calls if "VilageFcst" in c]) == 1  # 다시 시도하지 않음
    assert r.failed and not r.water_outlook
    assert KEY not in _all_text(r) and kma.service_key(KEY) not in _all_text(r)


def test_late_run_tries_once():
    r, api, clock = _run("2026-06-10 14:00")
    x = r.forecast_runs[0]
    assert x["result"] == "지연 수집" and not x["on_time"] and x["attempts"] == 1


def test_no_wait_tries_once():
    ready = {TS("2026-06-10 02:00"): TS("2026-06-10 02:40")}
    r, _, clock = _run("2026-06-10 02:15", ready=ready, wait=False)
    assert r.forecast_runs[0]["attempts"] == 1 and r.forecast_runs[0]["result"] == "백업: 직전 발표"
    assert clock.now() == TS("2026-06-10 02:15")


def test_yesterday_obs_missing_starts_from_last_observed():
    r, _, _ = _run("2026-06-10 02:20", asos_last=dt.date(2026, 6, 8))
    x = r.forecast_runs[0]
    assert x["obs_prev_date"] == "2026-06-08" and x["result"] == "정시 성공"
    wb = [w for w in r.water_balance_daily if w["scenario_id"] == "101-apple-main-k3-loam-2026"]
    assert (
        wb[-1]["date"] == "2026-06-09" and wb[-1]["eto_src"] == "예보" and wb[-1]["filled"]
    )  # 전날은 예보로
    tb = [w for w in r.water_balance_daily if w["scenario_id"] == "216-apple-main-k3-loam-2026"]
    assert tb[-1]["date"] == "2026-06-08"  # 예보 없는 지점은 관측까지만


def test_no_issue_within_24h_fails():
    r, _, _ = _run("2026-06-10 02:20", issues=[TS("2026-06-08 20:00")])
    x = r.forecast_runs[0]
    assert x["result"] == "실패" and "24시간" in x["error"] and r.failed
    assert any(w["scenario_id"].startswith("101-") for w in r.water_balance_daily)  # 관측 물수지는 남김


def test_evening_slot():
    issues = ISSUES + [TS("2026-06-10 05:00") + pd.Timedelta(hours=3 * k) for k in range(5)]
    r, _, _ = _run("2026-06-10 17:20", issues=issues, issue=TS("2026-06-10 17:00"), name="저녁")
    x = r.forecast_runs[0]
    assert x["result"] == "정시 성공" and x["run_name"] == "저녁"
    out = [o for o in r.water_outlook if o["scenario_id"] == "101-apple-main-k3-loam-2026"]
    assert [o["lead_day"] for o in out] == [1, 2, 3, 4]


def test_write_outputs(tmp_path):
    r, _, _ = _run("2026-06-10 02:20")
    d = W.write_outputs(r, tmp_path)
    assert {p.name for p in d.iterdir()} >= {
        "forecast_runs.json",
        "weather_daily.json",
        "water_balance_daily.json",
        "water_outlook.json",
        "collect_log.jsonl",
        "summary.md",
        "raw",
    }
    assert json.loads((d / "forecast_runs.json").read_text(encoding="utf-8"))[0]["result"] == "정시 성공"
    assert "정시 성공" in (d / "summary.md").read_text(encoding="utf-8")
    assert len(list((d / "raw").glob("*.json.gz"))) >= 10
    text = "".join(p.read_text(encoding="utf-8") for p in d.glob("*.*"))
    assert "SECRET" not in text


# ── 사전 자료 ───────────────────────────────────────────────────────────
def test_read_dictionaries_from_rest():
    rows = {
        "stations": [dict(stn_id="101", sort_order=1)],
        "crops": [dict(crop_id="apple")],
        "crop_seasons": [dict(season_type_id="apple-main")],
        "crop_kc_sets": [dict(kc_set_id="apple-k3")],
        "soil_types": [dict(soil_code="loam")],
        "scenarios": [
            dict(scenario_id="b", stn_id="101", season_year=2026),
            dict(scenario_id="a", stn_id="216"),
        ],
    }
    seen = []

    def http(url, headers):
        seen.append((url, headers))
        table = url.split("/rest/v1/")[1].split("?")[0]
        return 200, json.dumps(rows[table]).encode()

    d = load_dictionaries("https://x.supabase.co/", "srv", http)
    assert [s["scenario_id"] for s in d.scenarios] == ["a", "b"] and list(d.stations) == ["101"]
    assert seen[0][0] == "https://x.supabase.co/rest/v1/stations?select=*"
    assert seen[0][1]["apikey"] == "srv" and seen[0][1]["Authorization"] == "Bearer srv"
    with pytest.raises(RuntimeError, match="crops 읽기 실패"):
        read_table("https://x.supabase.co", "srv", "crops", lambda u, h: (401, b"no"))


def test_crop_params_from_dictionaries():
    d = _dicts()
    s = next(x for x in d.scenarios_for("101") if x["kc_set_id"] == "apple-k3")
    c = crop_params(d, s)
    assert (c.season_start, c.l_ini, c.l_late, c.kc_ini, c.kc_mid, c.kc_end) == (
        dt.date(2026, 4, 1),
        20,
        30,
        0.5,
        1.2,
        0.95,
    )


def test_main_needs_keys_in_environment(monkeypatch):
    for k in ("DATA_GO_KR", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit, match="SUPABASE_URL"):
        W.main(["--slot", "2026-06-10 02", "--no-wait"])


def test_main_runs_and_writes(monkeypatch, tmp_path):
    clock = FakeClock("2026-06-10 02:20")
    api = FakeApi(clock, ISSUES)
    monkeypatch.setenv("DATA_GO_KR", KEY)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "srv")
    monkeypatch.setattr(W, "load_dictionaries", lambda url, key: _dicts())
    monkeypatch.setattr(W, "Clock", lambda: clock)
    monkeypatch.setattr(W.kma, "http_get", api)
    real = W.run_slot
    monkeypatch.setattr(W, "run_slot", lambda *a, **k: real(*a, **{**k, "http": api}))
    assert W.main(["--slot", "2026-06-10 02", "--out", str(tmp_path), "--stations", "101"]) == 0
    assert (tmp_path / "20260610_0200" / "summary.md").exists()
