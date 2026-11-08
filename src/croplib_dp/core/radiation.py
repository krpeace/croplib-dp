"""일사(Rs) 유효성과 대체 순서 (이슈 #44, 결정 0020). 순수 함수만 둔다.

문제: 일부 지점은 일사(sumGsr)가 `0.0`으로만 오거나(센서 고장) 아예 없다. `0.0`을 관측값으로 쓰면 맑은 여름 날
ETo가 크게 줄어든다. 값 범위 검사(0~40)로는 걸러지지 않는다.

규칙
  1. 일 단위: 일사가 0.01 × Ra(천문일사)보다 작으면 결측으로 본다. 이 규칙의 목적은 물리적으로
     불가능한 값(0.0 등)만 거르는 것이다. 센서 고장은 아래 월 단위 규칙이 잡는다.
     (0.05·0.02 × Ra 는 호우 날의 진짜 낮은 일사까지 버려서 쓰지 않는다. 결정 0020)
  2. 달력 월 단위: 한 지점의 한 달에 자료가 10일 이상이고 그중 무효가 90% 이상이면
     그 달 일사를 전부 무효로 본다.
     자료가 10일 미만인 달은 일 단위 규칙만 쓴다. 달마다 따로 판단해서, 센서를 고친 다음 달부터는 다시 쓴다.
  3. 대체 순서: 유효한 일사 → 일조시간(FAO-56 식35) → 기온차(식50, kRs는 지점별) → 비움(None)
  4. 앞 7일 평균으로 채운 날이 연속 7일을 넘으면 8일째부터 ETo를 비운다 (daily_weather 에서 쓴다)
  5. 지점 단위: 평년선의 유효 일수가 100일 미만이면 일사 미측정 지점으로 본다 (적산일사 수집기에서 쓴다)
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Mapping

from croplib_dp.core.fao56 import extra_radiation, rs_from_sunshine
from croplib_dp.core.solar import KRS_FAO, ra_rso, rs_s1

KRS_INLAND = KRS_FAO  # 식50 kRs 기본값(내륙). 해안 값은 지점별로 StationMeta.krs 에 넣는다 (io/coastal.py)
KRS_COASTAL = 0.19  # 해안 지점(해안선 10 km 이하) 식50 kRs (#48 승인 2026-11-08, 결정 0021)
COASTAL_MAX_KM = 10.0  # 해안선까지 이 거리 이하(같으면 포함)이면 해안
MIN_RS_RATIO = 0.01  # 일사 하한 = 0.01 × Ra (결정 0020: 정상 지점에서 무효가 되는 날 0.01%)
MONTH_MIN_DAYS = 10  # 이 일수 이상 자료가 있는 달만 월 단위로 판단한다
MONTH_INVALID_RATIO = 0.9  # 자료 중 무효가 이 비율 이상이면 그 달 전체 무효
MAX_FILL_DAYS = 7  # 앞 7일 평균으로 채우는 연속 일수 한도
MIN_NORMAL_DAYS = 100  # 평년선의 유효 일수가 이보다 적으면 일사 미측정 지점 (정상 지점 최소 244일, 영주 1일)

# rs_source 값: 계산에 쓴 일사가 어디서 왔나 (없으면 None)
OBSERVED, SUNSHINE, TEMP_RANGE = "observed", "sunshine", "temp_range"


def rs_floor(lat: float, day: dt.date) -> float:
    """그날 일사 하한 [MJ/m²/d] = 0.01 × Ra"""
    return MIN_RS_RATIO * extra_radiation(lat, day.timetuple().tm_yday)


def _is_valid(rs: float | None, floor: float) -> bool:
    # NaN 은 어떤 비교도 거짓이라 여기서 걸러진다
    return rs is not None and rs >= floor


def clean_observed_rs(rs_by_day: Mapping[dt.date, float | None], lat: float) -> dict[dt.date, float | None]:
    """관측 일사 {날짜: 값 또는 None} → 규칙 1·2를 적용한 {날짜: 유효한 값 또는 None}.
    값이 없는 날(None)은 '자료'로 세지 않는다. 입력은 바꾸지 않는다."""
    floors = {d: rs_floor(lat, d) for d in rs_by_day}
    out = {d: (v if _is_valid(v, floors[d]) else None) for d, v in rs_by_day.items()}  # 규칙 1
    data: dict[tuple[int, int], int] = defaultdict(int)
    bad: dict[tuple[int, int], int] = defaultdict(int)
    for d, v in rs_by_day.items():
        if v is None:
            continue
        data[(d.year, d.month)] += 1
        if out[d] is None:
            bad[(d.year, d.month)] += 1
    dead = {
        ym
        for ym, n in data.items()
        if n >= MONTH_MIN_DAYS and bad[ym] / n >= MONTH_INVALID_RATIO  # 규칙 2
    }
    return {d: (None if (d.year, d.month) in dead else v) for d, v in out.items()}


def has_enough_normals(normal: Mapping[tuple[int, int], float]) -> bool:
    """규칙 5. 평년선 {(월, 일): 값}의 유효 일수가 100일 이상이면 일사를 재는 지점이다."""
    return len(normal) >= MIN_NORMAL_DAYS


def choose_rs(
    *,
    valid_rs: float | None,
    sunshine_h: float | None,
    tmax: float | None,
    tmin: float | None,
    lat: float,
    elev_m: float,
    day: dt.date,
    krs: float = KRS_INLAND,
) -> tuple[float | None, str | None]:
    """규칙 3. 유효한 일사 → 일조시간 → 기온차 → 비움. (일사, 출처)를 돌려준다."""
    if valid_rs is not None:
        return valid_rs, OBSERVED
    J = day.timetuple().tm_yday
    if sunshine_h is not None:
        return rs_from_sunshine(lat, J, sunshine_h), SUNSHINE
    if tmax is not None and tmin is not None:
        ra, rso = ra_rso(lat, elev_m, J)
        return float(rs_s1(tmax, tmin, ra, rso, krs)[0]), TEMP_RANGE
    return None, None
