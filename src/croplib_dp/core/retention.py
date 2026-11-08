"""전망 표 월 보관·예보 원문 보관 기간 규칙 (이슈 #61, #28 S3-a, 결정 0022). 순수 함수만 둔다: DB·API·파일을 모른다.

규칙 (#28 계획서 D3·D4, 2026-11-08 승인)
  - water_outlook·water_outlook_summary: `forecast_runs.run_at`의 한국 날짜가 오늘 − 14일보다 앞인 줄은
    월 단위 csv.gz 로 Storage 에 보관한 뒤 지운다. **forecast_runs 는 지우지 않는다**(외래키 run_id 가 계속 유효해서
    보관 파일로 되살릴 수 있다).
  - 예보 원문: 대표 밖 지점(`fcst-ext/`)만 발표 시각이 90일 지난 것을 지운다. 대표 10곳(`fcst/`)은 지금처럼 보관한다.
보관 파일 경로·이름·열은 아래 상수와 함수가 정한다(결정 0022에도 같은 내용을 적는다).
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import csv
import datetime as dt
import gzip
import io
import json
import lzma
import re
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

OUTLOOK_KEEP_DAYS = 14  # 이 일수 안의 회차 줄은 DB 에 남긴다
RAW_EXT_KEEP_DAYS = 90  # 대표 밖 원문 보관 일수
RAW_REP_PREFIX = "fcst/"  # 대표 10곳 원문: 지우지 않는다
RAW_EXT_PREFIX = "fcst-ext/"  # 대표 밖 지점 원문: 90일 지나면 지운다 (쓰는 것은 S4)
ARCHIVE_ROOT = "archive"
ARCHIVE_PREFIX_COLS = (
    "run_at",
    "run_name",
    "stn_id",
    "collector",
)  # 파일만 봐도 어느 회차인지 알게 앞에 붙임

# 올리는 열(io/contract.py SPECS 와 같아야 한다: 시험이 같은지 확인한다. core 는 io 를 가져오지 못한다)
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "water_outlook": (
        "run_id", "scenario_id", "target_date", "lead_day", "eto_mm", "etc_mm", "etc_err_mm", "rain_fcst_mm",
        "pop_pct", "rain_expected_mm", "dr_center_mm", "dr_early_mm", "dr_late_mm", "status", "calc_version",
    ),
    "water_outlook_summary": (
        "run_id", "scenario_id", "horizon_days", "start_date", "dr_start_mm", "status_start", "need_center_in",
        "need_early_in", "need_late_in", "advice_net_mm", "advice_gross_mm", "etc_sum_mm", "etc_sum_err_mm",
        "rain_sum_mm", "rain_expected_sum_mm", "calc_version",
    ),
}  # fmt: skip
ROW_KEY: dict[str, tuple[str, ...]] = {
    "water_outlook": ("run_id", "scenario_id", "target_date"),
    "water_outlook_summary": ("run_id", "scenario_id"),
}
ARCHIVE_COLUMNS = {t: ARCHIVE_PREFIX_COLS + cols for t, cols in TABLE_COLUMNS.items()}
INT_COLS = {"run_id", "lead_day", "horizon_days"}
TEXT_COLS = {
    "run_at", "run_name", "stn_id", "collector", "scenario_id", "target_date", "start_date", "status",
    "status_start", "calc_version",
}  # fmt: skip

# 줄당 실측 크기(E2 2026-11-08, pg_total_relation_size ÷ 줄 수). dry-run 의 "줄어드는 용량" 계산용
ROW_BYTES = {
    "heat_daily": 219,
    "soil_water_daily": 253,
    "sun_daily": 187,
    "water_balance_daily": 376,
    "water_outlook": 247,
    "weather_daily": 301,
    "water_outlook_summary": 245,
    "forecast_runs": 224,  # 줄 수가 적어 부풀려진 실측(465B) 대신 추정
}
SYSTEM_MB = 12.0  # E2: 전체 57MB − 10표 합계 45MB


# ── 시각·기준 날짜 (모두 한국 시각) ───────────────────────────────────────
def _kst(t: Any) -> pd.Timestamp:
    """시각 → 한국 시각(시간대 없음). 시간대가 없으면 이미 한국 시각으로 본다."""
    ts = pd.Timestamp(t)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Seoul").tz_localize(None)
    return ts


def kst_date(t: Any) -> dt.date:
    return _kst(t).date()


def keep_from_date(now: Any) -> dt.date:
    """이 날짜부터는 DB 에 남긴다 = 오늘 − 14일. 이보다 앞선 날짜의 회차 줄이 보관 대상."""
    return kst_date(now) - dt.timedelta(days=OUTLOOK_KEEP_DAYS)


def is_expired(run_at: Any, now: Any) -> bool:
    return kst_date(run_at) < keep_from_date(now)


def max_through(now: Any) -> dt.date:
    """`--through` 로 줄 수 있는 가장 늦은 날짜(포함) = 남기는 첫 날의 전날"""
    return keep_from_date(now) - dt.timedelta(days=1)


def check_through(through: dt.date, now: Any) -> None:
    if through > max_through(now):
        raise ValueError(
            f"--through {through} 는 14일 안의 날짜를 포함합니다. {max_through(now)} 이전만 보관·삭제할 수 있습니다 "
            f"(오늘 − {OUTLOOK_KEEP_DAYS}일 이내는 DB 에 남깁니다)"
        )


def through_end_utc(through: dt.date) -> str:
    """`through` 날짜의 끝 = 다음 날 한국 자정(UTC 표기). run_at < 이 값 인 줄이 대상."""
    t = pd.Timestamp(through + dt.timedelta(days=1)).tz_localize("Asia/Seoul").tz_convert("UTC")
    return t.isoformat()


def archive_month(run_at: Any) -> str:
    return f"{_kst(run_at):%Y-%m}"


def archive_path(table: str, month: str) -> str:
    """`archive/<표>/<연>/<표>_<연>-<월>.csv.gz` (raw 버킷 안)"""
    if table not in ARCHIVE_COLUMNS:
        raise ValueError(f"보관 대상이 아닌 표입니다: {table} (water_outlook·water_outlook_summary 만)")
    return f"{ARCHIVE_ROOT}/{table}/{month[:4]}/{table}_{month}.csv.gz"


# ── 줄 만들기·합치기 ────────────────────────────────────────────────────
def flatten(table: str, row: Mapping[str, Any], run: Mapping[str, Any]) -> dict:
    """DB 줄 + 그 회차(forecast_runs) → 보관 줄: 회차 열을 앞에 붙이고 표의 모든 열(run_id 포함)을 그대로 둔다."""
    out: dict[str, Any] = {
        "run_at": _kst(run["run_at"]).isoformat() + "+09:00",
        "run_name": run.get("run_name"),
        "stn_id": run.get("stn_id"),
        "collector": run.get("collector"),
    }
    out.update({c: row.get(c) for c in TABLE_COLUMNS[table]})
    return out


def db_row(table: str, row: Mapping[str, Any]) -> dict:
    """보관 줄 → DB 줄(회차 열 제거). 복원(upsert)에 쓴다."""
    return {c: row.get(c) for c in TABLE_COLUMNS[table]}


def row_key(table: str, row: Mapping[str, Any]) -> tuple:
    return tuple(row.get(c) for c in ROW_KEY[table])


def sort_rows(table: str, rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """줄 순서: run_at, stn_id, scenario_id, (target_date) — 같은 입력은 같은 파일이 되게"""
    last = ("target_date",) if table == "water_outlook" else ()
    return sorted(
        (dict(r) for r in rows),
        key=lambda r: (
            (str(r["run_at"]), str(r.get("stn_id")), str(r.get("scenario_id")))
            + tuple(str(r.get(c)) for c in last)
        ),
    )


def merge_rows(
    table: str, existing: Sequence[Mapping[str, Any]], new: Sequence[Mapping[str, Any]]
) -> list[dict]:
    """기존 보관 줄에 새 줄을 더한다(키가 같으면 새 값). 같은 일을 두 번 해도 결과가 같다."""
    merged = {row_key(table, r): dict(r) for r in existing}
    merged.update({row_key(table, r): dict(r) for r in new})
    return sort_rows(table, list(merged.values()))


# ── 압축 파일(csv.gz) ───────────────────────────────────────────────────
def _cell(v: Any) -> str:
    return "" if v is None else str(v)


def encode_archive(table: str, rows: Sequence[Mapping[str, Any]]) -> bytes:
    """보관 줄 → csv.gz (UTF-8, 첫 줄 열 이름, 빈 칸 = None, gzip mtime = 0 이라 같은 내용은 같은 바이트)"""
    cols = ARCHIVE_COLUMNS[table]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator=chr(10))
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in cols])
    return gzip.compress(buf.getvalue().encode("utf-8"), mtime=0)


def decode_archive(table: str, data: bytes) -> list[dict]:
    """csv.gz → 보관 줄(숫자는 숫자로, 빈 칸은 None). 열 이름이 규칙과 다르면 거부한다."""
    cols = ARCHIVE_COLUMNS[table]
    reader = csv.reader(io.StringIO(gzip.decompress(data).decode("utf-8")))
    header = next(reader, None)
    if header != list(cols):
        raise ValueError(f"보관 파일의 열 이름이 규칙과 다릅니다: {header} != {list(cols)}")
    out = []
    for rec in reader:
        row: dict[str, Any] = {}
        for c, v in zip(cols, rec, strict=True):
            if v == "":
                row[c] = None
            elif c in INT_COLS:
                row[c] = int(v)
            elif c in TEXT_COLS:
                row[c] = v
            else:
                row[c] = float(v)
        out.append(row)
    return out


def sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# ── 예보 원문 (raw 버킷) ────────────────────────────────────────────────
_RAW_NAME = re.compile(r"(\d{8})_(\d{4})\.json\.(?:gz|xz)$")


def raw_prefix(representative: bool) -> str:
    """원문을 둘 접두어: 대표 10곳 `fcst`, 그 밖 `fcst-ext` (S4 가 대표 밖 지점을 켤 때 쓴다)"""
    return RAW_REP_PREFIX.rstrip("/") if representative else RAW_EXT_PREFIX.rstrip("/")


def raw_path(prefix: str, grid: str, issue: Any, ext: str = "json.gz") -> str:
    t = _kst(issue)
    return f"{prefix}/{grid}/{t:%Y}/{t:%m}/{t:%Y%m%d_%H%M}.{ext}"


def raw_issue_time(path: str) -> pd.Timestamp | None:
    """파일 이름의 발표 시각(한국). 이름이 규칙과 다르면 None(짐작하지 않는다)."""
    m = _RAW_NAME.search(path)
    return pd.Timestamp(f"{m.group(1)} {m.group(2)}") if m else None


def raw_expired(path: str, now: Any, days: int = RAW_EXT_KEEP_DAYS) -> bool:
    """90일 지난 대표 밖 원문인가. **`fcst-ext/`만 대상**이고 `fcst/`(대표 10곳)나 다른 경로는 늘 False."""
    if not path.startswith(RAW_EXT_PREFIX):
        return False
    issue = raw_issue_time(path)
    return issue is not None and (_kst(now) - issue) > pd.Timedelta(days=days)


def read_raw_doc(data: bytes) -> dict:
    """원문 파일(gzip 또는 xz) → 문서. 저장 형식이 바뀌어도 읽는 쪽이 깨지지 않게 매직 바이트로 알아본다."""
    if data[:2] == b"\x1f\x8b":
        raw = gzip.decompress(data)
    elif data[:6] == b"\xfd7zXZ\x00":
        raw = lzma.decompress(data)
    else:
        raise ValueError("알 수 없는 원문 압축 형식입니다 (gzip·xz 만 지원)")
    return json.loads(raw.decode("utf-8"))


def xz_saving(gz_files: Sequence[bytes]) -> dict:
    """gzip 원문 파일들을 xz(9e)로 다시 압축하면 얼마나 줄어드나(보고용, 읽기만 한다)"""
    gz_total = xz_total = 0
    for b in gz_files:
        raw = gzip.decompress(b)
        gz_total += len(b)
        xz_total += len(lzma.compress(raw, preset=9 | lzma.PRESET_EXTREME))
    return dict(
        n=len(gz_files),
        gz_bytes=gz_total,
        xz_bytes=xz_total,
        ratio=(xz_total / gz_total if gz_total else None),
    )


def db_bytes_freed(rows_by_table: Mapping[str, int]) -> int:
    """지우는 줄 수 × 줄당 실측 크기 = DB 에서 줄어드는 바이트(근사)"""
    return sum(n * ROW_BYTES[t] for t, n in rows_by_table.items())
