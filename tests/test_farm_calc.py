"""농장 계산 작업 (collectors/farm_calc.py, io/farm_db.py). 이슈 #131, 결정 0040.

공개 조건 계산(`run_slot`, 가짜 API)이 만든 `weather_daily` · `forecast_runs` · `water_outlook` 줄을
"가짜 DB"로 넣고, 가짜 농장(모두 가짜 값)을 계산해 공개 시나리오 결과와 비교한다. 가짜 DB 는 숫자를 문자열로
돌려줘 실제처럼 "문자열 → 수"로 되읽는 길을 거친다. 허용 오차는 상대 1e-9: 공개 줄과 같은 식 · 같은 입력이라
거의 같고, 토성 TAW 의 부동소수 끝자리만 다를 수 있다.
"""

import copy
import datetime as dt
import json
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from croplib_dp.collectors import farm_calc as F  # noqa: E402
from croplib_dp.core import CALC_VERSION  # noqa: E402
from croplib_dp.io.farm_db import FarmDb  # noqa: E402
from test_water_protected import protected_dicts  # noqa: E402
from test_water_protected import run as public_run  # noqa: E402

NOW = pd.Timestamp("2026-06-10 02:30", tz="Asia/Seoul")  # 공개 아침 회차(02:00) 직후
FARM = "11111111-1111-4111-8111-111111111111"
FARM2 = "22222222-2222-4222-8222-222222222222"
FARM3 = "33333333-3333-4333-8333-333333333333"
SECRET_AREA = 1234.5  # 농장 정보가 로그에 새는지 보려고 일부러 눈에 띄는 값
SECRET_L = 98765.0


def _strs(row: dict) -> dict:
    """PostgREST 가 numeric 을 글자로 돌려주는 경우를 흉내: 소수 값은 글자로"""
    return {k: (str(v) if isinstance(v, float) else v) for k, v in row.items()}


class FakeDb:
    """FarmDb 와 같은 이름의 메서드를 가진 메모리 DB. 호출 수와 쓴 줄을 센다."""

    def __init__(
        self,
        farms=(),
        seasons=(),
        logs=(),
        status=(),
        weather=(),
        runs=(),
        outlook=(),
        fetched=None,
        strs=True,
    ):
        self.f, self.s, self.l = [dict(x) for x in farms], [dict(x) for x in seasons], [dict(x) for x in logs]
        self.st = {x["farm_id"]: dict(x) for x in status}
        self.wx, self.rn, self.ol = (
            [dict(x) for x in weather],
            [dict(x) for x in runs],
            [dict(x) for x in outlook],
        )
        self.fetched, self.strs = dict(fetched or {}), strs
        self.daily, self.outlook, self.summary = {}, {}, {}
        self.calls, self.synced, self.hooks = Counter(), 0, {}

    def _hook(self, name):
        if name in self.hooks:
            self.hooks.pop(name)(self)

    # 읽기
    def farms(self):
        self.calls["farms"] += 1
        return copy.deepcopy(self.f)

    def seasons(self):
        self.calls["seasons"] += 1
        return copy.deepcopy(self.s)

    def logs(self):
        self.calls["logs"] += 1
        return copy.deepcopy(self.l)

    def status(self):
        self.calls["status"] += 1
        return copy.deepcopy(list(self.st.values()))

    def existing_daily(self, ids):
        self.calls["existing_daily"] += 1
        return [dict(farm_id=k[0], date=k[1]) for k in sorted(self.daily) if k[0] in ids]

    def weather(self, stn, start):
        self.calls["weather"] += 1
        self._hook("weather")
        rows = [r for r in self.wx if r["stn_id"] == stn and r["date"] >= start]
        return [_strs(r) if self.strs else r for r in sorted(rows, key=lambda r: r["date"])]

    def latest_fetched(self, stn):
        self.calls["latest_fetched"] += 1
        return self.fetched.get(stn)

    def runs(self, since):
        self.calls["runs"] += 1
        return copy.deepcopy(self.rn)

    def outlook_src(self, run_ids):
        self.calls["outlook_src"] += 1
        ids = set(run_ids)
        return [_strs(r) if self.strs else r for r in copy.deepcopy(self.ol) if r["run_id"] in ids]

    # 쓰기
    def sync_all(self):
        self.synced += 1
        return len(self.f)

    def upsert(self, table, rows):
        for r in rows:
            r = dict(r)
            if table == "farm_water_daily":
                self.daily[(r["farm_id"], r["date"])] = r
            elif table == "farm_water_outlook":
                self.outlook[(r["farm_id"], r["target_date"])] = r
            elif table == "farm_water_outlook_summary":
                self.summary[r["farm_id"]] = r

    def put_status(self, farm_id, done_at, result, error, calc_version):
        self.st.setdefault(farm_id, dict(farm_id=farm_id, requested_at=None, recalc_from=None))
        self.st[farm_id].update(done_at=done_at, result=result, error=error, calc_version=calc_version)

    def delete_daily(self, farm_id, dates):
        gone = [k for k in self.daily if k[0] == farm_id and k[1] in set(dates)]
        for k in gone:
            del self.daily[k]
        return len(gone)

    def delete_outlook(self, farm_id, keep_run_id=None):
        gone = [
            k
            for k, v in self.outlook.items()
            if k[0] == farm_id and (keep_run_id is None or v["run_id"] != keep_run_id)
        ]
        for k in gone:
            del self.outlook[k]
        return len(gone)

    def delete_summary(self, farm_id):
        return 1 if self.summary.pop(farm_id, None) is not None else 0

    def farm_daily(self, farm_id):
        return {k[1]: v for k, v in sorted(self.daily.items()) if k[0] == farm_id}


# ── 공개 조건 결과로 만든 가짜 DB 재료 ───────────────────────────────────────────────
@pytest.fixture(scope="module")
def world():
    r, d = public_run(protected_dicts(("101",)))
    runs = []
    for i, x in enumerate(r.forecast_runs):
        runs.append(dict(x, run_id=100 + i))
    rid = {(x["run_at"], x["stn_id"]): x["run_id"] for x in runs}
    outlook = [dict(o, run_id=rid[(o["run_at"], o["stn_id"])]) for o in r.water_outlook]
    weather = [dict(w, stn_id="101") for w in r.weather_daily]
    return dict(r=r, d=d, runs=runs, outlook=outlook, weather=weather)


def make_db(world, farms, seasons, logs=(), status=None, **kw):
    runs = world["runs"]
    return FakeDb(
        farms=farms,
        seasons=seasons,
        logs=logs,
        status=status
        if status is not None
        else [dict(farm_id=f["farm_id"], requested_at="2026-06-10T01:00:00+00:00") for f in farms],
        weather=world["weather"],
        runs=runs,
        outlook=world["outlook"],
        **kw,
    )


def farm(fid=FARM, crop="apple", season="apple-main", **kw):
    return dict(
        farm_id=fid,
        stn_id="101",
        soil_code="loam",
        irrigated_area_m2=1000.0,
        crop_id=crop,
        season_type_id=season,
        kc_set_id=None,
        **kw,
    )


def fseason(
    fid=FARM, sid="s-" + FARM[:4], crop="apple", season="apple-main", start="2026-04-01", end="2026-10-27"
):
    return dict(
        farm_season_id=sid,
        farm_id=fid,
        crop_id=crop,
        season_type_id=season,
        kc_set_id=None,
        start_date=start,
        end_date=end,
        start_unset=False,
    )


def go(db, world, **kw):
    return F.run(db, world["d"], now=kw.pop("now", NOW), **kw)


def public_rows(world, table, sid):
    return [x for x in world["r"].__dict__[table] if x["scenario_id"] == sid]


def same(a, b, path=""):
    if isinstance(a, dict):
        assert a.keys() == b.keys(), (path, sorted(a.keys() ^ b.keys()))
        for k in a:
            same(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), (path, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            same(x, y, f"{path}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert a is not None and b is not None and a == pytest.approx(b, rel=1e-9, abs=1e-12), (path, a, b)
    else:
        assert a == b, (path, a, b)


# ── ⑥ ⑪ 노지: 기록이 없으면 같은 조건의 공개 결과와 같은 값 ─────────────────────────────
def test_open_field_without_logs_equals_the_public_scenario_water_balance_outlook_and_summary(world):
    db = make_db(world, [farm()], [fseason()])
    res = go(db, world)
    assert res.n_ok == 1 and res.n_failed == 0 and not res.failed
    pub = public_rows(world, "water_balance_daily", "101-apple-main-k3-loam-2026")
    mine = db.farm_daily(FARM)
    assert [x["date"] for x in pub] == list(mine)
    for p in pub:
        m = mine[p["date"]]
        for k in (
            "kc",
            "eto_mm",
            "eto_src",
            "etc_mm",
            "rain_mm",
            "irr_net_mm",
            "dr_prev_mm",
            "ks",
            "etc_adj_mm",
            "dp_mm",
            "dr_mm",
            "status",
            "filled",
            "calc_version",
        ):
            same(m[k], p[k], f"{p['date']}.{k}")
        assert m["irr_l"] == 0 and m["farm_season_id"] == "s-" + FARM[:4]
        assert m["taw_mm"] == pytest.approx(130.0) and m["raw_mm"] == pytest.approx(65.0)
    pub_o = public_rows(world, "water_outlook", "101-apple-main-k3-loam-2026")
    mine_o = [v for k, v in sorted(db.outlook.items())]
    assert len(mine_o) == len(pub_o) == 4
    for p, m in zip(pub_o, mine_o, strict=True):
        same(
            {k: m[k] for k in p if k not in ("run_at", "stn_id", "collector", "scenario_id")},
            {k: p[k] for k in p if k not in ("run_at", "stn_id", "collector", "scenario_id")},
        )
        assert m["run_id"] == world["runs"][0]["run_id"]
    pub_s = public_rows(world, "water_outlook_summary", "101-apple-main-k3-loam-2026")[0]
    mine_s = db.summary[FARM]
    same(
        {k: mine_s[k] for k in pub_s if k not in ("run_at", "stn_id", "collector", "scenario_id")},
        {k: pub_s[k] for k in pub_s if k not in ("run_at", "stn_id", "collector", "scenario_id")},
    )
    assert (
        db.st[FARM]["result"] == "ok"
        and db.st[FARM]["error"] is None
        and db.st[FARM]["calc_version"] == CALC_VERSION
    )


def test_protected_open_conditions_without_logs_match_the_public_etc_only_rows(world):
    f = farm(crop="grape", season="grape-heated")
    s = fseason(crop="grape", season="grape-heated", start="2026-01-01", end="2026-07-09", sid="s-heat")
    db = make_db(world, [f], [s])
    go(db, world)
    pub = public_rows(world, "water_balance_daily", "101-grape-heated-k1-loam-2026")
    mine = db.farm_daily(FARM)
    assert [x["date"] for x in pub] == list(mine) and {m["status"] for m in mine.values()} == {"etc_only"}
    for p in pub:
        for k in ("kc", "eto_mm", "etc_mm", "status", "dr_mm", "ks", "irr_net_mm"):
            same(mine[p["date"]][k], p[k], f"{p['date']}.{k}")
    pub_o = public_rows(world, "water_outlook", "101-grape-heated-k1-loam-2026")
    mine_o = [v for k, v in sorted(db.outlook.items())]
    assert [o["status"] for o in mine_o] == [o["status"] for o in pub_o] == ["etc_only"] * 4
    for p, m in zip(pub_o, mine_o, strict=True):
        for k in ("eto_mm", "etc_mm", "etc_err_mm", "rain_fcst_mm", "dr_center_mm"):
            same(m[k], p[k], k)


# ── 노지에 기록이 있으면 그날부터 Dr 가 줄어듦 · 오늘 기록 ───────────────────────────────
def test_open_field_with_logs_lowers_dr_and_today_record_reaches_the_lead_0_step(world):
    f, s = farm(), fseason()
    base = make_db(world, [f], [s])
    go(base, world)
    logs = [
        dict(log_id=1, farm_id=FARM, date="2026-06-01", volume_l=30000.0),
        dict(log_id=2, farm_id=FARM, date="2026-06-10", volume_l=30000.0),
    ]
    db = make_db(world, [f], [s], logs)
    go(db, world)
    a, b = base.farm_daily(FARM), db.farm_daily(FARM)
    assert a["2026-05-31"]["dr_mm"] == b["2026-05-31"]["dr_mm"] and b["2026-06-01"]["irr_l"] == 30000.0
    assert (
        b["2026-06-01"]["dr_mm"] < a["2026-06-01"]["dr_mm"] and "2026-06-10" not in b
    )  # 오늘 줄은 만들지 않는다
    oa, ob = base.outlook, db.outlook
    k0 = (FARM, "2026-06-10")
    assert ob[k0]["dr_center_mm"] < oa[k0]["dr_center_mm"]  # 오늘 기록이 오늘 단계에 들어감


# ── ① 해를 넘는 작기 ─────────────────────────────────────────────────────────
def wx_rows(start, n, eto=3.0, rain=0.0):
    s = dt.date.fromisoformat(start)
    return [
        dict(
            stn_id="101",
            date=(s + dt.timedelta(days=i)).isoformat(),
            eto_mm=eto,
            rain_mm=rain,
            filled=False,
            u2_ms=1.5,
            rh_min_pct=45.0,
            fetched_at="2027-02-10T00:00:00+00:00",
        )
        for i in range(n)
    ]


def test_season_crossing_the_new_year_keeps_dr_continuous_and_is_not_cut_at_jan_1(world):
    now = pd.Timestamp("2027-02-10 10:00", tz="Asia/Seoul")
    f = farm()
    s = fseason(start="2026-12-01", end="2027-06-30", sid="s-x")
    db = FakeDb(
        [f],
        [s],
        [],
        [dict(farm_id=FARM, requested_at="2027-02-10T00:00:00+00:00")],
        wx_rows("2026-12-01", 72, eto=4.0),
        [],
        [],
        strs=True,
    )
    res = F.run(db, world["d"], now=now)
    rows = db.farm_daily(FARM)
    assert (
        res.n_ok == 1
        and "2026-12-01" in rows
        and rows["2027-02-09"]["date"] == "2027-02-09"
        and len(rows) == 71
    )
    assert (
        rows["2027-01-01"]["dr_prev_mm"] == pytest.approx(rows["2026-12-31"]["dr_mm"])
        and rows["2027-01-01"]["dr_prev_mm"] > 0
    )
    assert (
        res.n_outlook_warn == 1 and not db.outlook and FARM not in db.summary
    )  # 공개 전망 줄이 없으면 경고(실패 아님)


# ── ② 시작일을 늦춰 기록 · 줄이 작기 밖이 되는 경우 ─────────────────────────────────────
def test_moving_the_start_later_ignores_old_logs_and_removes_old_rows(world):
    f = farm()
    db = make_db(
        world,
        [f],
        [fseason(start="2026-06-03", end="2026-12-01")],
        [
            dict(log_id=1, farm_id=FARM, date="2026-06-01", volume_l=5000.0),
            dict(log_id=2, farm_id=FARM, date="2026-06-05", volume_l=1000.0),
        ],
    )
    for d_ in ("2026-05-20", "2026-06-01", "2026-06-02"):  # 옛 시작일 때 올라간 줄
        db.daily[(FARM, d_)] = dict(farm_id=FARM, date=d_)
    res = go(db, world)
    rows = db.farm_daily(FARM)
    assert (
        min(rows) == "2026-06-03" and sum(r["irr_l"] for r in rows.values()) == 1000.0
    )  # 작기 밖 기록은 irr_l 0
    assert res.deleted_rows >= 3 and not {"2026-05-20", "2026-06-01", "2026-06-02"} & set(rows)


def test_past_season_rows_are_kept_and_not_recalculated(world):
    old = fseason(sid="old", start="2026-01-01", end="2026-03-31")
    cur = fseason(sid="cur", start="2026-04-01", end="2026-10-27")
    db = make_db(world, [farm()], [old, cur])
    db.daily[(FARM, "2026-02-01")] = dict(farm_id=FARM, date="2026-02-01", marker="old")
    go(db, world)
    assert db.daily[(FARM, "2026-02-01")].get("marker") == "old"  # 끝난 지 오래된 작기 줄은 그대로


def test_season_that_ended_two_days_ago_gets_its_last_day_recalculated(world):
    old = fseason(sid="old", start="2026-04-01", end="2026-06-08")
    nxt = fseason(sid="nxt", start="2027-04-01", end="2027-10-27")
    db = make_db(world, [farm()], [old, nxt])
    go(db, world)
    rows = db.farm_daily(FARM)
    assert max(rows) == "2026-06-08" and rows["2026-06-08"]["farm_season_id"] == "old"  # 마지막 날까지 채워짐
    assert not db.outlook and FARM not in db.summary  # 시작 전인 다음 작기: 전망 · 요약 줄 없음


def test_season_not_started_yet_has_no_rows_and_old_outlook_rows_are_deleted(world):
    db = make_db(world, [farm()], [fseason(start="2026-07-01", end="2026-11-30")])
    db.outlook[(FARM, "2026-06-09")] = dict(farm_id=FARM, run_id=1, target_date="2026-06-09")
    db.summary[FARM] = dict(farm_id=FARM, run_id=1)
    res = go(db, world)
    assert res.n_ok == 1 and not db.farm_daily(FARM) and not db.outlook and FARM not in db.summary


# ── ③ ④ ⑤ 시설 ───────────────────────────────────────────────────────────────
def prot_db(world, logs=(), status=None, season_start="2026-06-01"):
    f = farm(crop="grape", season="grape-heated")
    s = fseason(crop="grape", season="grape-heated", start=season_start, end="2026-11-10", sid="s-p")
    return make_db(world, [f], [s], list(logs), status=status)


def test_protected_unset_start_with_no_records_is_all_etc_only(world):
    db = prot_db(world)
    go(db, world)
    rows = db.farm_daily(FARM)
    assert rows and {r["status"] for r in rows.values()} == {"etc_only"}
    assert all(
        r[c] is None for r in rows.values() for c in ("dr_prev_mm", "ks", "etc_adj_mm", "dp_mm", "dr_mm")
    )
    assert [o["status"] for o in db.outlook.values()] == ["etc_only"] * 4 and db.summary[FARM][
        "status_start"
    ] == "etc_only"


def test_protected_first_record_day_ends_at_dr_zero_and_deleting_it_moves_the_first_record(world):
    logs = [
        dict(log_id=1, farm_id=FARM, date="2026-06-05", volume_l=9000.0),
        dict(log_id=2, farm_id=FARM, date="2026-06-08", volume_l=9000.0),
    ]
    db = prot_db(world, logs)
    go(db, world)
    rows = db.farm_daily(FARM)
    assert (
        rows["2026-06-04"]["status"] == "etc_only"
        and rows["2026-06-05"]["dr_mm"] == 0.0
        and rows["2026-06-05"]["status"] != "etc_only"
    )
    assert rows["2026-06-06"]["dr_prev_mm"] == 0.0 and rows["2026-06-06"]["dr_mm"] > 0  # 비 0, ETc 만큼 빠짐
    assert [o["status"] for o in db.outlook.values()] != ["etc_only"] * 4  # 기록이 있으니 Dr 경로
    # 첫 기록(6/5)을 지우면 다음 기록(6/8)이 첫 기록 — 6/5~6/7 줄이 etc_only 로 돌아간다(같은 키로 덮어씀)
    db.l = [x for x in db.l if x["log_id"] != 1]
    db.st[FARM]["requested_at"] = "2026-06-10T01:20:00+00:00"
    go(db, world)
    rows = db.farm_daily(FARM)
    assert rows["2026-06-05"]["status"] == "etc_only" and rows["2026-06-05"]["dr_mm"] is None
    assert rows["2026-06-08"]["dr_mm"] == 0.0
    db.l = []
    db.st[FARM]["requested_at"] = "2026-06-10T01:40:00+00:00"
    go(db, world)  # 기록이 모두 없으면 작기 전체가 다시 etc_only
    assert {r["status"] for r in db.farm_daily(FARM).values()} == {"etc_only"}


def test_protected_first_record_today_makes_today_end_at_zero_in_the_outlook(world):
    db = prot_db(world, [dict(log_id=1, farm_id=FARM, date="2026-06-10", volume_l=9000.0)])
    go(db, world)
    assert {r["status"] for r in db.farm_daily(FARM).values()} == {"etc_only"}  # 어제까지 기록 0건
    o = {k[1]: v for k, v in db.outlook.items()}
    assert o["2026-06-10"]["dr_center_mm"] == 0.0 and o["2026-06-11"]["dr_center_mm"] > 0
    assert db.summary[FARM]["start_date"] == "2026-06-10" and db.summary[FARM]["dr_start_mm"] == 0.0


# ── ⑧ done_at = 읽은 requested_at, 계산 도중 들어온 요청은 다음 차례 ───────────────────────
def test_done_at_is_the_requested_at_read_and_a_request_arriving_during_the_run_stays_pending(world):
    db = make_db(
        world,
        [farm()],
        [fseason()],
        status=[dict(farm_id=FARM, requested_at="2026-06-10T01:00:00+00:00", done_at=None)],
    )

    def new_request(d):  # 계산 도중(기상을 읽을 때) 회원이 또 저장
        d.st[FARM]["requested_at"] = "2026-06-10T01:29:00+00:00"

    db.hooks["weather"] = new_request
    go(db, world)
    st = db.st[FARM]
    assert st["done_at"] == "2026-06-10T01:00:00+00:00"  # 읽은 시각의 값
    assert F.is_pending(st) is True  # requested_at(01:29) > done_at → 다음 차례에 다시 잡힌다
    res2 = go(db, world)
    assert res2.n_pending == 1 and F.is_pending(db.st[FARM]) is False


def test_non_pending_farm_gets_the_read_start_time_as_done_at(world):
    db = make_db(world, [farm()], [fseason()], status=[dict(farm_id=FARM, requested_at=None, done_at=None)])
    go(db, world)
    assert db.st[FARM]["done_at"] == NOW.tz_convert("UTC").isoformat() and db.st[FARM]["result"] == "ok"


# ── ⑨ 한 농장 실패가 다른 농장을 막지 않는다 ───────────────────────────────────────────
def test_one_failing_farm_does_not_stop_the_others_and_error_is_a_short_safe_phrase(world):
    bad = farm(FARM2, season="no-such-season")
    db = make_db(world, [farm(), bad], [fseason(), fseason(FARM2, sid="s2", season="no-such-season")])
    res = go(db, world)
    assert res.n_ok == 1 and res.n_failed == 1 and res.failed
    assert db.st[FARM]["result"] == "ok" and db.farm_daily(FARM)
    assert db.st[FARM2]["result"] == "failed" and "찾지 못함" in db.st[FARM2]["error"]
    assert FARM2 not in db.st[FARM2]["error"] and not db.farm_daily(FARM2)


def test_unreadable_is_protected_fails_that_farm_not_silently_as_open_field(world):
    d = copy.deepcopy(world["d"])
    d.seasons["grape-heated"].pop("is_protected")
    db = prot_db(world)
    res = F.run(db, d, now=NOW)
    assert res.n_failed == 1 and "is_protected" in db.st[FARM]["error"] and not db.farm_daily(FARM)


def test_unexpected_error_is_recorded_with_the_kind_only(world):
    db = make_db(world, [farm()], [fseason()])
    db.hooks["weather"] = lambda d: (_ for _ in ()).throw(KeyError("secret-farm-info"))
    res = go(db, world)
    assert (
        res.n_failed == 1
        and db.st[FARM]["error"] == "계산 오류(KeyError)"
        and "secret" not in db.st[FARM]["error"]
    )


# ── ⑩ 작물 공통 ─────────────────────────────────────────────────────────────
def test_common_crop_farm_is_closed_ok_without_calculation_and_old_rows_are_cleaned(world):
    f = dict(farm("common", crop="common", season=None))
    f["farm_id"] = FARM3
    f["season_type_id"] = None
    db = make_db(world, [f], [], status=[dict(farm_id=FARM3, requested_at="2026-06-10T01:00:00+00:00")])
    db.daily[(FARM3, "2026-05-01")] = dict(farm_id=FARM3, date="2026-05-01", farm_season_id=None)
    db.outlook[(FARM3, "2026-06-10")] = dict(farm_id=FARM3, run_id=100, target_date="2026-06-10")
    db.summary[FARM3] = dict(farm_id=FARM3, run_id=100)
    res = go(db, world)
    assert res.n_common == 1 and res.n_ok == 0 and res.n_failed == 0 and not res.failed
    assert db.st[FARM3]["result"] == "ok" and db.st[FARM3]["done_at"] == "2026-06-10T01:00:00+00:00"
    assert (
        not db.daily and not db.outlook and FARM3 not in db.summary
    )  # 작물 → 작물 공통으로 바꾼 농장의 남은 줄 정리
    assert db.calls["weather"] == 0  # 계산하지 않았다


def test_common_crop_farm_is_not_part_of_the_stale_selection(world):
    f = farm(crop="common", season=None)
    f["season_type_id"] = None
    db = make_db(world, [f], [], status=[dict(farm_id=FARM, requested_at=None, done_at=None)])
    res = go(db, world)
    assert res.selected == 0 and db.st[FARM]["done_at"] is None


# ── ⑫ 계약 검사 ─────────────────────────────────────────────────────────────
def test_contract_violation_fails_the_farm_and_writes_nothing(world):
    db = make_db(
        world, [farm()], [fseason()], [dict(log_id=1, farm_id=FARM, date="2026-06-02", volume_l=900000000.0)]
    )  # 100만 mm
    res = go(db, world)
    assert res.n_failed == 1 and db.st[FARM]["error"] == "계약 검사 실패" and not db.daily and not db.summary


# ── (가) ∪ (나) 고르기, 읽기는 한꺼번에, 정기 실행 ───────────────────────────────────────
def three_farms(world, **kw):
    farms = [farm(FARM), farm(FARM2), farm(FARM3)]
    seasons = [fseason(f["farm_id"], sid="s" + f["farm_id"][:2]) for f in farms]
    return make_db(world, farms, seasons, **kw)


def test_auto_mode_takes_pending_and_stale_farms_and_a_second_run_has_nothing_to_do(world):
    finished = world["runs"][0]["finished_at"]
    status = [
        dict(farm_id=FARM, requested_at="2026-06-10T01:00:00+00:00", done_at=None),  # 대기 중
        dict(farm_id=FARM2, requested_at=None, done_at="2026-06-09T00:00:00+00:00"),  # 오래됨(회차보다 앞)
        dict(
            farm_id=FARM3, requested_at=None, done_at="2026-06-10T09:00:00+00:00"
        ),  # 회차 뒤에 계산함 → 최신
    ]
    db = three_farms(world, status=status)
    assert pd.Timestamp(finished) < pd.Timestamp("2026-06-10T09:00:00+00:00")
    res = go(db, world)
    assert (res.n_pending, res.n_stale, res.n_ok) == (1, 1, 2)
    assert db.st[FARM3]["done_at"] == "2026-06-10T09:00:00+00:00"  # 건드리지 않음
    res2 = go(db, world)
    assert (
        res2.selected == 0
    )  # 모두 최신: 두 번째 실행(예: collect 두 번째 · 세 번째 종료)은 거의 바로 끝난다


def test_pending_mode_ignores_stale_farms_and_all_mode_takes_every_non_common_farm(world):
    status = [
        dict(farm_id=f, requested_at=None, done_at="2026-06-09T00:00:00+00:00") for f in (FARM, FARM2, FARM3)
    ]
    assert go(three_farms(world, status=copy.deepcopy(status)), world, mode="pending").selected == 0
    fresh = [dict(s, done_at="2026-06-10T09:00:00+00:00") for s in status]
    assert go(three_farms(world, status=fresh), world, mode="all").n_ok == 3
    assert go(three_farms(world, status=copy.deepcopy(fresh)), world, mode="auto").selected == 0


def test_stale_reference_falls_back_to_the_latest_weather_fetch_when_a_station_has_no_runs(world):
    db = three_farms(
        world,
        status=[
            dict(farm_id=f, requested_at=None, done_at="2026-06-10T00:00:00+00:00")
            for f in (FARM, FARM2, FARM3)
        ],
        fetched={"101": "2026-06-10T02:00:00+00:00"},
    )
    db.rn = []  # 회차 기록이 없는 지점
    res = go(db, world)
    assert res.n_stale == 3 and db.calls["latest_fetched"] == 1  # 지점마다 한 번
    db2 = three_farms(
        world,
        status=[
            dict(farm_id=f, requested_at=None, done_at="2026-06-10T03:00:00+00:00")
            for f in (FARM, FARM2, FARM3)
        ],
        fetched={"101": "2026-06-10T02:00:00+00:00"},
    )
    db2.rn = []
    assert go(db2, world).selected == 0


def test_reads_are_per_table_and_per_station_not_per_farm(world):
    db = three_farms(world)
    go(db, world)
    assert db.calls["farms"] == db.calls["seasons"] == db.calls["logs"] == db.calls["status"] == 1
    assert (
        db.calls["weather"] == 1
        and db.calls["runs"] == 1
        and db.calls["outlook_src"] == 1
        and db.calls["existing_daily"] == 1
    )


def test_sync_runs_only_when_asked_and_only_when_writing(world):
    assert go(make_db(world, [farm()], [fseason()]), world).synced is None
    db = make_db(world, [farm()], [fseason()])
    assert go(db, world, sync=True).synced == 1 and db.synced == 1
    db2 = make_db(world, [farm()], [fseason()])
    go(db2, world, sync=True, write=False)
    assert db2.synced == 0


def test_dry_run_writes_nothing_but_counts(world):
    db = make_db(world, [farm()], [fseason()])
    res = go(db, world, write=False)
    assert res.dry_run and res.n_ok == 1 and res.daily_rows > 0
    assert not db.daily and not db.outlook and not db.summary and "done_at" not in db.st[FARM]
    assert "시험 실행(쓰지 않음)" in F.brief_text(res)


def test_station_without_public_outlook_rows_warns_but_does_not_fail_and_clears_old_outlook(world):
    db = make_db(world, [farm()], [fseason()])
    db.ol = []
    db.outlook[(FARM, "2026-06-09")] = dict(farm_id=FARM, run_id=1, target_date="2026-06-09")
    res = go(db, world)
    assert res.n_ok == 1 and res.n_outlook_warn == 1 and not res.failed and db.farm_daily(FARM)
    assert not db.outlook and FARM not in db.summary


def test_outlook_keeps_only_the_latest_run_rows(world):
    db = make_db(world, [farm()], [fseason()])
    db.outlook[(FARM, "2026-06-09")] = dict(farm_id=FARM, run_id=7, target_date="2026-06-09")  # 옛 회차 줄
    go(db, world)
    assert (FARM, "2026-06-09") not in db.outlook and len(db.outlook) == 4


# ── ⑬ 로그 · 요약 · 결과 파일에 농장 정보가 없다 ─────────────────────────────────────────
def test_summary_files_and_errors_contain_no_farm_information(world, tmp_path, capsys):
    f = farm()
    f["irrigated_area_m2"] = SECRET_AREA
    db = make_db(
        world,
        [f, farm(FARM2, season="no-such-season")],
        [fseason(), fseason(FARM2, sid="s2", season="no-such-season")],
        [dict(log_id=1, farm_id=FARM, date="2026-06-02", volume_l=SECRET_L)],
    )
    res = go(db, world)
    out = F.write_outputs(res, tmp_path)
    texts = [
        (out / "brief.md").read_text(encoding="utf-8"),
        (out / "summary.md").read_text(encoding="utf-8"),
        F.brief_text(res),
        json.dumps(db.st, ensure_ascii=False),
    ]
    blob = "\n".join(texts) + capsys.readouterr().out
    for secret in (FARM, FARM2, str(SECRET_AREA), str(int(SECRET_L)), "101", "춘천"):
        assert secret not in "\n".join(t for t in texts if t is not texts[3]) + capsys.readouterr().out, (
            secret
        )
    for secret in (FARM, FARM2, str(SECRET_AREA), str(int(SECRET_L))):
        assert secret not in db.st[FARM2]["error"]
    assert sorted(p.name for p in out.iterdir()) == [
        "brief.md",
        "summary.md",
    ]  # 줄 내용을 파일로 남기지 않는다
    assert "대상 2곳" in blob and "실패 1" in blob


# ── FarmDb: 삭제 조건에는 늘 farm_id 가 들어간다 ─────────────────────────────────────────
class Spy:
    def __init__(self, body=b"[]"):
        self.calls, self.body = [], body

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        return 200, self.body


def test_farm_db_deletes_always_carry_the_farm_id_condition():
    spy = Spy()
    db = FarmDb("https://x.example", "k", post=spy)
    db.delete_daily(FARM, ["2026-06-01", "2026-06-02"])
    db.delete_outlook(FARM)
    db.delete_outlook(FARM, keep_run_id=105)
    db.delete_summary(FARM)
    assert spy.calls and all(m == "DELETE" and f"farm_id=eq.{FARM}" in u for m, u in spy.calls)
    assert any("run_id=neq.105" in u for _, u in spy.calls) and any(
        "date=in.(2026-06-01,2026-06-02)" in u for _, u in spy.calls
    )
    for bad in ("", None, "not-a-uuid", "farm_id=gt.0"):
        with pytest.raises(ValueError):
            db.delete_daily(bad, ["2026-06-01"])
        with pytest.raises(ValueError):
            db.delete_outlook(bad)
        with pytest.raises(ValueError):
            db.delete_summary(bad)
    assert len([c for c in spy.calls]) == 4  # 잘못된 번호로는 요청을 보내지 않았다


def test_farm_db_status_write_only_touches_the_four_columns():
    sent = []

    def post(method, url, headers, body):
        sent.append((method, url, json.loads(body)))
        return 201, b""

    FarmDb("https://x.example", "k", post=post).put_status(FARM, "2026-06-10T01:00:00+00:00", "ok", None, "v")
    ((method, url, rows),) = sent
    assert method == "POST" and "farm_calc_status" in url and "on_conflict=farm_id" in url
    assert set(rows[0]) == {
        "farm_id",
        "done_at",
        "result",
        "error",
        "calc_version",
    }  # requested_at · recalc_from 은 안 건드린다


def test_farm_db_sync_all_calls_the_rpc_and_returns_the_count():
    def post(method, url, headers, body):
        assert method == "POST" and url.endswith("/rest/v1/rpc/sync_all_farm_seasons")
        return 200, b"7"

    assert FarmDb("https://x.example", "k", post=post).sync_all() == 7
    with pytest.raises(RuntimeError):
        FarmDb("https://x.example", "k", post=lambda *a: (404, b"")).sync_all()


# ── main (명령줄) ─────────────────────────────────────────────────────────────
REAL_RUN = F.run


def _run_main(monkeypatch, tmp_path, world, db, *args):
    for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(F, "FarmDb", lambda base, key: db)
    monkeypatch.setattr(F, "load_dictionaries", lambda base, key, http=None: world["d"])
    monkeypatch.setattr(F, "run", lambda *a, **k: REAL_RUN(*a, now=NOW, **k))
    return F.main(["--out", str(tmp_path / "out"), *args])


def test_main_exit_code_is_0_when_all_farms_succeed_and_1_when_one_fails(
    monkeypatch, tmp_path, world, capsys
):
    ok = make_db(world, [farm()], [fseason()])
    assert _run_main(monkeypatch, tmp_path, world, ok, "--sync") == 0
    assert ok.synced == 1 and (tmp_path / "out" / "farm_calc" / "brief.md").is_file()
    bad = make_db(world, [farm(season="no-such-season")], [fseason(season="no-such-season")])
    assert _run_main(monkeypatch, tmp_path, world, bad) == 1  # 실패한 농장이 있으면 빨갛게
    assert bad.synced == 0 and "실패 1" in capsys.readouterr().out


def test_main_dry_run_writes_nothing(monkeypatch, tmp_path, world):
    db = make_db(world, [farm()], [fseason()])
    assert _run_main(monkeypatch, tmp_path, world, db, "--dry-run", "--mode", "all", "--sync") == 0
    assert not db.daily and db.synced == 0


def test_status_write_failure_is_counted_and_makes_the_run_fail(world):
    db = make_db(world, [farm()], [fseason()])

    def boom(*a, **k):
        raise RuntimeError("down")

    db.put_status = boom
    res = go(db, world)
    assert res.n_status_fail == 1 and res.failed and "계산 상태를 못 씀 1곳" in F.brief_text(res)


# ── FarmDb 읽기 주소: 값의 '+' 가 빈칸으로 읽히지 않게 인코딩한다 (이슈 #133) ─────────────────
class UrlSpy:
    """읽기 주소를 기록하고 빈 목록을 돌려주는 가짜 http (쪽 나눔이 한 번에 끝난다)"""

    def __init__(self):
        self.urls: list[str] = []

    def __call__(self, url, headers):
        self.urls.append(url)
        return 200, b"[]"


@pytest.mark.parametrize(
    "since",
    ["2026-11-18T17:55:13.489175+00:00", "2026-11-18T17:55:13Z", "2026-11-19T02:55:13+09:00"],
)
def test_farm_db_runs_encodes_the_timestamp_in_the_query(since):
    spy = UrlSpy()
    FarmDb("https://x.example", "k", http=spy).runs(since)
    assert len(spy.urls) == 1
    query = spy.urls[0].split("?", 1)[1]
    assert "run_at=gte." in query
    # 보낸 글자에 날것의 '+' 가 없고(주소 질의에서 '+' 는 빈칸으로 읽힌다), 되읽으면 원래 시각이다
    assert "+" not in query
    if "+" in since:
        assert "%2B" in query
    got = urllib.parse.parse_qs(query)["run_at"]
    assert got == ["gte." + since]
    assert pd.Timestamp(got[0].removeprefix("gte.")) == pd.Timestamp(since)


def test_farm_db_station_ids_are_encoded_but_numeric_ones_are_unchanged():
    spy = UrlSpy()
    db = FarmDb("https://x.example", "k", http=spy)
    db.weather("108", "2026-06-01")
    db.latest_fetched("108")
    assert all("stn_id=eq.108" in u for u in spy.urls) and len(spy.urls) == 2
    spy.urls.clear()
    db.weather("a b&c+d", "2026-06-01")  # 숫자가 아닌 값이 와도 다른 조건으로 새지 않는다
    q = urllib.parse.parse_qs(spy.urls[0].split("?", 1)[1])
    assert q["stn_id"] == ["eq.a b&c+d"] and q["date"] == ["gte.2026-06-01"]
