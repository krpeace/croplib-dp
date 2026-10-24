"""관측 물수지와 토양수분 상태 판정 (DB `water_balance_daily` 한 줄).

원본: cropwater `fcst_wb.observed_wb`, `fcst_wb.soil_status` (feat/cycle02-forecast).
pandas 없이 날짜순 목록으로 같은 계산을 한다.

상태 판정 (데이터 계약 v1, 위에서부터 처음 맞는 것)
  dormant  휴면기   Kc ≤ 0 (작기 밖)
  wet      과습     관측 물수지 날이고 끝 Dr이 0 (0.05 mm 미만 = 0.1 mm 단위로 0)
  need     관수 필요 Dr ≥ RAW
  caution  주의     Dr ≥ 주의 기준 (기본 RAW × 0.5)
  normal   보통     그 밖
이 기준은 운영하면서 다듬는다. 바꾸면 core/__init__.py 의 CALC_VERSION 을 올린다.
"""

import datetime as dt
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from croplib_dp.core import CALC_VERSION
from croplib_dp.core.fao56 import kc_of_date, raw_mm, taw_mm, wb_step

STATUSES = ("need", "caution", "normal", "wet", "dormant")  # 데이터 계약 v1 상태 코드
CAUTION_RATIO = 0.5  # 주의 기준 = RAW × 0.5 (포장용수량과 RAW의 중간)
WET_ZERO_MM = 0.05  # 과습: 끝 Dr이 이보다 작으면 0으로 본다 (부동소수 오차 포함)


@dataclass(frozen=True)
class Soil:
    """토양·관수 조건. taw·raw는 FAO-56 식(82)(83)으로 구한 값(DB `scenarios`의 taw_mm, raw_mm)"""

    taw_mm: float
    raw_mm: float
    dr0_mm: float = 0.0  # 계산 첫날 전날 끝 고갈량 (0 = 포장용수량에서 출발)
    ea: float = 0.95  # 관수효율 (공급 관수량 × ea = 순관수량)

    @classmethod
    def from_soil(
        cls, fc: float, wp: float, zr_m: float, p: float, dr0_mm: float = 0.0, ea: float = 0.95
    ) -> "Soil":
        taw = taw_mm(fc, wp, zr_m)
        return cls(taw_mm=taw, raw_mm=raw_mm(p, taw), dr0_mm=dr0_mm, ea=ea)


@dataclass(frozen=True)
class CropParams:
    """작물·작형 조건 (DB `crops` + `crop_seasons` + `scenarios.season_start`)"""

    season_start: dt.date
    l_ini: int
    l_dev: int
    l_mid: int
    l_late: int
    kc_ini: float
    kc_mid: float
    kc_end: float

    def kc(self, day: dt.date) -> float:
        return kc_of_date(
            day.toordinal(),
            self.season_start.toordinal(),
            self.l_ini,
            self.l_dev,
            self.l_mid,
            self.l_late,
            self.kc_ini,
            self.kc_mid,
            self.kc_end,
        )


def _missing(x: float | None) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def soil_status(
    dr: float | None,
    raw: float,
    kc: float | None = None,
    observed: bool = True,
    caution_mm: float | None = None,
) -> str | None:
    """토양수분 상태 코드. 자료가 없으면 None.
    과습은 관측 물수지 날(observed=True)이고 Kc를 알 때만 판정한다. 예보로 진행한 날은 판정하지 않는다."""
    if _missing(dr):
        return None
    caution = raw * CAUTION_RATIO if caution_mm is None else caution_mm
    if not _missing(kc) and kc <= 0:
        return "dormant"
    if observed and not _missing(kc) and kc > 0 and dr < WET_ZERO_MM:
        return "wet"
    if dr >= raw:
        return "need"
    return "caution" if dr >= caution else "normal"


def observed_water_balance(
    days: Iterable[Mapping[str, Any]],
    crop: CropParams | None,
    soil: Soil,
    irrigation: Mapping[dt.date, float] | None = None,
    forecast_fill: Mapping[dt.date, tuple[float, float]] | None = None,
    end: dt.date | None = None,
) -> list[dict]:
    """날짜순 일 기상값(date, eto_mm, rain_mm, filled) → 날마다 관측 물수지.
    crop이 None이면 Kc = 1 (기준작물, cropwater_multi와 같음).
    irrigation: {날짜: 공급 관수량 mm}. 근권에는 × soil.ea 로 들어간다. 공개 조건은 관수 기록이 없다.
    forecast_fill: {날짜: (ETo, 기대 강수)} 관측 ETo가 없는 날에 쓸 그날 아침 발표 D+0 예보
                   (outlook.fill_from_forecast). 강수는 ASOS 행이 아예 없는 날에만 쓴다.
                   예보도 없으면 들어온 값(앞 7일 평균)을 쓴다.
    end: 마지막 날. 관측보다 뒤면 빈 날을 더해 예보로 채운다
         (관측이 늦게 올 때, cropwater fcst_wb.observed_wb).
    반환 열은 `water_balance_daily`와 같고, obs_row(ASOS 행이 있었나)를 더 붙인다.
    eto_src: 관측 / 예보(D+0 예보로 채움) / 없음(앞 7일 평균 또는 값 없음).
    filled = 채운 값에 기대는 날(채운 날부터 끝 Dr이 0이 되기 전까지)."""
    irrigation = irrigation or {}
    forecast_fill = forecast_fill or {}
    days = list(days)
    if end is not None:
        last = days[-1]["date"] if days else None
        day = (last + dt.timedelta(days=1)) if last is not None else end
        while day <= end:
            days.append(dict(date=day, eto_mm=None, rain_mm=None, filled=True, obs_row=False))
            day += dt.timedelta(days=1)
    rows: list[dict] = []
    dr = soil.dr0_mm
    tainted = False
    for d in days:
        day = d["date"]
        eto = d.get("eto_mm")
        rain = d.get("rain_mm")
        obs_row = bool(d["obs_row"]) if "obs_row" in d else rain is not None
        filled_day = bool(d.get("filled")) or _missing(eto) or not obs_row
        src = "없음" if filled_day else "관측"
        if filled_day and day in forecast_fill:
            eto, f_rain = forecast_fill[day]
            src = "예보"
            if not obs_row:
                rain = f_rain
        kc = 1.0 if crop is None else crop.kc(day)
        etc = 0.0 if _missing(eto) else kc * eto
        P = 0.0 if _missing(rain) else float(rain)
        i_net = float(irrigation.get(day, 0.0)) * soil.ea
        ks, etc_adj, dp, dr_new = wb_step(dr, P, etc, soil.taw_mm, soil.raw_mm, i_net)
        tainted = filled_day or (tainted and dr_new > 0)
        rows.append(
            dict(
                date=day,
                kc=kc,
                eto_mm=eto,
                eto_src=src,
                etc_mm=etc,
                rain_mm=rain,
                irr_net_mm=i_net,
                dr_prev_mm=dr,
                ks=ks,
                etc_adj_mm=etc_adj,
                dp_mm=dp,
                dr_mm=dr_new,
                status=soil_status(dr_new, soil.raw_mm, kc, observed=True),
                filled=tainted,
                obs_row=obs_row,
                calc_version=CALC_VERSION,
            )
        )
        dr = dr_new
    return rows
