"""올리기 전 계약 검사 (데이터 계약 v1.3, CropMon `docs/DATA_CONTRACT.md`).

DB는 구조(키, 연결, 이름이 정해진 칸)만 막고, 값의 범위는 여기서 막는다(CropMon 결정 0004).
한 줄이라도 어긋나면 그 회차의 계산 결과는 올리지 않고, 회차 기록에 '실패'와 이유를 남긴다.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = "v1.3"
STATUS = ("need", "caution", "normal", "wet", "dormant")


@dataclass(frozen=True)
class TableSpec:
    columns: tuple[str, ...]  # 올리는 열 (이 순서, 이 이름만)
    required: tuple[str, ...]  # 비어 있으면 안 되는 열
    ranges: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    enums: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    key: tuple[str, ...] = ()


SPECS: dict[str, TableSpec] = {
    "forecast_runs": TableSpec(
        columns=(
            "run_at",
            "run_name",
            "stn_id",
            "started_at",
            "finished_at",
            "result",
            "on_time",
            "src_run_at",
            "rs_method",
            "obs_prev_date",
            "attempts",
            "collector",
            "raw_path",
            "error",
        ),
        required=("run_at", "run_name", "stn_id", "started_at", "result", "on_time", "attempts", "collector"),
        ranges={"attempts": (1, 1000)},
        enums={
            "run_name": ("아침", "저녁"),
            "result": ("정시 성공", "백업: S3", "백업: 직전 발표", "지연 수집", "실패"),
            "rs_method": ("S3", "S4", None),
            "collector": ("actions", "pc"),
        },
        key=("run_at", "stn_id", "collector"),
    ),
    "weather_daily": TableSpec(
        columns=(
            "stn_id",
            "date",
            "tmax_c",
            "tmin_c",
            "tavg_c",
            "rh_mean_pct",
            "rh_min_pct",
            "wind_ms",
            "u2_ms",
            "sunshine_h",
            "rs_mj",
            "rain_mm",
            "eto_mm",
            "filled",
            "fetched_at",
            "calc_version",
        ),
        required=("stn_id", "date", "filled", "fetched_at", "calc_version"),
        ranges={
            "tmax_c": (-40, 50),
            "tmin_c": (-40, 50),
            "tavg_c": (-40, 50),
            "rh_mean_pct": (0, 100),
            "rh_min_pct": (0, 100),
            "wind_ms": (0, 60),
            "u2_ms": (0, 60),
            "sunshine_h": (0, 15),
            "rs_mj": (0, 40),
            "rain_mm": (0, 500),
            "eto_mm": (0, 15),
        },
        key=("stn_id", "date"),
    ),
    "water_balance_daily": TableSpec(
        columns=(
            "scenario_id",
            "date",
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
        ),
        required=("scenario_id", "date", "eto_src", "irr_net_mm", "status", "filled", "calc_version"),
        ranges={
            "kc": (0, 1.5),
            "eto_mm": (0, 15),
            "etc_mm": (0, 25),
            "rain_mm": (0, 500),
            "irr_net_mm": (0, 500),
            "ks": (0, 1),
            "etc_adj_mm": (0, 25),
            "dp_mm": (0, 1000),
        },
        enums={"eto_src": ("관측", "예보", "없음"), "status": STATUS},
        key=("scenario_id", "date"),
    ),
    "water_outlook": TableSpec(
        columns=(
            "run_id",
            "scenario_id",
            "target_date",
            "lead_day",
            "eto_mm",
            "etc_mm",
            "etc_err_mm",
            "rain_fcst_mm",
            "pop_pct",
            "rain_expected_mm",
            "dr_center_mm",
            "dr_early_mm",
            "dr_late_mm",
            "status",
            "calc_version",
        ),
        required=("scenario_id", "target_date", "lead_day", "status", "calc_version"),
        ranges={
            "lead_day": (0, 4),
            "eto_mm": (0, 15),
            "etc_mm": (0, 25),
            "etc_err_mm": (0, 25),
            "rain_fcst_mm": (0, 500),
            "pop_pct": (0, 100),
            "rain_expected_mm": (0, 500),
        },
        enums={"status": ("need", "caution", "normal", "dormant")},  # 예보 날은 과습을 판정하지 않는다
        key=("run_id", "scenario_id", "target_date"),
    ),
}
SPECS["water_outlook_summary"] = TableSpec(
    columns=(
        "run_id",
        "scenario_id",
        "horizon_days",
        "start_date",
        "dr_start_mm",
        "status_start",
        "need_center_in",
        "need_early_in",
        "need_late_in",
        "advice_net_mm",
        "advice_gross_mm",
        "etc_sum_mm",
        "etc_sum_err_mm",
        "rain_sum_mm",
        "rain_expected_sum_mm",
        "calc_version",
    ),
    required=("scenario_id", "horizon_days", "start_date", "status_start", "calc_version"),
    ranges={
        "horizon_days": (1, 14),
        "need_center_in": (0, 14),
        "need_early_in": (0, 14),
        "need_late_in": (0, 14),
        "advice_net_mm": (0, 500),
        "advice_gross_mm": (0, 600),
        "etc_sum_mm": (0, 200),
        "etc_sum_err_mm": (0, 200),
        "rain_sum_mm": (0, 2000),
        "rain_expected_sum_mm": (0, 2000),
    },
    enums={"status_start": STATUS},
    key=("run_id", "scenario_id"),
)
DR_COLUMNS = {
    "water_balance_daily": ("dr_prev_mm", "dr_mm"),
    "water_outlook": ("dr_center_mm", "dr_early_mm", "dr_late_mm"),
    "water_outlook_summary": ("dr_start_mm", "advice_net_mm"),
}


def _is_missing(v: Any) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def check_rows(
    table: str,
    rows: Iterable[Mapping[str, Any]],
    taw_by_scenario: Mapping[str, float] | None = None,
    ignore: tuple[str, ...] = (),
    extra: tuple[str, ...] = (),
    max_report: int = 10,
) -> list[str]:
    """줄 목록 → 어긋난 곳 설명 목록(없으면 빈 목록).
    taw_by_scenario: 계산 조건별 TAW — 부족한 물(Dr)이 0~TAW 안인지 본다.
    ignore: 아직 정해지지 않아 검사에서 빼는 열(예: 올리기 전 water_outlook.run_id).
    extra: 계약 열 말고 함께 있어도 되는 열(예: run_id 대신 붙여 둔 회차 열쇠 run_at·stn_id·collector).
           키 중복 검사에는 이 열도 넣는다."""
    spec = SPECS[table]
    want = set(spec.columns) - set(ignore)
    problems: list[str] = []
    n_bad = 0
    seen: set = set()
    for i, r in enumerate(rows):
        got = set(r) - set(ignore) - set(extra)
        bad: list[str] = []
        if got != want:
            if want - got:
                bad.append(f"빠진 열 {sorted(want - got)}")
            if got - want:
                bad.append(f"계약에 없는 열 {sorted(got - want)}")
        for c in spec.required:
            if c in r and c not in ignore and _is_missing(r[c]):
                bad.append(f"{c} 비어 있음")
        for c, (lo, hi) in spec.ranges.items():
            v = r.get(c)
            if _is_missing(v):
                continue
            if not isinstance(v, int | float) or isinstance(v, bool) or not (lo <= v <= hi):
                bad.append(f"{c}={v} (범위 {lo}~{hi})")
        for c, allowed in spec.enums.items():
            if c in r and r[c] not in allowed:
                bad.append(f"{c}={r[c]!r} (허용 {list(allowed)})")
        for c in DR_COLUMNS.get(table, ()):
            v = r.get(c)
            if _is_missing(v):
                continue
            taw = (taw_by_scenario or {}).get(r.get("scenario_id"))
            hi = taw + 1e-6 if taw is not None else math.inf
            if not (-1e-6 <= v <= hi):
                bad.append(f"{c}={v} (범위 0~{taw})")
        k = tuple(r.get(c) for c in spec.key if c not in ignore) + tuple(r.get(c) for c in extra)
        if spec.key and k in seen:
            bad.append(f"같은 키가 두 번 {k}")
        seen.add(k)
        if bad:
            n_bad += 1
            if len(problems) < max_report:
                problems.append(f"{table} {i + 1}번째 줄: " + "; ".join(bad))
    if n_bad > len(problems):
        problems.append(f"{table}: 어긋난 줄 모두 {n_bad}개 (처음 {len(problems)}개만 적음)")
    return problems
