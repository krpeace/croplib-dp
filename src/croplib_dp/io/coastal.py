"""지점별 식50 kRs: 해안 목록(`data/coastal_stations.csv`)에서 읽는다 (이슈 #48 PR-B, 결정 0021).

해안선까지 10 km 이하인 지점은 0.19, 아니면(목록에 없는 지점 포함) 내륙 기본값 0.16이다.
**관측 결측일 대체(식50)에만** 쓰인다(`core/daily_weather`의 `StationMeta.krs`). 예보용 kRs
(`io/coefficients.py`, `core/solar.KRS_FAO`)는 이 목록을 읽지 않는다.
목록은 `collectors/radiation_report.py --coastal-csv`로 만든다(점검 결과 + 해안선 거리).
"""

import csv
from functools import lru_cache
from importlib import resources
from typing import Any

from croplib_dp.core.radiation import KRS_INLAND

DATA = "croplib_dp.data"
FILENAME = "coastal_stations.csv"


@lru_cache(maxsize=1)
def _load() -> dict[str, float]:
    with resources.files(DATA).joinpath(FILENAME).open(encoding="utf-8-sig") as f:
        return {str(r["stn_id"]).strip(): float(r["krs"]) for r in csv.DictReader(f)}


def coastal_krs(stn_id: Any) -> float:
    """지점의 식50 kRs. 목록에 없거나 지점 번호가 없으면 내륙 기본값."""
    if stn_id is None:
        return KRS_INLAND
    return _load().get(str(stn_id).strip(), KRS_INLAND)
