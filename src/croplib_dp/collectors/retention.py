"""전망 표 월 보관·삭제·복원과 예보 원문 90일 정리 (이슈 #61, #28 S3-a, 결정 0022).

모드 (기본은 `plan` = 읽기만 하는 dry-run)
  plan    14일보다 오래된 water_outlook·water_outlook_summary 줄을 월 단위 csv.gz 로 어떻게 보관·삭제할지 **보고만** 한다.
          지울 줄 수·월별 보관 파일 크기·Storage 사용량·xz 로 바꾸면 줄어드는 크기를 적는다. 쓰기·삭제 0.
  apply   `--through <날짜>` 와 `--confirm-rows <N>`(dry-run 이 보고한 줄 수)이 **둘 다 맞아야** 실행한다.
          보관 파일을 올리고 → 다시 내려받아 줄 수·sha256·지울 줄이 모두 있음을 확인하고 → 지운다.
          하나라도 어긋나면 그 뒤를 하지 않고 멈춘다. forecast_runs 는 지우지 않는다(읽기만).
          대표 밖 원문(`fcst-ext/`)이 90일 지난 것이 있으면 `--confirm-raw-files <N>` 도 맞아야 같이 지운다.
  restore 보관 파일 → 표로 되살린다(기본은 보고만, `--apply` 면 같은 키로 upsert).
키는 환경변수(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)로만 읽는다.
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import argparse
import datetime as dt
import gzip
import json
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from croplib_dp.collectors.water import _env, kst_now
from croplib_dp.core import retention as R
from croplib_dp.io.db_stats import HeadRequest, http_request_h, read_all, row_count
from croplib_dp.io.dictionaries import RestGet, rest_get
from croplib_dp.io.storage import Storage
from croplib_dp.io.supabase_write import SupabaseWriter, http_request

BUCKET = "raw"
TABLES = ("water_outlook", "water_outlook_summary")
RUN_CHUNK = 50  # 한 번에 지우는 회차 수
XZ_SAMPLE = 200
OUT_NAME = "retention"
RUN_EMBED = "forecast_runs!inner(run_at,run_name,stn_id,collector)"


class RetentionError(RuntimeError):
    """확인이 어긋나 더 진행하지 않고 멈춘다(조용히 넘어가지 않는다)"""


@dataclass
class Env:
    base: str
    key: str
    http: Callable[[str, str, dict, bytes], tuple[int, bytes]] = http_request
    rest_get: RestGet = rest_get
    head: HeadRequest = http_request_h
    storage: Storage = field(init=False)
    writer: SupabaseWriter = field(init=False)

    def __post_init__(self) -> None:
        self.storage = Storage(self.base, self.key, self.http)
        self.writer = SupabaseWriter(self.base, self.key, self.http)

    @classmethod
    def from_environ(cls) -> "Env":
        return cls(_env("SUPABASE_URL"), _env("SUPABASE_SERVICE_ROLE_KEY"))

    def count(self, table: str, query: str = "") -> int:
        return row_count(self.base, self.key, table, query, self.head)

    def read(self, table: str, query: str, order: str) -> list[dict]:
        return read_all(self.base, self.key, table, query, order, self.rest_get)


@dataclass
class MonthPlan:
    table: str
    month: str
    path: str
    new_rows: int
    existing_rows: int
    merged_rows: int
    csv_bytes: int
    gz_bytes: int
    sha256: str
    data: bytes = field(repr=False)
    run_ids: list[int] = field(default_factory=list)
    keys: set = field(default_factory=set, repr=False)


@dataclass
class Plan:
    now: pd.Timestamp
    through: dt.date
    rows: dict[str, int] = field(default_factory=dict)
    runs: dict[str, int] = field(default_factory=dict)
    months: list[MonthPlan] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    forecast_runs_rows: int = 0
    storage_usage: dict[str, dict] = field(default_factory=dict)
    raw_expired: list[str] = field(default_factory=list)
    raw_expired_bytes: int = 0
    xz: dict = field(default_factory=lambda: dict(n=0, gz_bytes=0, xz_bytes=0, ratio=None))
    xz_total_saving_bytes: float = 0.0

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())

    @property
    def db_bytes_freed(self) -> int:
        return R.db_bytes_freed(self.rows)


@dataclass
class ApplyResult:
    through: dt.date
    deleted: dict[str, int]
    raw_deleted: int
    raw_path_cleared: int
    archives: list[str]


@dataclass
class RestoreResult:
    table: str
    month: str
    path: str
    rows: int
    missing_runs: list[int]
    applied: bool


# ── plan ───────────────────────────────────────────────────────────────
def _is_gzip(b: bytes) -> bool:
    try:
        gzip.decompress(b)
        return True
    except Exception:
        return False


def _bucket_usage(files: Sequence[dict]) -> dict[str, dict]:
    usage = {p: dict(files=0, bytes=0) for p in (R.RAW_REP_PREFIX, R.RAW_EXT_PREFIX, R.ARCHIVE_ROOT + "/")}
    for f in files:
        for prefix in usage:
            if f["path"].startswith(prefix):
                usage[prefix]["files"] += 1
                usage[prefix]["bytes"] += f["size"]
    return usage


def _sample(items: list, n: int) -> list:
    """n개를 고르게 뽑는다(앞에서 뒤까지)"""
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return list(items)
    step = (len(items) - 1) / (n - 1) if n > 1 else 0
    return [items[round(i * step)] for i in range(n)]


def _expired_rows(env: Env, table: str, through_end: str) -> list[tuple[dict, dict]]:
    """보관 대상 줄 [(DB 줄, 그 회차)]: 외래키로 forecast_runs 를 붙여 회차 시각이 through 끝 이전인 줄"""
    end = urllib.parse.quote(through_end, safe="")
    order = ",".join(R.ROW_KEY[table])
    try:
        rows = env.read(table, f"select=*,{RUN_EMBED}&forecast_runs.run_at=lt.{end}", order)
    except RuntimeError as e:
        if "HTTP 400" not in str(e) and "PGRST200" not in str(e):
            raise
        return _expired_rows_two_step(
            env, table, end
        )  # 조인 조회가 안 되는 DB: 회차를 먼저 읽고 줄을 따로 읽는다
    out = []
    for r in rows:
        run = r.pop("forecast_runs")
        out.append((r, run))
    return out


def _expired_rows_two_step(env: Env, table: str, end_quoted: str) -> list[tuple[dict, dict]]:
    """조인 없이: ① forecast_runs 에서 기준 이전 회차를 읽고 ② 그 회차들의 줄을 run_id 묶음으로 읽는다"""
    runs = env.read(
        "forecast_runs",
        f"select=run_id,run_at,run_name,stn_id,collector&run_at=lt.{end_quoted}",
        "run_id",
    )
    by_id = {r["run_id"]: r for r in runs}
    out: list[tuple[dict, dict]] = []
    for ids in _chunks(sorted(by_id), 100):
        for r in env.read(table, f"select=*&run_id=in.{_in(ids)}", ",".join(R.ROW_KEY[table])):
            out.append((r, by_id[r["run_id"]]))
    return out


def build_plan(env: Env, now: Any, through: dt.date | None = None, xz_sample: int = XZ_SAMPLE) -> Plan:
    now = pd.Timestamp(now)
    through = through or R.max_through(now)
    R.check_through(through, now)
    end = R.through_end_utc(through)
    plan = Plan(now=now, through=through)
    for table in TABLES:
        pairs = _expired_rows(env, table, end)
        plan.rows[table] = len(pairs)
        plan.runs[table] = len({r["run_id"] for r, _ in pairs})
        by_month: dict[str, list[tuple[dict, dict]]] = {}
        for r, run in pairs:
            by_month.setdefault(R.archive_month(run["run_at"]), []).append((r, run))
        for month in sorted(by_month):
            path = R.archive_path(table, month)
            new_rows = [R.flatten(table, r, run) for r, run in by_month[month]]
            old = env.storage.download(BUCKET, path)
            try:
                existing = R.decode_archive(table, old) if old is not None else []
            except Exception as e:  # 기존 보관 파일이 깨졌으면 덮어쓰지 않고 멈춘다
                raise RetentionError(
                    f"기존 보관 파일을 읽지 못했습니다: {BUCKET}/{path} ({type(e).__name__}: {str(e)[:100]})"
                ) from e
            merged = R.merge_rows(table, existing, new_rows)
            data = R.encode_archive(table, merged)
            plan.months.append(
                MonthPlan(
                    table=table,
                    month=month,
                    path=path,
                    new_rows=len(new_rows),
                    existing_rows=len(existing),
                    merged_rows=len(merged),
                    csv_bytes=len(gzip.decompress(data)),
                    gz_bytes=len(data),
                    sha256=R.sha256(data),
                    data=data,
                    run_ids=sorted({r["run_id"] for r, _ in by_month[month]}),
                    keys={R.row_key(table, r) for r, _ in by_month[month]},
                )
            )
        plan.counts[table] = env.count(table)
    plan.forecast_runs_rows = env.count("forecast_runs")
    files = env.storage.list_files(BUCKET)
    plan.storage_usage = _bucket_usage(files)
    expired = [f for f in files if R.raw_expired(f["path"], now)]
    plan.raw_expired = [f["path"] for f in expired]
    plan.raw_expired_bytes = sum(f["size"] for f in expired)
    gz = [
        f
        for f in files
        if f["path"].startswith((R.RAW_REP_PREFIX, R.RAW_EXT_PREFIX)) and f["path"].endswith(".gz")
    ]
    blobs = [
        b for b in (env.storage.download(BUCKET, f["path"]) for f in _sample(gz, xz_sample)) if b is not None
    ]
    blobs = [b for b in blobs if _is_gzip(b)]  # 깨진 파일은 표본에서 뺀다(보고용 추정이라 멈추지 않는다)
    if blobs:
        plan.xz = R.xz_saving(blobs)
        raw_bytes = (
            plan.storage_usage[R.RAW_REP_PREFIX]["bytes"] + plan.storage_usage[R.RAW_EXT_PREFIX]["bytes"]
        )
        plan.xz_total_saving_bytes = raw_bytes * (1 - plan.xz["ratio"])
    return plan


def _mb(n: float) -> str:
    return f"{n / 1024 / 1024:.2f}MB"


def report_dict(plan: Plan) -> dict:
    return dict(
        through=plan.through.isoformat(),
        now=str(plan.now),
        rows=plan.rows,
        runs=plan.runs,
        total_rows=plan.total_rows,
        db_bytes_freed=plan.db_bytes_freed,
        counts_now=plan.counts,
        counts_after={t: plan.counts[t] - plan.rows[t] for t in plan.rows},
        forecast_runs_rows=plan.forecast_runs_rows,
        forecast_runs_deleted=0,
        months=[
            dict(
                table=m.table,
                month=m.month,
                path=m.path,
                new_rows=m.new_rows,
                existing_rows=m.existing_rows,
                merged_rows=m.merged_rows,
                csv_bytes=m.csv_bytes,
                gz_bytes=m.gz_bytes,
                sha256=m.sha256,
            )
            for m in plan.months
        ],  # fmt: skip
        storage_usage=plan.storage_usage,
        raw_expired_files=len(plan.raw_expired),
        raw_expired_bytes=plan.raw_expired_bytes,
        xz=plan.xz,
        xz_total_saving_bytes=plan.xz_total_saving_bytes,
    )


def report_markdown(plan: Plan) -> str:
    L = [
        f"# 보관·삭제 dry-run 보고 ({plan.now:%Y-%m-%d %H:%M} KST) — 아무것도 쓰거나 지우지 않았습니다",
        "",
        f"- 기준: `--through {plan.through}`(이 날짜 포함, 한국 날짜) 이전 회차 줄을 보관 대상으로 봄. 오늘 − 14일부터는 DB에 남김.",
        f"- **지울 줄 합계: {plan.total_rows}줄** (water_outlook {plan.rows['water_outlook']}줄 / 회차 {plan.runs['water_outlook']}개, "
        f"water_outlook_summary {plan.rows['water_outlook_summary']}줄 / 회차 {plan.runs['water_outlook_summary']}개)",
        f"- DB에서 줄어드는 용량(줄 수 × 줄당 실측 크기): 약 {_mb(plan.db_bytes_freed)}",
        "",
        "## 표별 줄 수 (삭제 전 → 후)",
        "",
        "| 표 | 지금 | 지울 줄 | 삭제 후 |",
        "|---|---:|---:|---:|",
    ]
    for t in TABLES:
        L.append(f"| {t} | {plan.counts[t]} | {plan.rows[t]} | {plan.counts[t] - plan.rows[t]} |")
    L += [
        f"| forecast_runs | {plan.forecast_runs_rows} | **삭제 0** (전량 보관) | {plan.forecast_runs_rows} |",
        "",
        "## 월별 보관 파일 (`raw` 버킷)",
        "",
        "| 표 | 월 | 경로 | 새 줄 | 기존 파일 줄 | 합친 줄 | csv 크기 | gz 크기 | sha256(앞 12자) |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for m in plan.months:
        L.append(
            f"| {m.table} | {m.month} | `{m.path}` | {m.new_rows} | {m.existing_rows} | {m.merged_rows} | "
            f"{m.csv_bytes:,}B | {m.gz_bytes:,}B | {m.sha256[:12]} |"
        )
    if not plan.months:
        L.append("| (보관할 줄 없음) | | | | | | | | |")
    gz_total = sum(m.gz_bytes for m in plan.months)
    L += [
        "",
        f"- 이번에 올릴 보관 파일 크기 합계(gz): {gz_total:,}B ({_mb(gz_total)})",
        "",
        "## Storage 사용량 (`raw` 버킷)",
        "",
    ]
    L += ["| 접두어 | 파일 수 | 크기 |", "|---|---:|---:|"]
    for prefix, u in plan.storage_usage.items():
        L.append(f"| `{prefix}` | {u['files']} | {_mb(u['bytes'])} |")
    L += [
        "",
        f"- 대표 밖 원문(`fcst-ext/`) 중 발표가 90일 지난 파일: **{len(plan.raw_expired)}개** ({_mb(plan.raw_expired_bytes)}). 대표 10곳 `fcst/`는 대상이 아닙니다.",
    ]
    for p in plan.raw_expired[:10]:
        L.append(f"  - `{p}`")
    if plan.xz["n"]:
        L += [
            "",
            "## 원문을 xz로 바꾸면 (보고만, 바꾸지 않음)",
            "",
            f"- 표본 {plan.xz['n']}개: gzip {plan.xz['gz_bytes']:,}B → xz {plan.xz['xz_bytes']:,}B (gzip의 {plan.xz['ratio'] * 100:.1f}%).",
            f"- `fcst/`·`fcst-ext/` 전체에 곱하면 줄어드는 크기: 약 {_mb(plan.xz_total_saving_bytes)}.",
        ]
    L += [
        "",
        "## 실제 삭제하려면 (승인 뒤, 수동 실행 한 번)",
        "",
        f"`apply --through {plan.through} --confirm-rows {plan.total_rows}`"
        + (f" `--confirm-raw-files {len(plan.raw_expired)}`" if plan.raw_expired else ""),
        "",
        "줄 수·날짜가 하나라도 다르면 아무것도 지우지 않고 멈춥니다. 삭제 전에 보관 파일을 올리고 다시 내려받아 줄 수·sha256과 지울 줄이 모두 있음을 확인합니다.",
    ]
    return "\n".join(L) + "\n"


# ── apply ──────────────────────────────────────────────────────────────
def _in(values: Sequence[Any]) -> str:
    return "(" + ",".join(urllib.parse.quote(str(v), safe="") for v in values) + ")"


def _chunks(seq: Sequence, n: int):
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])


def apply(
    env: Env,
    now: Any,
    through: dt.date | None,
    confirm_rows: int | None,
    confirm_raw_files: int = 0,
) -> ApplyResult:
    if through is None or confirm_rows is None:
        raise RetentionError(
            "실제 삭제에는 --through <날짜> 와 --confirm-rows <N>(dry-run 이 보고한 줄 수)이 모두 필요합니다"
        )
    R.check_through(through, now)
    plan = build_plan(env, now, through, xz_sample=0)
    if plan.total_rows != confirm_rows:
        raise RetentionError(
            f"승인한 줄 수({confirm_rows})와 지금 지울 줄 수({plan.total_rows})가 다릅니다 — 아무것도 지우지 않았습니다. dry-run 을 다시 확인하세요"
        )
    if len(plan.raw_expired) != confirm_raw_files:
        raise RetentionError(
            f"승인한 원문 파일 수({confirm_raw_files})와 90일 지난 대표 밖 원문 수({len(plan.raw_expired)})가 다릅니다 — 아무것도 지우지 않았습니다"
        )
    deleted = {t: 0 for t in TABLES}
    archives: list[str] = []
    for m in sorted(plan.months, key=lambda x: (x.month, x.table)):
        env.storage.upload(BUCKET, m.path, m.data)
        back = env.storage.download(BUCKET, m.path)
        if back is None or R.sha256(back) != m.sha256:
            raise RetentionError(
                f"{m.path}: 올린 보관 파일을 다시 내려받아 확인하지 못했습니다(내려받은 파일이 올린 것과 다름). 이 뒤로 지우지 않습니다"
            )
        rows_back = R.decode_archive(m.table, back)
        if len(rows_back) != m.merged_rows:
            raise RetentionError(
                f"{m.path}: 내려받은 보관 파일의 줄 수({len(rows_back)})가 올리기 전 계산값({m.merged_rows})과 다릅니다"
            )
        arch_keys = {R.row_key(m.table, r) for r in rows_back}
        if not m.keys <= arch_keys:
            raise RetentionError(f"{m.path}: 지울 줄 {len(m.keys - arch_keys)}개가 보관 파일에 없습니다")
        archives.append(m.path)
        key_cols = ",".join(R.ROW_KEY[m.table])
        for ids in _chunks(m.run_ids, RUN_CHUNK):
            current = env.read(m.table, f"select={key_cols}&run_id=in.{_in(ids)}", key_cols)
            extra = {R.row_key(m.table, r) for r in current} - arch_keys
            if extra:
                raise RetentionError(
                    f"{m.table}: 보관 파일에 없는 줄 {len(extra)}개가 있어 지우지 않고 멈춥니다 (예: {sorted(extra, key=str)[0]})"
                )
            gone = env.writer.delete(m.table, f"run_id=in.{_in(ids)}", key_cols)
            if len(gone) != len(current):
                raise RetentionError(
                    f"{m.table}: 지우려던 {len(current)}줄과 지워진 {len(gone)}줄이 다릅니다"
                )
            deleted[m.table] += len(gone)
    raw_deleted = cleared = 0
    if plan.raw_expired:
        raw_deleted = env.storage.delete(BUCKET, plan.raw_expired)
        for part in _chunks(plan.raw_expired, RUN_CHUNK):
            cleared += len(
                env.writer.patch("forecast_runs", f"raw_path=in.{_in(part)}", {"raw_path": None}, "run_id")
            )
    return ApplyResult(
        through=through, deleted=deleted, raw_deleted=raw_deleted, raw_path_cleared=cleared, archives=archives
    )


def apply_markdown(res: ApplyResult, now: Any) -> str:
    total = sum(res.deleted.values())
    L = [
        f"# 보관·삭제 실행 결과 ({pd.Timestamp(now):%Y-%m-%d %H:%M} KST, `--through {res.through}`)",
        "",
        f"- **지운 줄 합계: {total}줄** (water_outlook {res.deleted['water_outlook']}, water_outlook_summary {res.deleted['water_outlook_summary']})",
        f"- 올린 보관 파일 {len(res.archives)}개(모두 다시 내려받아 줄 수·sha256 확인 뒤 삭제):",
    ]
    L += [f"  - `{p}`" for p in res.archives]
    L += [
        "- forecast_runs: 삭제 0",
        f"- 대표 밖 원문 삭제 {res.raw_deleted}개, forecast_runs.raw_path 비움 {res.raw_path_cleared}줄",
    ]
    return "\n".join(L) + "\n"


# ── restore ────────────────────────────────────────────────────────────
def restore(env: Env, table: str, month: str, apply: bool = False) -> RestoreResult:
    path = R.archive_path(table, month)
    data = env.storage.download(BUCKET, path)
    if data is None:
        raise RetentionError(f"보관 파일이 없습니다: {BUCKET}/{path}")
    rows = R.decode_archive(table, data)
    run_ids = sorted({r["run_id"] for r in rows})
    present: set[int] = set()
    for ids in _chunks(run_ids, 100):
        present |= {
            r["run_id"] for r in env.read("forecast_runs", f"select=run_id&run_id=in.{_in(ids)}", "run_id")
        }
    missing = sorted(set(run_ids) - present)
    if apply:
        if missing:
            raise RetentionError(
                f"forecast_runs 에 없는 run_id {missing[:10]} — 외래키가 맞지 않아 복원하지 않습니다"
            )
        env.writer.upsert(table, [R.db_row(table, r) for r in rows], R.ROW_KEY[table])
    return RestoreResult(
        table=table, month=month, path=path, rows=len(rows), missing_runs=missing, applied=apply
    )


def restore_markdown(r: RestoreResult) -> str:
    state = "복원했습니다(upsert)" if r.applied else "보고만 했습니다(--apply 를 주면 복원)"
    miss = (
        f"forecast_runs 에 없는 run_id: {r.missing_runs[:10]}"
        if r.missing_runs
        else "모든 run_id 가 forecast_runs 에 있습니다"
    )
    return (
        f"# 복원 {'실행' if r.applied else 'dry-run'}: {r.table} {r.month}\n\n"
        f"- 파일: `{BUCKET}/{r.path}`, 줄 {r.rows}개\n- {miss}\n- {state}\n"
    )


# ── CLI ────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    p = argparse.ArgumentParser(
        description="전망 표 월 보관·삭제·복원, 대표 밖 원문 90일 정리 (기본은 dry-run)"
    )
    p.add_argument("mode", nargs="?", choices=["plan", "apply", "restore"], default="plan")
    p.add_argument("--out", default="out", help="결과 폴더")
    p.add_argument("--through", help="보관·삭제할 마지막 날짜(포함, YYYY-MM-DD). apply 에 필수")
    p.add_argument(
        "--confirm-rows",
        type=int,
        help="dry-run 이 보고한 지울 줄 수. apply 에 필수, 다르면 아무것도 지우지 않음",
    )
    p.add_argument(
        "--confirm-raw-files", type=int, default=0, help="90일 지난 대표 밖 원문 파일 수(없으면 0)"
    )
    p.add_argument("--xz-sample", type=int, default=XZ_SAMPLE, help="plan: xz 절감 크기를 재는 원문 표본 수")
    p.add_argument("--table", choices=list(TABLES), help="restore: 표")
    p.add_argument("--month", help="restore: 월(YYYY-MM)")
    p.add_argument("--apply", action="store_true", help="restore: 실제로 되살림(기본은 보고만)")
    a = p.parse_args(argv)
    env = env or Env.from_environ()
    now = kst_now()
    through = dt.date.fromisoformat(a.through) if a.through else None
    out = Path(a.out) / OUT_NAME
    out.mkdir(parents=True, exist_ok=True)
    try:
        if a.mode == "plan":
            plan = build_plan(env, now, through, xz_sample=a.xz_sample)
            text = report_markdown(plan)
            (out / "plan.md").write_text(text, encoding="utf-8")
            (out / "plan.json").write_text(
                json.dumps(report_dict(plan), ensure_ascii=False, indent=1), encoding="utf-8"
            )
        elif a.mode == "apply":
            res = apply(env, now, through, a.confirm_rows, a.confirm_raw_files)
            text = apply_markdown(res, now)
            (out / "apply.md").write_text(text, encoding="utf-8")
        else:
            if not a.table or not a.month:
                raise RetentionError("restore 에는 --table 과 --month(YYYY-MM)가 필요합니다")
            r = restore(env, a.table, a.month, apply=a.apply)
            text = restore_markdown(r)
            (out / "restore.md").write_text(text, encoding="utf-8")
    except (RetentionError, ValueError) as e:
        raise SystemExit(f"중단: {e}") from e
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
