"""수집 감시 (core/monitor.py, io/github_issue.py, collectors/monitor.py). 가짜 DB·가짜 GitHub로 돈다."""

import datetime as dt
import json
import urllib.parse

import pandas as pd
import pytest

from croplib_dp.collectors import monitor as M
from croplib_dp.core.monitor import STALE_H, check_health, kst, report
from croplib_dp.io.github_issue import LABEL, TITLE, IssueAlerts

TS = pd.Timestamp
NAMES = {"101": "춘천", "216": "태백"}


def _run(run_at, result="정시 성공", finished=None, on_time=True, error=None):
    t = TS(run_at)
    return dict(
        run_at=t.tz_localize("Asia/Seoul").tz_convert("UTC").isoformat(),  # DB는 UTC로 돌려준다
        run_name="아침" if t.hour == 2 else "저녁",
        stn_id="101",
        started_at=None,
        finished_at=(finished or t + pd.Timedelta(minutes=25)).tz_localize("Asia/Seoul").isoformat(),
        result=result,
        on_time=on_time,
        error=error,
    )


OK_WEATHER = {"101": dt.date(2026, 10, 25), "216": dt.date(2026, 10, 25)}


# ── 규칙 ────────────────────────────────────────────────────────────────
def test_kst_converts_db_time():
    assert kst("2026-10-24T17:00:00+00:00") == TS("2026-10-25 02:00")
    assert kst("2026-10-25 02:00") == TS("2026-10-25 02:00")


def test_all_good():
    now = TS("2026-10-26 08:00")
    runs = [_run("2026-10-25 17:00"), _run("2026-10-26 02:00")]
    assert check_health(now, runs, OK_WEATHER, NAMES) == []
    assert "이상 없음" in report([], now)


def test_stale_after_17_hours():
    runs = [_run("2026-10-25 17:00")]  # 17:25에 끝남
    assert check_health(TS("2026-10-26 10:00"), runs, OK_WEATHER, NAMES) == []  # 16.6시간
    a = check_health(TS("2026-10-26 10:30"), runs, OK_WEATHER, NAMES)  # 17.1시간
    assert [(x.level, x.code) for x in a] == [("error", "stale")] and "101 춘천 갱신 멈춤" in a[0].text
    assert STALE_H == 17


def test_stale_boundary_is_17_hours():
    done = TS("2026-10-26 02:25")  # _run("2026-10-26 02:00") 이 끝나는 시각
    runs = [_run("2026-10-26 02:00")]
    ok = check_health(done + pd.Timedelta(hours=16.9), runs, {}, NAMES)
    assert ok == []
    a = check_health(done + pd.Timedelta(hours=17.1), runs, {}, NAMES)
    assert [(x.level, x.code) for x in a] == [("error", "stale")]


def test_no_stale_alert_in_the_afternoon_gap_between_slots():
    """#35 재현: 02시 회차가 02:29에 끝나면 17시 회차가 끝나기 전 15:40(13.2시간)·17:00(14.5시간)은 정상이다.
    13시간 기준일 때는 이 시각에 가짜 '갱신 멈춤' 오류가 났다."""
    runs = [_run("2026-10-29 02:00", finished=TS("2026-10-29 02:29"))]
    for now in ("2026-10-29 15:40", "2026-10-29 17:00"):
        assert check_health(TS(now), runs, {}, NAMES) == [], now


def test_missing_slot_is_still_caught():
    """17시 회차가 빠지면 02:25 + 17시간 = 19:25 쯤 오류, 02시 회차가 빠지면 17:25 + 17시간 = 다음 날 10:25 쯤 오류."""
    morning = [_run("2026-10-29 02:00")]
    assert check_health(TS("2026-10-29 19:00"), morning, {}, NAMES) == []
    assert [x.code for x in check_health(TS("2026-10-29 19:40"), morning, {}, NAMES)] == ["stale"]
    evening = [_run("2026-10-29 17:00")]
    assert check_health(TS("2026-10-30 10:00"), evening, {}, NAMES) == []
    assert [x.code for x in check_health(TS("2026-10-30 10:40"), evening, {}, NAMES)] == ["stale"]


def test_failed_and_backup():
    now = TS("2026-10-26 03:00")
    a = check_health(now, [_run("2026-10-26 02:00", "실패", error="[30] SERVICE_KEY")], OK_WEATHER, NAMES)
    assert [(x.level, x.code) for x in a] == [("error", "failed")] and "[30] SERVICE_KEY" in a[0].text
    a = check_health(now, [_run("2026-10-26 02:00", "백업: 직전 발표", on_time=False)], OK_WEATHER, NAMES)
    assert [(x.level, x.code) for x in a] == [("warn", "late")] and "정시 아니오" in a[0].text
    a = check_health(now, [_run("2026-10-25 17:00", "실패"), _run("2026-10-26 02:00")], OK_WEATHER, NAMES)
    assert a == []  # 마지막 회차만 본다 (회복)


def test_no_runs_at_all():
    a = check_health(TS("2026-10-26 08:00"), [], OK_WEATHER, NAMES)
    assert [(x.level, x.code) for x in a] == [("error", "stale")]


def test_asos_checked_in_evening_only():
    runs = [_run("2026-10-26 17:00")]
    weather = {"101": dt.date(2026, 10, 25), "216": dt.date(2026, 10, 24)}
    assert check_health(TS("2026-10-26 17:40"), runs, weather, NAMES) == []
    a = check_health(TS("2026-10-26 18:10"), runs, weather, NAMES)
    assert [(x.level, x.code) for x in a] == [("warn", "asos")] and "216 태백" in a[0].text
    assert "101" not in a[0].text.split("—")[1]


def test_alert_keys_stable_for_same_problem():
    runs = [_run("2026-10-25 17:00")]
    k1 = [a.key for a in check_health(TS("2026-10-26 11:00"), runs, OK_WEATHER, NAMES)]
    k2 = [a.key for a in check_health(TS("2026-10-26 13:00"), runs, OK_WEATHER, NAMES)]
    assert k1 and k1 == k2  # 비어 있지 않아야 의미가 있다  # 시간이 지나도 같은 경고 → 댓글을 다시 달지 않음


# ── GitHub 이슈 ─────────────────────────────────────────────────────────
class FakeGitHub:
    def __init__(self, has_label=True):
        self.issues: list[dict] = []
        self.comments: list[tuple[int, str]] = []
        self.has_label = has_label
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == "Bearer tok"
        path = urllib.parse.urlparse(url).path.removeprefix("/repos/o/r")
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.calls.append((method, path))
        data = json.loads(body) if body else None
        if path.startswith("/labels/") and method == "GET":
            return (200, b"{}") if self.has_label else (404, b'{"message":"Not Found"}')
        if path == "/labels":
            self.has_label = True
            return 201, b"{}"
        if path == "/issues" and method == "GET":
            assert q["labels"] == LABEL and q["state"] == "open"
            return 200, json.dumps([i for i in self.issues if i["state"] == "open"]).encode()
        if path == "/issues" and method == "POST":
            i = dict(number=len(self.issues) + 1, state="open", **data)
            self.issues.append(i)
            return 201, json.dumps(i).encode()
        n = int(path.split("/")[2])
        issue = self.issues[n - 1]
        if path.endswith("/comments"):
            self.comments.append((n, data["body"]))
            return 201, b"{}"
        issue.update(data)
        return 200, json.dumps(issue).encode()


def test_issue_lifecycle():
    gh = FakeGitHub(has_label=False)
    s = IssueAlerts("o/r", "tok", http=gh)
    assert s.sync([], "이상 없음") == "none"
    assert s.sync(["error:stale:101"], "멈춤") == "opened"
    assert gh.issues[0]["title"] == TITLE and gh.issues[0]["labels"] == [LABEL] and gh.has_label
    assert s.sync(["error:stale:101"], "멈춤 (2시간 뒤)") == "unchanged" and gh.comments == []
    assert "2시간 뒤" in gh.issues[0]["body"]  # 본문은 최신으로
    assert s.sync(["error:stale:101", "warn:asos:x"], "멈춤 + 관측") == "commented"
    assert gh.comments == [(1, "멈춤 + 관측")]
    assert s.sync([], "이상 없음") == "closed" and gh.issues[0]["state"] == "closed"
    assert gh.comments[-1][1].startswith("✅ 정상 회복")
    assert s.sync(["error:failed:x"], "실패") == "opened" and len(gh.issues) == 2  # 다시 생기면 새 이슈


def test_github_error_raises():
    def bad(method, url, headers, body):
        return 403, b'{"message":"Resource not accessible by integration"}'

    with pytest.raises(RuntimeError, match="HTTP 403"):
        IssueAlerts("o/r", "tok", http=bad).find_open()


# ── 감시 한 번 ──────────────────────────────────────────────────────────
def test_read_state_and_run(monkeypatch):
    runs = [_run("2026-10-26 02:00", "지연 수집", on_time=False)]
    seen = []

    def db(url, headers):
        u = urllib.parse.urlparse(url)
        table, q = u.path.split("/rest/v1/")[1], dict(urllib.parse.parse_qsl(u.query))
        seen.append((table, q))
        if table == "forecast_runs":
            assert q["collector"] == "eq.actions" and q["order"] == "run_at.desc"
            return 200, json.dumps(runs).encode()
        if table == "stations":
            return 200, json.dumps(
                [{"stn_id": "101", "name_ko": "춘천"}, {"stn_id": "216", "name_ko": "태백"}]
            ).encode()
        if table == "heat_daily":  # 적산온도는 한 번에: 어제 값이 있는 지점 목록
            return 200, json.dumps([{"stn_id": "101"}]).encode()
        rows = [{"date": "2026-10-25"}] if q["stn_id"] == "eq.101" else []
        return 200, json.dumps(rows).encode()

    state = M.read_state("https://x.supabase.co", "srv", db, now=TS("2026-10-26 18:30"))
    assert state["weather_last"] == {"101": dt.date(2026, 10, 25), "216": None}
    gh = FakeGitHub()
    text, action = M.run_monitor(state, IssueAlerts("o/r", "tok", http=gh), now=TS("2026-10-26 18:30"))
    assert action == "opened" and "지연 수집" in text and "216 태백" in text
    assert "SUPABASE" not in text and "srv" not in gh.issues[0]["body"]


def test_heat_checked_after_noon():
    runs = [_run("2026-10-26 02:00")]
    heat = {"101": dt.date(2026, 10, 25), "216": dt.date(2026, 10, 23)}
    assert check_health(TS("2026-10-26 11:30"), runs, OK_WEATHER, NAMES, heat_last=heat) == []
    a = check_health(TS("2026-10-26 12:30"), runs, OK_WEATHER, NAMES, heat_last=heat)
    assert [(x.level, x.code) for x in a] == [("warn", "heat")] and "216 태백" in a[0].text
    assert "101" not in a[0].text.split("—")[1]
    assert check_health(TS("2026-10-26 12:30"), runs, OK_WEATHER, NAMES) == []  # 넘기지 않으면 보지 않음


# ── 알림 연습 (--drill) ─────────────────────────────────────────────────
class FakeGitHubByLabel(FakeGitHub):
    """연습은 진짜 경고와 다른 라벨을 쓴다. 라벨이 맞는 이슈만 찾는다."""

    def __init__(self, label):
        super().__init__()
        self.label = label

    def __call__(self, method, url, headers, body):
        if urllib.parse.urlparse(url).path.endswith("/issues") and method == "GET":
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            assert q["labels"] == self.label
            found = [i for i in self.issues if i["state"] == "open" and self.label in i["labels"]]
            return 200, json.dumps(found).encode()
        return super().__call__(method, url, headers, body)


def test_drill_start_opens_a_practice_issue_with_its_own_title_and_label():
    gh = FakeGitHubByLabel(M.DRILL_LABEL)
    sink = M.drill_sink("o/r", "tok", http=gh)
    text, action = M.run_drill("start", sink, now=TS("2026-10-26 10:00"))
    assert action == "opened"
    issue = gh.issues[0]
    assert issue["title"] == M.DRILL_TITLE and issue["labels"] == [M.DRILL_LABEL]
    assert M.DRILL_LABEL != LABEL and M.DRILL_TITLE != TITLE
    assert "실제 문제가 아닙니다" in text  # 연습이라는 말이 본문 맨 위에 있다
    assert text.splitlines()[0].startswith("🧪")
    assert "실패" in text  # 진짜 감시 규칙(check_health)이 실패 회차를 잡아 만든 글


def test_drill_end_closes_with_recovery_comment_and_start_again_after_close_opens_new():
    gh = FakeGitHubByLabel(M.DRILL_LABEL)
    sink = M.drill_sink("o/r", "tok", http=gh)
    assert M.run_drill("end", sink, now=TS("2026-10-26 10:00"))[1] == "none"  # 열린 연습이 없으면 할 일 없음
    M.run_drill("start", sink, now=TS("2026-10-26 10:00"))
    assert M.run_drill("start", sink, now=TS("2026-10-26 10:05"))[1] == "unchanged"  # 메일이 또 가지 않는다
    _, action = M.run_drill("end", sink, now=TS("2026-10-26 10:10"))
    assert action == "closed" and gh.issues[0]["state"] == "closed"
    assert gh.comments[-1][1].startswith("✅ 정상 회복")
    assert M.run_drill("start", sink, now=TS("2026-10-26 10:20"))[1] == "opened" and len(gh.issues) == 2


def test_drill_never_reads_the_db_or_touches_the_real_alert_issue(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("연습은 DB를 읽지 않는다")

    monkeypatch.setattr(M, "read_state", boom)
    gh = FakeGitHubByLabel(M.DRILL_LABEL)
    # 진짜 경고 이슈가 열려 있어도 연습은 건드리지 않는다 (라벨이 달라 찾지 못한다)
    gh.issues.append({"number": 1, "state": "open", "title": TITLE, "labels": [LABEL], "body": "진짜"})
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    original = M.drill_sink
    monkeypatch.setattr(M, "drill_sink", lambda repo, token: original(repo, token, http=gh))
    assert M.main(["--drill", "start"]) == 0
    assert len(gh.issues) == 2 and gh.issues[1]["title"] == M.DRILL_TITLE
    assert gh.issues[0]["body"] == "진짜" and gh.comments == []


def test_main_rejects_unknown_drill_mode():
    with pytest.raises(SystemExit):
        M.main(["--drill", "oops"])


def test_default_alert_sink_is_unchanged():
    gh = FakeGitHub()
    IssueAlerts("o/r", "tok", http=gh).sync(["error:failed:x"], "실패")
    assert gh.issues[0]["title"] == TITLE and gh.issues[0]["labels"] == [LABEL]


# ── 관측은 수집하는 지점만, 적산온도는 운영 지점 전부 (#33, 결정 0017) ──────
NOW = TS("2026-10-26 18:30")
YESTERDAY = "2026-10-25"
STATIONS_105 = [
    {"stn_id": "101", "name_ko": "춘천", "has_forecast": True, "is_active": True},
    {"stn_id": "216", "name_ko": "태백", "has_forecast": True, "is_active": True},
    {
        "stn_id": "108",
        "name_ko": "서울",
        "has_forecast": False,
        "is_active": True,
    },  # 운영 중, 물은 수집 안 함
    {"stn_id": "999", "name_ko": "폐지지점", "has_forecast": False, "is_active": False},  # 폐지·휴지
]


class FakeDb:
    """가짜 Supabase REST. 요청 주소를 그대로 기록하고, stations 는 has_forecast·is_active 조건대로 걸러 준다
    (조건이 없으면 전부 돌려준다 — 조건을 빼면 시험이 깨지게).
    heat_daily 는 지점마다 묻지 않고 한 번에 묻는다: 어제 날짜·작물 공통 값이 있는 지점 목록을 돌려준다."""

    def __init__(self, obs: dict[str, str], heat: set[str] | None = None, stations=None):
        self.obs = obs  # 지점 → 관측 마지막 날 (없으면 빈 응답)
        self.heat = heat if heat is not None else set()  # 어제 적산온도(common)가 있는 지점
        self.stations = STATIONS_105 if stations is None else stations
        self.urls: list[str] = []

    def __call__(self, url, headers):
        self.urls.append(url)
        u = urllib.parse.urlparse(url)
        table, q = u.path.split("/rest/v1/")[1], dict(urllib.parse.parse_qsl(u.query))
        if table == "forecast_runs":
            return 200, json.dumps([_run("2026-10-26 17:00")]).encode()
        if table == "stations":
            rows = self.stations
            if q.get("has_forecast") == "eq.true":
                rows = [s for s in rows if s["has_forecast"] is True]
            if q.get("is_active") == "eq.true":
                rows = [s for s in rows if s["is_active"] is True]
            return 200, json.dumps([{"stn_id": s["stn_id"], "name_ko": s["name_ko"]} for s in rows]).encode()
        if table == "heat_daily":
            assert "stn_id" not in q or q["stn_id"] == ""  # 지점마다 묻지 않는다
            assert q["date"] == f"eq.{YESTERDAY}" and q["crop_id"] == "eq.common"
            assert q["gdd_cum"] == "not.is.null"
            return 200, json.dumps([{"stn_id": s} for s in sorted(self.heat)]).encode()
        stn = q["stn_id"].removeprefix("eq.")
        rows = [{"date": self.obs[stn]}] if stn in self.obs else []
        return 200, json.dumps(rows).encode()


def _state(db):
    return M.read_state("https://x.supabase.co", "srv", db, now=NOW)


def test_read_state_asks_collected_for_obs_and_active_for_heat_in_few_calls():
    db = FakeDb({"101": YESTERDAY, "216": YESTERDAY}, heat={"101", "216", "108"})
    _state(db)
    st_urls = [u for u in db.urls if "/rest/v1/stations" in u]
    assert len(st_urls) == 2
    assert sum("has_forecast=eq.true" in u for u in st_urls) == 1  # 요청 주소에 조건이 들어 있다
    assert sum("is_active=eq.true" in u for u in st_urls) == 1
    obs = [u for u in db.urls if "/rest/v1/weather_daily" in u]
    assert len(obs) == 2 and not any("stn_id=eq.108" in u or "stn_id=eq.999" in u for u in obs)
    heat = [u for u in db.urls if "/rest/v1/heat_daily" in u]
    assert len(heat) == 1  # 적산온도는 지점 수와 상관없이 REST 한 번
    assert "stn_id=eq" not in heat[0] and f"date=eq.{YESTERDAY}" in heat[0] and "crop_id=eq.common" in heat[0]
    assert len(db.urls) == 1 + 2 + 2 + 1  # 회차, 지점 2번, 관측 2곳, 적산온도 1번


def test_state_targets_obs_collected_and_heat_active():
    db = FakeDb({"101": YESTERDAY, "216": YESTERDAY}, heat={"101", "216", "108"})
    state = _state(db)
    assert set(state["weather_last"]) == {"101", "216"}  # 관측: has_forecast 만
    assert set(state["heat_last"]) == {"101", "216", "108"}  # 적산온도: 운영 지점 (폐지 999 는 없음)
    assert state["names"]["108"] == "서울"
    text, action = M.run_monitor(state, None, now=NOW)
    assert "이상 없음" in text and "999" not in text and action == "dry"


def test_uncollected_station_without_obs_gives_no_alert():
    """108 서울은 물을 수집하지 않아 관측이 없다 — 관측 경고는 없고(has_forecast 만 봄) 적산온도만 본다."""
    db = FakeDb({"101": YESTERDAY, "216": YESTERDAY}, heat={"101", "216", "108"})
    text, _ = M.run_monitor(_state(db), None, now=NOW)
    assert "108" not in text and "이상 없음" in text


def test_collected_station_without_obs_still_alerts():
    """감시 의미를 줄이지 않는다: 수집하는 지점(216)에 어제 관측이 없으면 여전히 경고."""
    db = FakeDb({"101": YESTERDAY}, heat={"101", "216", "108"})
    state = _state(db)
    alerts = check_health(NOW, state["runs"], state["weather_last"], state["names"], state["heat_last"])
    assert [(a.level, a.code) for a in alerts] == [("warn", "asos")]
    assert "216 태백" in alerts[0].text and "101" not in alerts[0].text.split("—")[1]


def test_only_active_stations_missing_yesterday_heat_are_warned():
    """운영 지점 중 어제 적산온도가 빠진 곳만 경고. 폐지 지점(999)은 어제 값이 없어도 경고하지 않는다."""
    db = FakeDb({"101": YESTERDAY, "216": YESTERDAY}, heat={"101"})
    state = _state(db)
    alerts = check_health(NOW, state["runs"], state["weather_last"], state["names"], state["heat_last"])
    assert [(a.level, a.code) for a in alerts] == [("warn", "heat")]
    t = alerts[0].text
    assert "216 태백" in t and "108 서울" in t and "101" not in t.split("—")[1] and "999" not in t


def test_zero_collect_stations_is_an_error_not_silence():
    db = FakeDb({}, heat={"101"}, stations=[dict(s, has_forecast=False) for s in STATIONS_105])
    state = _state(db)
    assert state["weather_last"] == {}
    text, _ = M.run_monitor(state, None, now=NOW)
    assert "🔴 오류" in text and "수집 지점이 0곳 — 지점 사전 has_forecast 확인" in text
    a = check_health(NOW, state["runs"], {}, state["names"], state["heat_last"], collect_count=0)
    zero = next(x for x in a if "수집 지점이 0곳" in x.text)
    assert (zero.level, zero.code) == ("error", "stale")


def test_zero_active_stations_is_an_error_not_silence():
    db = FakeDb(
        {"101": YESTERDAY},
        stations=[dict(s, is_active=False) for s in STATIONS_105],
    )
    text, _ = M.run_monitor(_state(db), None, now=NOW)
    assert "🔴 오류" in text and "적산온도 대상 지점이 0곳 — 지점 사전 is_active 확인" in text


def test_no_zero_station_error_when_not_asked():
    """연습(--drill)처럼 지점 수를 알리지 않으면 0곳 오류를 만들지 않는다."""
    a = check_health(NOW, [_run("2026-10-26 17:00")], {}, {"101": "춘천"})
    assert a == []


def test_many_missing_heat_stations_are_shortened():
    names = {f"{i}": f"지점{i}" for i in range(100, 197)}  # 운영 97곳
    heat = {k: None for k in names}  # 모두 빠짐
    a = check_health(TS("2026-10-26 12:30"), [_run("2026-10-26 02:00")], OK_WEATHER, names, heat_last=heat)
    t = a[0].text
    assert "97곳" in t and "앞 10곳" in t and "지점100" in t and "지점109" in t and "지점110" not in t
    assert len(t) < 300
    few = {k: None for k in list(names)[:10]}
    t10 = check_health(TS("2026-10-26 12:30"), [_run("2026-10-26 02:00")], OK_WEATHER, names, heat_last=few)[
        0
    ].text
    assert "앞 10곳" not in t10 and "지점109" in t10  # 10곳까지는 다 적는다


def test_shortened_alert_key_is_stable_when_missing_set_changes():
    names = {f"{i}": f"지점{i}" for i in range(100, 197)}
    a1 = check_health(
        TS("2026-10-26 12:30"), [_run("2026-10-26 02:00")], OK_WEATHER, names, {k: None for k in names}
    )
    a2 = check_health(
        TS("2026-10-26 13:30"),
        [_run("2026-10-26 02:00")],
        OK_WEATHER,
        names,
        {k: None for k in list(names)[:50]},
    )
    assert [a.key for a in a1] == [a.key for a in a2]  # 빠진 지점 수가 달라도 같은 경고
