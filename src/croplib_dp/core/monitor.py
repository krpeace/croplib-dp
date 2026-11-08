"""수집 감시 규칙 (공3): 회차 기록·관측 마지막 날로 경고를 정한다. DB·GitHub를 모르는 순수 함수.

error (오류) — 바로 봐야 함
  - stale   : 마지막 회차가 끝난 지 17시간 넘음 (회차 하나가 빠짐. 02시→17시 간격이 15시간이라
              13시간이면 매일 오후 가짜 오류, #37)
  - failed  : 마지막 회차 결과가 '실패'
  - stale   : 수집 지점(has_forecast)이 0곳 (지점 사전 문제 — 관측 경고가 조용히 사라지지 않게)
  - stale   : 적산온도 대상 지점(is_active)이 0곳 (같은 이유)
warn (경고) — 서비스는 되지만 기록해 둘 것
  - late    : 마지막 회차가 '지연 수집' 또는 '백업: 직전 발표'·'백업: S3'
  - asos    : 오후 회차(17시 발표 + 1시간) 뒤에도 어제 ASOS 관측이 DB에 없음 (수집 지점 has_forecast 만)
  - heat    : 낮 12시 뒤에도 적산온도(heat_daily)에 어제 값이 없음 (하루 한 번 09:20 수집, 온1).
              대상은 운영 지점(is_active) 전부 — 물 수집 지점보다 넓다 (결정 0017)
  - krei    : 매달 8일 23:59 KST 뒤(9일 00:00~)에도 원예 4부류(과일·과채·엽근채소·양념채소)의 이번 달 월보가
              fetched 가 아니거나, fetch_status = 'failed' 인 건이 있음 (결정 0019)
빠진 지점이 많으면(MAX_NAMED 곳 넘음) "N곳 — 앞 10곳 …" 으로 줄인다 (이슈·메일 가독성).
"""

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from croplib_dp.core.krei import HORT_MONTH_ITEMS

# 이보다 오래 갱신이 없으면 멈춤. 정상 최대 간격 약 16시간(02시 회차 → 17시 회차 마감 18:30) + 여유.
# CropMon src/domain/freshness.ts STALE_AFTER_MS 와 같은 값이어야 함
STALE_H = 17
ASOS_CHECK_AFTER_H = 18  # 이 시각(KST) 뒤에는 어제 관측이 있어야 함 (17:20 회차 + 여유)
BACKUP_RESULTS = ("지연 수집", "백업: 직전 발표", "백업: S3")
HEAT_CHECK_AFTER_H = 12  # 적산온도는 09:20 에 한 번 수집 (재시도·늦은 시작 여유)
KREI_CHECK_AFTER_DAY = 8  # 매달 8일 23:59 KST까지 여유, 9일(now.day > 8)부터 이번 달 원예 4부류 월보 확인
MAX_NAMED = 10  # 경고 문구에 이름을 다 적는 지점 수의 한도


@dataclass(frozen=True)
class Alert:
    level: str  # "error" | "warn"
    code: str  # stale | failed | late | asos | heat | krei
    text: str

    @property
    def key(self) -> str:
        """같은 경고인지 가리는 열쇠 (같으면 이슈에 다시 댓글을 달지 않음)"""
        return f"{self.level}:{self.code}:{self.text.split(' — ')[0]}"


def kst(t: Any) -> pd.Timestamp:
    """DB 시각(시간대 포함, 보통 UTC) → 한국 시각(시간대 없음)"""
    ts = pd.Timestamp(t)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Seoul").tz_localize(None)
    return ts


def named(missing: list[str], names: Mapping[str, str]) -> str:
    """빠진 지점을 문구로. 많으면 "N곳 — 앞 10곳 … …" 으로 줄인다."""
    ns = ", ".join(f"{s} {names.get(s, '')}".strip() for s in missing[:MAX_NAMED])
    if len(missing) <= MAX_NAMED:
        return ns
    return f"{len(missing)}곳 — 앞 {MAX_NAMED}곳 {ns} …"


def _check_krei(now: pd.Timestamp, krei_reports: Iterable[Mapping[str, Any]]) -> list[Alert]:
    rows = list(krei_reports)
    out: list[Alert] = []
    if now.day > KREI_CHECK_AFTER_DAY:
        fetched_hort = {
            str(r.get("ovr_item_code"))
            for r in rows
            if int(r.get("yyyy") or 0) == now.year
            and int(r.get("mm") or 0) == now.month
            and r.get("report_type") == "월보"
            and r.get("fetch_status") == "fetched"
        }
        missing_hort = [name for code, name in HORT_MONTH_ITEMS.items() if code not in fetched_hort]
        if missing_hort:
            out.append(
                Alert(
                    "warn",
                    "krei",
                    f"KREI {now.month}월 월보 미수집 — {', '.join(missing_hort)}",
                )
            )
    failed = sorted(
        (r for r in rows if r.get("fetch_status") == "failed"),
        key=lambda r: int(r.get("file_id") or 0),
    )
    for r in failed:
        item_name = (
            r.get("item_name")
            or HORT_MONTH_ITEMS.get(str(r.get("ovr_item_code")), "")
            or str(r.get("ovr_item_code") or "미상")
        )
        fid = r.get("file_id")
        raw_err = str(r.get("fetch_error") or "이유 없음").strip()
        first_line = (raw_err.splitlines() or ["이유 없음"])[0]
        out.append(Alert("warn", "krei", f"KREI 수집 실패 ({item_name} #{fid}) — {first_line}"))
    return out


def check_health(
    now: pd.Timestamp,
    runs: Iterable[Mapping[str, Any]],
    weather_last: Mapping[str, dt.date | None],
    station_names: Mapping[str, str] | None = None,
    heat_last: Mapping[str, dt.date | None] | None = None,
    collect_count: int | None = None,
    krei_reports: Iterable[Mapping[str, Any]] | None = None,
    krei_error: str | None = None,
) -> list[Alert]:
    """now: 한국 시각. runs: 서버 회차 기록(forecast_runs, 최근 것부터 아니어도 됨).
    weather_last: 수집 지점(has_forecast)별 weather_daily 마지막 날짜.
    heat_last: 적산온도 대상 지점(is_active)별 날짜 (어제 값이 있으면 그 날짜, 없으면 None).
    collect_count: 수집 지점 수. 0 이면 오류 (넘기지 않으면 보지 않는다 — 알림 연습 등).
    krei_reports: krei.report 행 목록 (이번 달 행 + failed 행). 넘기지 않으면 보지 않는다."""
    names = station_names or {}
    now = pd.Timestamp(now)
    alerts: list[Alert] = []
    runs = sorted(runs, key=lambda r: kst(r["run_at"]))
    by_stn: dict[str, dict] = {}
    for r in runs:
        by_stn[str(r["stn_id"])] = r
    if not by_stn:
        alerts.append(
            Alert("error", "stale", "회차 기록이 하나도 없습니다 — 예약 수집이 돌았는지 확인하세요")
        )
    if collect_count == 0:
        alerts.append(Alert("error", "stale", "수집 지점이 0곳 — 지점 사전 has_forecast 확인"))
    if heat_last is not None and not heat_last:
        alerts.append(Alert("error", "stale", "적산온도 대상 지점이 0곳 — 지점 사전 is_active 확인"))
    for stn, r in sorted(by_stn.items()):
        label = f"{stn} {names.get(stn, '')}".strip()
        done = kst(r.get("finished_at") or r.get("started_at") or r["run_at"])
        age_h = (now - done).total_seconds() / 3600
        run = kst(r["run_at"])
        head = f"{label} {run:%m/%d %H}시 회차"
        if age_h > STALE_H:
            alerts.append(
                Alert(
                    "error",
                    "stale",
                    f"{label} 갱신 멈춤 — 마지막 회차 {run:%m/%d %H}시, {age_h:.0f}시간 전에 끝남",
                )
            )
        if r["result"] == "실패":
            alerts.append(Alert("error", "failed", f"{head} 실패 — {r.get('error') or '이유 없음'}"))
        elif r["result"] in BACKUP_RESULTS:
            alerts.append(
                Alert("warn", "late", f"{head} {r['result']} — 정시 {'예' if r.get('on_time') else '아니오'}")
            )
    if now.hour >= ASOS_CHECK_AFTER_H:
        yesterday = (now - pd.Timedelta(days=1)).date()
        missing = sorted(s for s, d in weather_last.items() if d is None or d < yesterday)
        if missing:
            alerts.append(
                Alert("warn", "asos", f"어제({yesterday:%m/%d}) ASOS 관측 없음 — {named(missing, names)}")
            )
    if heat_last is not None and now.hour >= HEAT_CHECK_AFTER_H:
        yesterday = (now - pd.Timedelta(days=1)).date()
        missing = sorted(s for s, d in heat_last.items() if d is None or d < yesterday)
        if missing:
            alerts.append(
                Alert("warn", "heat", f"어제({yesterday:%m/%d}) 적산온도 없음 — {named(missing, names)}")
            )

    if krei_error:
        first_line = (str(krei_error).strip().splitlines() or ["이유 없음"])[0]
        alerts.append(Alert("warn", "krei", f"KREI 상태 조회 실패 — {first_line}"))
    elif krei_reports is not None:
        alerts.extend(_check_krei(now, krei_reports))
    return alerts


def report(alerts: list[Alert], now: pd.Timestamp) -> str:
    """이슈 본문·댓글용 글"""
    if not alerts:
        return f"{now:%Y-%m-%d %H:%M} 점검: 이상 없음"
    lines = [f"{now:%Y-%m-%d %H:%M} 점검 결과", ""]
    for a in sorted(alerts, key=lambda a: (a.level != "error", a.code, a.text)):
        lines.append(f"- {'🔴 오류' if a.level == 'error' else '🟡 경고'}: {a.text}")
    return "\n".join(lines)
