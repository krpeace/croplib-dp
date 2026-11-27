"""적산온도(GDD)·적산일사 (CropMon 계약 v2.5 heat_series_daily·sun_series_daily).

원본: cropweather `temp_core.py`(calc_gdd, gdd_reaching_dates). 식은 그대로 옮겼다.
CropMon 표를 위해 더한 것: 같은 날 평균으로 구한 평년(normals_by_day),
1월 1일 ~ 12월 31일 한 해 표(year_table), 평년보다 며칠 빠른가(lead_days).
DB·API·파일을 모르는 순수 함수만 둔다.
"""

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from croplib_dp.core.daily_weather import num

DEFAULT_T_BASE = 10.0
DEFAULT_T_UPPER = 35.0
DEFAULT_METHOD = "maxmin"
DEFAULT_MILESTONES = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000]
EPS = 1e-9


def calc_gdd(avg_ta, max_ta, min_ta, t_base=DEFAULT_T_BASE, t_upper=DEFAULT_T_UPPER, method=DEFAULT_METHOD):
    """하루 GDD (cropweather temp_core.calc_gdd 그대로). 입력이 비면 None.
    maxmin = max((최고+최저)/2 − 기준, 0) (기본, 기본형: McMaster & Wilhelm 1997 Method 1, 상한 없음),
    avg = max(평균 − 기준, 0),
    aquacrop = 일평균으로 판정해 상한 반영 (FAO56rev Paredes 2025 Eq. 2와 같은 꼴.
    판정에는 기상청 일평균기온을 쓴다)."""
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


# ── 적산 계열 (이슈 #127, 결정 0038) ─────────────────────────────────────────
# 적산온도는 작목 기준온도와 기상청 기온만으로 계산한다. 값을 가르는 것은 작물이 아니라 기준온도와
# 누적 시작 월-일 둘뿐이라, 이 짝을 "적산 계열"이라 부른다. 계열 목록은 DB 뷰 crop_accum_series 에서 읽는다.
VIEW = "crop_accum_series"


def parse_mmdd(s: Any) -> tuple[int, int]:
    """'MM-DD' 글자 → (월, 일). 형식이 틀리거나 없는 날짜, 윤일(02-29)이면 ValueError.
    02-29 는 평년에 없는 날이라 누적 시작일로 쓸 수 없다."""
    if not isinstance(s, str) or not re.fullmatch(r"\d{2}-\d{2}", s):
        raise ValueError(f"누적 시작일 형식이 'MM-DD'가 아닙니다: {s!r}")
    month, day = int(s[:2]), int(s[3:])
    if (month, day) == (2, 29):
        raise ValueError("누적 시작일은 02-29 로 둘 수 없습니다 (평년에 없는 날)")
    try:
        dt.date(2001, month, day)  # 평년 기준으로 있는 날인지
    except ValueError:
        raise ValueError(f"없는 날짜입니다: {s!r}") from None
    return month, day


def _t_base_number(v: Any) -> Any:
    """뷰의 기준온도 → JSON 으로 보낼 수 있는 숫자. int·float 은 받은 그대로, Decimal·글자는 숫자로."""
    if isinstance(v, bool):
        raise ValueError(f"기준온도가 숫자가 아닙니다: {v!r}")
    if isinstance(v, int | float):
        return v
    if isinstance(v, Decimal):
        return float(v)
    return float(str(v))


def t_base_key(v: Any) -> Decimal:
    """기준온도를 묶고 비교할 때 쓰는 열쇠(Decimal). 4 · 4.0 · '4.00' 은 같은 열쇠다."""
    return Decimal(str(_t_base_number(v))).normalize()


def group_heat_series(rows: Iterable[Mapping]) -> tuple[list[tuple[Any, str]], list[str]]:
    """뷰 줄 → ([(기준온도, 'MM-DD')] 중복 없이 기준온도·시작일 순, 문제 목록).
    기준온도와 시작일이 둘 다 있는 줄만 적산온도 계열이다(시설가온은 둘 다 비어 있어 제외).
    같은 기준온도는 Decimal 로 비교해 한 계열로 묶고, 보낼 값은 처음 받은 값 그대로 둔다.
    쓸 수 없는 줄(형식이 틀린 시작일 · 숫자가 아닌 기준온도)은 건너뛰고 문제로 알린다."""
    found: dict[tuple[Decimal, str], Any] = {}
    problems: list[str] = []
    for r in rows:
        tb, start = r.get("heat_t_base_c"), r.get("heat_cum_start_mmdd")
        if tb is None or start is None:
            continue
        who = f"{r.get('crop_id')}/{r.get('season_type_id')}"
        try:
            parse_mmdd(start)
            value = _t_base_number(tb)
            key = (t_base_key(tb), start)
        except (ValueError, ArithmeticError) as e:
            problems.append(
                f"뷰 {VIEW} {who}: 적산온도 계열을 쓸 수 없음(기준온도 {tb!r}, 시작 {start!r}): {e}"
            )
            continue
        found.setdefault(key, value)
    return [(found[k], k[1]) for k in sorted(found)], problems


def group_sun_series(rows: Iterable[Mapping]) -> tuple[list[str], list[str]]:
    """뷰 줄 → (적산일사 누적 시작 'MM-DD' 중복 없이 순서대로, 문제 목록). 일사는 기준온도가 없다."""
    found: set[str] = set()
    problems: list[str] = []
    for r in rows:
        start = r.get("sun_cum_start_mmdd")
        if start is None:
            continue
        try:
            parse_mmdd(start)
        except ValueError as e:
            who = f"{r.get('crop_id')}/{r.get('season_type_id')}"
            problems.append(f"뷰 {VIEW} {who}: 적산일사 계열을 쓸 수 없음(시작 {start!r}): {e}")
            continue
        found.add(start)
    return sorted(found), problems


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
    start: tuple[int, int] = (1, 1),
) -> list[dict]:
    """누적 시작(start, 기본 1월 1일) ~ 12월 31일 한 줄씩:
    day(그날 값), cum(시작일부터 합), normal_day, normal_cum(시작일부터 합), filled, lead.
    last_obs 까지는 올해 값을 쌓고, 결측 날은 평년 값으로 채워 filled = True. 그 뒤 날은 올해 값이 비어 있다.
    평년 값이 없는 날(예: 평년 기간에 그 날 자료가 하나도 없음)은 0으로 쌓는다.
    시작일 앞 날은 줄을 만들지 않는다. 누적 · 평년 누적 · lead(며칠 빠른가)는 시작일부터 센다 (이슈 #127).
    일값(day · normal_day)은 시작이 달라도 같다."""
    first = dt.date(year, *start)
    days = [
        first + dt.timedelta(days=k)
        for k in range((dt.date(year, 12, 31).toordinal() - first.toordinal()) + 1)
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
