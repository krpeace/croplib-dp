"""ASOS 일자료 → 일 기상값과 ETo (DB `weather_daily` 한 줄).

규칙은 cropwater `cropwater_multi.compute_station`과 `compute_water_balance`(결측 채우기)를 그대로 따른다.
  - 실제 수증기압 ea: 증기압(avgPv) → 이슬점(avgTd) → 평균습도(avgRhm) 순으로 있는 값을 쓴다
  - 일사 Rs: 관측 일사(sumGsr) → 없으면 일조시간(sumSsHr)으로 추정(FAO-56 식35)
  - 기압: 관측 현지기압(avgPa), 없으면 고도로 추정
  - 강수: ASOS는 비가 오지 않은 날 일강수 칸이 비어 있으므로 0. 행 자체가 없는 날은 비워 둔다(None)
  - 첫날~끝날 사이에 빠진 날짜는 행을 넣는다. ETo를 구하지 못한 날은 앞 7일 ETo 평균으로 채우고 filled = True
입력은 ASOS 응답의 item(문자열 값) 목록이다. 조회는 io/에서 한다.
"""

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from croplib_dp.core.fao56 import (
    FILL_DAYS,
    eto_penman_monteith,
    missing_dates,
    prev_mean,
    rs_from_sunshine,
    svp,
    wind_2m,
)


@dataclass(frozen=True)
class StationMeta:
    """지점 정보 (DB `stations`): 위도, 고도(m), 풍속계 높이(m)"""

    lat: float
    elev_m: float
    wind_height_m: float = 10.0


def num(x: Any) -> float | None:
    """문자열·None을 숫자로. 결측 표기('', '-', 'null', 'None')는 None."""
    try:
        s = str(x).strip()
        return float(s) if s not in ("", "-", "null", "None") else None
    except (TypeError, ValueError):
        return None


def _parse_date(tm: str) -> dt.date:
    return dt.date.fromisoformat(str(tm).strip()[:10])


def _one_day(item: Mapping[str, Any], st: StationMeta) -> dict:
    day = _parse_date(item["tm"])
    J = day.timetuple().tm_yday
    tmax, tmin = num(item.get("maxTa")), num(item.get("minTa"))
    rh, ws = num(item.get("avgRhm")), num(item.get("avgWs"))
    pv, td, pa = num(item.get("avgPv")), num(item.get("avgTd")), num(item.get("avgPa"))
    gsr, ss = num(item.get("sumGsr")), num(item.get("sumSsHr"))
    rain = num(item.get("sumRn")) or 0.0

    u2 = wind_2m(ws, st.wind_height_m or 10.0) if ws is not None else None
    if gsr is not None:
        rs = gsr
    elif ss is not None:
        rs = rs_from_sunshine(st.lat, J, ss)
    else:
        rs = None

    eto = None
    if tmax is not None and tmin is not None and u2 is not None:
        es = (svp(tmax) + svp(tmin)) / 2
        if pv is not None:
            ea = pv * 0.1
        elif td is not None:
            ea = svp(td)
        elif rh:
            ea = rh / 100 * es
        else:
            ea = None
        if rs is not None and ea is not None:
            eto = eto_penman_monteith(tmax, tmin, rs, u2, ea, st.elev_m, st.lat, J, pa)

    return dict(
        date=day,
        tmax_c=tmax,
        tmin_c=tmin,
        tavg_c=num(item.get("avgTa")),
        rh_mean_pct=rh,
        rh_min_pct=num(item.get("minRhm")),
        wind_ms=ws,
        u2_ms=u2,
        sunshine_h=ss,
        rs_mj=rs,
        rain_mm=rain,
        eto_mm=eto,
        filled=False,
    )


def _empty_day(day: dt.date) -> dict:
    """ASOS 행이 없는 날: 값은 모두 비우고 ETo만 채운다"""
    return dict(
        date=day,
        tmax_c=None,
        tmin_c=None,
        tavg_c=None,
        rh_mean_pct=None,
        rh_min_pct=None,
        wind_ms=None,
        u2_ms=None,
        sunshine_h=None,
        rs_mj=None,
        rain_mm=None,
        eto_mm=None,
        filled=False,
    )


def daily_weather(items: Iterable[Mapping[str, Any]], station: StationMeta) -> list[dict]:
    """ASOS 일자료 item 목록 → 날짜순 일 기상값 목록 (빠진 날짜 포함, ETo 결측은 앞 7일 평균으로 채움)"""
    by_day: dict[dt.date, dict] = {}
    for it in items:
        row = _one_day(it, station)
        by_day[row["date"]] = row  # 같은 날이 두 번 오면 마지막 값
    for day in missing_dates(list(by_day)):
        by_day[day] = _empty_day(day)

    out = [by_day[d] for d in sorted(by_day)]
    used: list[float] = []  # 앞 7일 평균에 쓰는 ETo (채운 값 포함, cropwater와 같음)
    for row in out:
        if row["eto_mm"] is None:
            mean = prev_mean(used, len(used), FILL_DAYS)
            row["eto_mm"] = mean if mean is not None else 0.0
            row["filled"] = True
        used.append(row["eto_mm"])
    return out
