"""기상청 단기예보 격자 (위경도 → 격자). DB·API·파일을 모르는 순수 함수만 둔다. 이슈 #72(#28 S1, 결정 0025).

변환식은 기상청 공개 상수의 Lambert 정각원추다: 지구 반지름 6371.00877km, 격자 5km, 표준위도 30°·60°,
기준점 126°E·38°N → 격자 (43, 136), 반올림은 `floor(값 + 0.5)`.

규칙(2026-11-08 확정, 결정 0025):
  - DB(`stations.grid_nx/ny`)에 격자가 있으면 **DB 값이 기준**이다. 계산값과 다르면 경고만 하고 예보는 계속한다.
    (반올림 경계 근처 지점은 좌표가 조금만 달라도 한 칸 달라질 수 있고, 불일치를 이유로 건너뛰면 잘 돌던 대표 지점 예보가 멈춘다.)
  - 격자가 비어 있는 has_forecast 지점만 예보를 건너뛴다(관측은 계속).
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

GRID_METHOD = "kma_lcc_5km_v1"  # CSV·보고서에 적는 계산 방식 이름 (상수를 바꾸면 이름도 올린다)

# 기상청 공개 상수
EARTH_RADIUS_KM = 6371.00877
GRID_KM = 5.0
STD_LAT1_DEG = 30.0
STD_LAT2_DEG = 60.0
ORIGIN_LON_DEG = 126.0
ORIGIN_LAT_DEG = 38.0
ORIGIN_X = 43
ORIGIN_Y = 136

NEAR_BOUNDARY_CELLS = 0.02  # 반올림 경계까지 이 칸 수 이내면 "경계 근접" (CSV 표시, 시드 전 한 번 더 확인)

GRID_CSV_COLUMNS = ("stn_id", "name_ko", "lat", "lon", "grid_nx", "grid_ny", "method", "near_boundary")

_RAD = math.pi / 180.0
_RE = EARTH_RADIUS_KM / GRID_KM
_S1, _S2 = STD_LAT1_DEG * _RAD, STD_LAT2_DEG * _RAD
_OLON, _OLAT = ORIGIN_LON_DEG * _RAD, ORIGIN_LAT_DEG * _RAD
_SN = math.log(math.cos(_S1) / math.cos(_S2)) / math.log(
    math.tan(math.pi * 0.25 + _S2 * 0.5) / math.tan(math.pi * 0.25 + _S1 * 0.5)
)
_SF = math.tan(math.pi * 0.25 + _S1 * 0.5) ** _SN * math.cos(_S1) / _SN
_RO = _RE * _SF / math.tan(math.pi * 0.25 + _OLAT * 0.5) ** _SN


def _number(v: Any, name: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name}가 숫자가 아님: {v!r}") from e
    if not math.isfinite(x):
        raise ValueError(f"{name}가 유한한 숫자가 아님: {v!r}")
    return x


def raw_grid(lat: Any, lon: Any) -> tuple[float, float]:
    """반올림 전 격자 좌표. 위도 −90~90, 경도 −180~180 밖이면 ValueError."""
    la, lo = _number(lat, "위도"), _number(lon, "경도")
    if not -90.0 < la < 90.0:
        raise ValueError(f"위도 범위 밖: {la}")
    if not -180.0 <= lo <= 180.0:
        raise ValueError(f"경도 범위 밖: {lo}")
    ra = _RE * _SF / math.tan(math.pi * 0.25 + la * _RAD * 0.5) ** _SN
    theta = (lo * _RAD - _OLON + math.pi) % (2 * math.pi) - math.pi
    theta *= _SN
    return ra * math.sin(theta) + ORIGIN_X + 0.5, _RO - ra * math.cos(theta) + ORIGIN_Y + 0.5


def latlon_to_grid(lat: Any, lon: Any) -> tuple[int, int]:
    """위경도 → 단기예보 격자 (nx, ny)."""
    x, y = raw_grid(lat, lon)
    return math.floor(x), math.floor(y)


def boundary_margin(lat: Any, lon: Any) -> float:
    """반올림 경계까지 남은 칸 수(작을수록 한 칸 옆으로 갈 위험이 큼). 두 축 중 더 가까운 쪽."""
    x, y = raw_grid(lat, lon)
    return min(x % 1.0, 1.0 - x % 1.0, y % 1.0, 1.0 - y % 1.0)


def near_boundary(lat: Any, lon: Any) -> bool:
    return boundary_margin(lat, lon) <= NEAR_BOUNDARY_CELLS


@dataclass(frozen=True)
class GridCheck:
    """status: ok(DB = 계산) / differs(다름, DB 값이 기준) / missing(DB 격자 비어 있음) / unchecked(DB 격자는 있는데 좌표가 없어 비교 못 함)"""

    status: str
    db: tuple[int, int] | None
    calc: tuple[int, int] | None


def _db_grid(st: Mapping[str, Any]) -> tuple[int, int] | None:
    nx, ny = st.get("grid_nx"), st.get("grid_ny")
    if nx is None or ny is None:
        return None
    return int(round(_number(nx, "grid_nx"))), int(round(_number(ny, "grid_ny")))


def _calc_grid(st: Mapping[str, Any]) -> tuple[int, int] | None:
    try:
        return latlon_to_grid(st.get("lat"), st.get("lon"))
    except ValueError:
        return None


def check_station_grid(st: Mapping[str, Any]) -> GridCheck:
    db, calc = _db_grid(st), _calc_grid(st)
    if db is None:
        return GridCheck("missing", None, calc)
    if calc is None:
        return GridCheck("unchecked", db, None)
    return GridCheck("ok" if db == calc else "differs", db, calc)


def forecast_ready(st: Mapping[str, Any]) -> bool:
    """예보를 받을 수 있는 지점인가: has_forecast 이고 DB 격자가 있다. 계산값과 같을 필요는 없다(DB 값이 기준)."""
    return bool(st.get("has_forecast")) and _db_grid(st) is not None


# ── 운영 지점 표 ─────────────────────────────────────────────────────────
def _is_true(v: Any) -> bool:
    return v is True or str(v).strip().lower() == "true"


def grid_rows(stations: Iterable[Mapping[str, Any]]) -> list[dict]:
    """운영 지점(is_active)의 계산 격자 표. 지점 번호순. CSV 한 줄 = dict 하나(GRID_CSV_COLUMNS 열)."""
    rows = []
    for s in stations:
        if not _is_true(s.get("is_active")):
            continue
        lat, lon = _number(s["lat"], "위도"), _number(s["lon"], "경도")
        nx, ny = latlon_to_grid(lat, lon)
        rows.append(
            dict(
                stn_id=str(s["stn_id"]),
                name_ko=s.get("name_ko") or "",
                lat=lat,
                lon=lon,
                grid_nx=nx,
                grid_ny=ny,
                method=GRID_METHOD,
                near_boundary=near_boundary(lat, lon),
            )
        )
    return sorted(rows, key=lambda r: int(r["stn_id"]))


def overlaps(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[int, int], list[str]]:
    """같은 격자를 쓰는 지점들. 겹침이 없으면 빈 dict."""
    seen: dict[tuple[int, int], list[str]] = {}
    for r in rows:
        seen.setdefault((int(r["grid_nx"]), int(r["grid_ny"])), []).append(str(r["stn_id"]))
    return {k: v for k, v in seen.items() if len(v) > 1}
