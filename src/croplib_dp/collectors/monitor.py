"""수집 감시 (공3): DB의 회차 기록과 관측 마지막 날을 보고 경고를 GitHub 이슈로 알린다.

`python -m croplib_dp.collectors.monitor` — 환경변수 SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
GITHUB_TOKEN, GITHUB_REPOSITORY. 예약 수집이 끝날 때와 2시간마다(monitor.yml) 돈다.
`--drill start|end` 는 알림이 메일까지 가는지 보는 연습이다 (DB를 읽지 않고, 진짜 경고 이슈는 건드리지 않음).
"""

import argparse
import datetime as dt
import os
import sys
from typing import Any

import pandas as pd

from croplib_dp.collectors.water import COLLECTOR, kst_now
from croplib_dp.core.monitor import check_health, report
from croplib_dp.io.dictionaries import RestGet, read_table, rest_get
from croplib_dp.io.github_issue import IssueAlerts, Request, http_request

DRILL_TITLE = "[수집 경고] 알림 연습 (실제 문제 아님)"
DRILL_LABEL = "수집 경고 연습"
RUN_COLS = "run_at,run_name,stn_id,started_at,finished_at,result,on_time,error"


def read_state(base: str, key: str, http: RestGet = rest_get) -> dict[str, Any]:
    """최근 서버 회차 기록, 지점 이름, 지점별 관측 마지막 날, 지점별 적산온도 올해 값 마지막 날"""
    runs = read_table(
        base,
        key,
        "forecast_runs",
        http,
        f"select={RUN_COLS}&collector=eq.{COLLECTOR}&order=run_at.desc&limit=20",
    )
    stations = read_table(base, key, "stations", http, "select=stn_id,name_ko&order=sort_order")
    last: dict[str, dt.date | None] = {}
    heat: dict[str, dt.date | None] = {}
    for st in stations:
        rows = read_table(
            base, key, "weather_daily", http, f"select=date&stn_id=eq.{st['stn_id']}&order=date.desc&limit=1"
        )
        last[str(st["stn_id"])] = dt.date.fromisoformat(rows[0]["date"][:10]) if rows else None
        rows = read_table(
            base,
            key,
            "heat_daily",
            http,
            f"select=date&stn_id=eq.{st['stn_id']}&gdd_cum=not.is.null&order=date.desc&limit=1",
        )
        heat[str(st["stn_id"])] = dt.date.fromisoformat(rows[0]["date"][:10]) if rows else None
    return dict(
        runs=runs,
        names={str(s["stn_id"]): s["name_ko"] for s in stations},
        weather_last=last,
        heat_last=heat,
    )


def run_monitor(state: dict[str, Any], alerts_sink: IssueAlerts | None, now=None) -> tuple[str, str]:
    now = now if now is not None else kst_now()
    alerts = check_health(now, state["runs"], state["weather_last"], state["names"], state.get("heat_last"))
    text = report(alerts, now)
    action = alerts_sink.sync([a.key for a in alerts], text) if alerts_sink is not None else "dry"
    return text, action


def drill_sink(repo: str, token: str, http: Request = http_request) -> IssueAlerts:
    """연습용 이슈 창구: 제목과 라벨이 진짜 경고와 달라서, 열려 있는 진짜 경고 이슈를 건드리지 않는다."""
    return IssueAlerts(
        repo, token, http=http, title=DRILL_TITLE, label=DRILL_LABEL, label_desc="알림 연습 (실제 문제 아님)"
    )


def run_drill(mode: str, sink: IssueAlerts, now=None) -> tuple[str, str]:
    """알림 연습. start: 가짜 실패 회차를 진짜 감시 규칙(check_health)에 넣어 이슈를 연다(메일이 감).
    end: 연습 이슈를 "정상 회복" 댓글과 함께 닫는다. DB는 읽지 않는다."""
    now = now if now is not None else kst_now()
    if mode == "start":
        run_at = (pd.Timestamp(now) - pd.Timedelta(hours=3)).isoformat()
        runs = [dict(run_at=run_at, stn_id="101", result="실패", on_time=False, error="연습용 가짜 실패")]
        alerts = check_health(now, runs, {}, {"101": "춘천"})
        text = "🧪 알림 연습입니다. 실제 문제가 아닙니다. 이 메일이 왔다면 알림이 잘 가는 것입니다.\n\n"
        text += report(alerts, now)
        return text, sink.sync([a.key for a in alerts], text)
    text = "🧪 알림 연습을 끝냅니다. 실제 문제가 아니었습니다.\n\n" + report([], now)
    return text, sink.sync([], text)


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"환경변수 {name}가 없습니다")
    return v


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="수집 감시")
    parser.add_argument("--drill", choices=["start", "end"], help="알림 연습 (DB를 읽지 않음)")
    args = parser.parse_args(argv)
    if args.drill:
        sink = drill_sink(_env("GITHUB_REPOSITORY"), _env("GITHUB_TOKEN"))
        text, action = run_drill(args.drill, sink)
        print(text)
        print(f"이슈: {action}")
        return 0
    state = read_state(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))
    sink = IssueAlerts(_env("GITHUB_REPOSITORY"), _env("GITHUB_TOKEN"))
    text, action = run_monitor(state, sink)
    print(text)
    print(f"이슈: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
