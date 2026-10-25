"""적산온도(GDD)·적산일사 (CropMon 계약 v1.4 heat_daily·sun_daily).

원본: cropweather `temp_core.py`(calc_gdd, gdd_reaching_dates). 식은 그대로 옮겼다.
CropMon 표를 위해 더한 것: 같은 날 평균으로 구한 평년(normals_by_day),
1월 1일 ~ 12월 31일 한 해 표(year_table), 평년보다 며칠 빠른가(lead_days).
DB·API·파일을 모르는 순수 함수만 둔다.
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Mapping, Sequence

DEFAULT_T_BASE = 10.0
DEFAULT_T_UPPER = 35.0
DEFAULT_METHOD = "maxmin"
DEFAULT_MILESTONES = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000]
EPS = 1e-9


def calc_gdd(avg_ta, max_ta, min_ta, t_base=DEFAULT_T_BASE, t_upper=DEFAULT_T_UPPER, method=DEFAULT_METHOD):
    """하루 GDD (cropweather temp_core.calc_gdd 그대로). 입력이 비면 None.
    maxmin = max((최고+최저)/2 − 기준, 0) (기본, FAO56rev),
    avg = max(평균 − 기준, 0), aquacrop = 상한 반영."""
    if method == "maxmin":
        if max_ta is None or min_ta is None:
            return None
        return max((max_ta + min_ta) / 2.0 - t_base, 0.0)
    if method == "avg":
        if avg_ta is None:
            return None
        return max(avg_ta - t_base, 0.0)
    if method == "aquacrop":
        if avg_ta is None or max_ta is None or min_ta is None:
            return None
        if avg_ta < t_base:
            return 0.0
        if avg_ta > t_upper:
            return t_upper - t_base
        return max((max_ta + min_ta) / 2.0 - t_base, 0.0)
    raise ValueError(f"알 수 없는 GDD 계산 방법: {method!r} ('maxmin', 'avg', 'aquacrop' 중 선택)")


def gdd_reaching_dates(
    rows,
    t_base=DEFAULT_T_BASE,
    t_upper=DEFAULT_T_UPPER,
    method=DEFAULT_METHOD,
    milestones=None,
    start_month=1,
    start_day=1,
):
    """연도별 적산온도 도달일 {year: {단계: "M-D" 또는 None}} (cropweather 그대로).
    rows: year·month·day·avg_ta·max_ta·min_ta (gdd 가 있으면 그 값을 쓴다)."""
    milestones = sorted(milestones or DEFAULT_MILESTONES)
    by_year = defaultdict(list)
    for r in rows:
        by_year[r["year"]].append(r)
    out = {}
    for year, yr in sorted(by_year.items()):
        yr.sort(key=lambda x: (x["month"], x["day"]))
        cum, reached, left = 0.0, {}, list(milestones)
        for r in yr:
            if (r["month"], r["day"]) < (start_month, start_day):
                continue
            g = r.get("gdd")
            if g is None:
                g = calc_gdd(r.get("avg_ta"), r.get("max_ta"), r.get("min_ta"), t_base, t_upper, method)
            if g is None:
                continue
            cum += g
            while left and cum >= left[0]:
                reached[left.pop(0)] = f"{r['month']}-{r['day']}"
        for ms in milestones:
            reached.setdefault(ms, None)
        out[year] = reached
    return out


def normals_by_day(values: Mapping[dt.date, float | None], year_from: int, year_to: int) -> dict:
    """평년: 평년 기간(year_from ~ year_to) 같은 날(월, 일) 값의 평균. 결측은 빼고 평균한다.
    2월 29일은 윤년 값으로만 낸다."""
    acc: dict[tuple[int, int], list[float]] = defaultdict(list)
    for d, v in values.items():
        if v is not None and year_from <= d.year <= year_to:
            acc[(d.month, d.day)].append(float(v))
    return {k: sum(v) / len(v) for k, v in acc.items()}


def lead_days(cum: float | None, normal_cum: Sequence[float], i: int) -> int | None:
    """평년보다 며칠 빠른가 (+ 빠름, − 늦음). 올해 i번째 날 누적(cum)에 평년 누적이 처음 닿는 날 − i.
    평년이 그해 안에 닿지 못하면 None."""
    if cum is None:
        return None
    if abs(normal_cum[i] - cum) < EPS:
        return 0
    for j, n in enumerate(normal_cum):
        if n >= cum - EPS:
            return j - i
    return None


def year_table(
    year: int,
    obs: Mapping[dt.date, float | None],
    normal: Mapping[tuple[int, int], float],
    last_obs: dt.date | None,
) -> list[dict]:
    """그해 1월 1일 ~ 12월 31일 한 줄씩:
    day(그날 값), cum(1월 1일부터 합), normal_day, normal_cum, filled, lead.
    last_obs 까지는 올해 값을 쌓고, 결측 날은 평년 값으로 채워 filled = True. 그 뒤 날은 올해 값이 비어 있다.
    평년 값이 없는 날(예: 평년 기간에 그 날 자료가 하나도 없음)은 0으로 쌓는다."""
    days = [
        dt.date(year, 1, 1) + dt.timedelta(days=k)
        for k in range((dt.date(year, 12, 31).toordinal() - dt.date(year, 1, 1).toordinal()) + 1)
    ]
    ncum, s = [], 0.0
    for d in days:
        s += normal.get((d.month, d.day), 0.0)
        ncum.append(s)
    rows, cum = [], 0.0
    for i, d in enumerate(days):
        nd = normal.get((d.month, d.day))
        row = dict(date=d, day=None, cum=None, normal_day=nd, normal_cum=ncum[i], filled=False, lead=None)
        if last_obs is not None and d <= last_obs:
            v = obs.get(d)
            if v is None:
                v, row["filled"] = (nd if nd is not None else 0.0), True
            cum += v
            row.update(day=v, cum=cum, lead=lead_days(cum, ncum, i))
        rows.append(row)
    return rows
