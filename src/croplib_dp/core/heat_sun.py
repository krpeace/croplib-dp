"""적산온도(GDD)·적산일사 (CropMon 계약 v1.4 heat_daily·sun_daily).

원본: cropweather `temp_core.py`(calc_gdd, gdd_reaching_dates). 식은 그대로 옮겼다.
CropMon 표를 위해 더한 것: 같은 날 평균으로 구한 평년(normals_by_day),
1월 1일 ~ 12월 31일 한 해 표(year_table), 평년보다 며칠 빠른가(lead_days).
DB·API·파일을 모르는 순수 함수만 둔다.
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Mapping, Sequence

from croplib_dp.core.daily_weather import num

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


# ── 빈 줄 날 · 이미 끝난 날 (이슈 #104, 결정 0033) ─────────────────────────────
# 기상청 일자료가 어떤 날을 날짜만 있는 줄(값이 모두 빔)로 돌려준 적이 있다(2026-11-10, 이슈 #100).
# 적산온도는 최고·최저기온으로 계산하므로 둘 중 하나라도 없으면 그 날은 계산할 수 없다. 그런 줄(빈 줄 포함)을
# 올리면 이미 올라간 옛 값을 비운 값(끝 날) 또는 평년 채움 값(가운데 날)으로 덮는다. 그래서 그 날 줄은
# 올리지 않는다.
# 기온이 있고 일사·강수·습도·풍속만 빈 날은 평소처럼 쓴다.
def is_blank_row(item: Mapping) -> bool:
    """ASOS 일자료 한 줄에서 적산온도를 계산할 기온(최고·최저)이 없다 (빈 줄 포함)"""
    return num(item.get("maxTa")) is None or num(item.get("minTa")) is None


def required_dates(today: dt.date) -> list[dt.date]:
    """오늘 실행이 채워야 하는 날: 어제. 1/2·1/3 에는 작년 12/31 마감도 함께 (1/1 의 어제가 곧 12/31)."""
    y = today - dt.timedelta(days=1)
    last_dec = dt.date(today.year - 1, 12, 31)
    if today.month == 1 and today.day <= 3 and y != last_dec:
        return [y, last_dec]
    return [y]


def pending_stations(
    targets: Sequence[str],
    dates: Sequence[dt.date],
    heat_have: set[tuple[str, dt.date]],
    sun_have: set[tuple[str, dt.date]],
) -> list[str]:
    """필요한 날 값을 아직 못 채운 지점. 비어 있어야 '이미 끝난 날'이다.
    - 적산온도: 지점마다 필요한 날마다 관측 줄(작물 공통)이 있어야 한다.
    - 적산일사: 그 전날 일사가 있던 지점만 그날도 있어야 한다 (일사를 재지 않는 지점은 기대하지 않는다)."""
    out = []
    for stn in targets:
        for d in dates:
            prev = d - dt.timedelta(days=1)
            if (stn, d) not in heat_have or ((stn, prev) in sun_have and (stn, d) not in sun_have):
                out.append(stn)
                break
    return out
