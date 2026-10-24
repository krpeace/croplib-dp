"""수집 감시 (공3): DB의 회차 기록과 관측 마지막 날을 보고 경고를 GitHub 이슈로 알린다.

`python -m croplib_dp.collectors.monitor` — 환경변수 SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
GITHUB_TOKEN, GITHUB_REPOSITORY. 예약 수집이 끝날 때와 2시간마다(monitor.yml) 돈다.
"""

import datetime as dt
import os
import sys
from typing import Any

from croplib_dp.collectors.water import COLLECTOR, kst_now
from croplib_dp.core.monitor import check_health, report
from croplib_dp.io.dictionaries import RestGet, read_table, rest_get
from croplib_dp.io.github_issue import IssueAlerts

RUN_COLS = "run_at,run_name,stn_id,started_at,finished_at,result,on_time,error"


def read_state(base: str, key: str, http: RestGet = rest_get) -> dict[str, Any]:
    """최근 서버 회차 기록, 지점 이름, 지점별 관측 마지막 날"""
    runs = read_table(
        base,
        key,
        "forecast_runs",
        http,
        f"select={RUN_COLS}&collector=eq.{COLLECTOR}&order=run_at.desc&limit=20",
    )
    stations = read_table(base, key, "stations", http, "select=stn_id,name_ko&order=sort_order")
    last: dict[str, dt.date | None] = {}
    for st in stations:
        rows = read_table(
            base, key, "weather_daily", http, f"select=date&stn_id=eq.{st['stn_id']}&order=date.desc&limit=1"
        )
        last[str(st["stn_id"])] = dt.date.fromisoformat(rows[0]["date"][:10]) if rows else None
    return dict(runs=runs, names={str(s["stn_id"]): s["name_ko"] for s in stations}, weather_last=last)


def run_monitor(state: dict[str, Any], alerts_sink: IssueAlerts | None, now=None) -> tuple[str, str]:
    now = now if now is not None else kst_now()
    alerts = check_health(now, state["runs"], state["weather_last"], state["names"])
    text = report(alerts, now)
    action = alerts_sink.sync([a.key for a in alerts], text) if alerts_sink is not None else "dry"
    return text, action


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"환경변수 {name}가 없습니다")
    return v


def main(argv: list[str] | None = None) -> int:
    state = read_state(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))
    sink = IssueAlerts(_env("GITHUB_REPOSITORY"), _env("GITHUB_TOKEN"))
    text, action = run_monitor(state, sink)
    print(text)
    print(f"이슈: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
