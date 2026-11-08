"""ASOS 일자료 → 일 기상값과 ETo (DB `weather_daily` 한 줄).

규칙은 cropwater `cropwater_multi.compute_station`과 `compute_water_balance`(결측 채우기)를 그대로 따른다.
  - 실제 수증기압 ea: 증기압(avgPv) → 이슬점(avgTd) → 평균습도(avgRhm) 순으로 있는 값을 쓴다
  - 일사 Rs: 유효한 관측 일사(sumGsr) → 일조시간(sumSsHr, FAO-56 식35) → 기온차(식50) → 없음.
    `0.0`처럼 하한(0.02×Ra)보다 작은 값과, 한 달 대부분이 무효인 달의 값은 관측으로 쓰지 않는다
    (core/radiation.py, 결정 0020)
  - 기압: 관측 현지기압(avgPa), 없으면 고도로 추정
  - 강수: ASOS는 비가 오지 않은 날 일강수 칸이 비어 있으므로 0. 행 자체가 없는 날은 비워 둔다(None)
  - 첫날~끝날 사이에 빠진 날짜는 행을 넣는다. ETo를 구하지 못한 날은 앞 7일 ETo 평균으로 채우고 filled = True.
    채운 날이 연속 7일을 넘으면 8일째부터 ETo를 비운다(None, filled = True)
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
    svp,
    wind_2m,
)
from croplib_dp.core.radiation import KRS_INLAND, MAX_FILL_DAYS, choose_rs, clean_observed_rs


@dataclass(frozen=True)
class StationMeta:
    """지점 정보 (DB `stations`): 위도, 고도(m), 풍속계 높이(m).
    krs: 기온차로 일사를 추정할 때(식50)의 계수. 기본 0.16(내륙), 해안은 0.19 (지점별 연결은 S0-b)"""

    lat: float
    elev_m: float
    wind_height_m: float = 10.0
    krs: float = KRS_INLAND


def num(x: Any) -> float | None:
    """문자열·None을 숫자로. 결측 표기('', '-', 'null', 'None')는 None."""
    try:
        s = str(x).strip()
        return float(s) if s not in ("", "-", "null", "None") else None
    except (TypeError, ValueError):
        return None


def _parse_date(tm: str) -> dt.date:
    return dt.date.fromisoformat(str(tm).strip()[:10])


def _one_day(item: Mapping[str, Any], st: StationMeta, valid_rs: float | None) -> dict:
    day = _parse_date(item["tm"])
    J = day.timetuple().tm_yday
    tmax, tmin = num(item.get("maxTa")), num(item.get("minTa"))
    rh, ws = num(item.get("avgRhm")), num(item.get("avgWs"))
    pv, td, pa = num(item.get("avgPv")), num(item.get("avgTd")), num(item.get("avgPa"))
    ss = num(item.get("sumSsHr"))
    rain = num(item.get("sumRn")) or 0.0

    u2 = wind_2m(ws, st.wind_height_m or 10.0) if ws is not None else None
    rs, rs_source = choose_rs(
        valid_rs=valid_rs,
        sunshine_h=ss,
        tmax=tmax,
        tmin=tmin,
        lat=st.lat,
        elev_m=st.elev_m,
        day=day,
        krs=st.krs,
    )

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
        rs_source=rs_source,
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
        rs_source=None,
        rain_mm=None,
        eto_mm=None,
        filled=False,
    )


def daily_weather(items: Iterable[Mapping[str, Any]], station: StationMeta) -> list[dict]:
    """ASOS 일자료 item 목록 → 날짜순 일 기상값 목록 (빠진 날짜 포함).
    ETo 결측은 앞 7일 평균으로 채우되 연속 7일까지만 채우고, 8일째부터는 비운다(None, filled = True)."""
    raw: dict[dt.date, Mapping[str, Any]] = {}
    for it in items:
        raw[_parse_date(it["tm"])] = it  # 같은 날이 두 번 오면 마지막 값
    # 지점의 일사는 달력 월 단위 규칙까지 보고 걸러야 해서, 날마다 계산하기 전에 한꺼번에 정리한다
    valid_rs = clean_observed_rs({d: num(it.get("sumGsr")) for d, it in raw.items()}, station.lat)
    by_day = {d: _one_day(it, station, valid_rs[d]) for d, it in raw.items()}
    for day in missing_dates(list(by_day)):
        by_day[day] = _empty_day(day)

    out = [by_day[d] for d in sorted(by_day)]
    used: list[float | None] = []  # 앞 7일 평균에 쓰는 ETo (채운 값 포함, 비운 날은 None. cropwater와 같음)
    run = 0  # 지금까지 이어서 채운 날 수
    for row in out:
        if row["eto_mm"] is None:
            row["filled"] = True
            if run < MAX_FILL_DAYS:
                mean = prev_mean(used, len(used), FILL_DAYS)
                row["eto_mm"] = mean if mean is not None else 0.0
                run += 1
        else:
            run = 0
        used.append(row["eto_mm"])
    return out
