"""FAO-56 계산식 (기준 증발산량 ETo, 작물계수 Kc, 하루 근권 물수지).

원본: cropwater `fao56_core.py` (feat/cycle02-forecast). 식과 숫자는 그대로 두고 계산 부분만 옮겼다.
API 조회·CSV·엑셀 함수는 옮기지 않았다 (조회는 io/, 파일은 쓰지 않음).
식 번호는 FAO-56 (Allen et al., 1998) 기준이다.
"""

import datetime as dt
import math
from collections.abc import Sequence


# ── 기상 보조식 ─────────────────────────────────────────────────────────
def svp(T: float) -> float:
    """포화수증기압 e°(T) [kPa], 식(11)"""
    return 0.6108 * math.exp(17.27 * T / (T + 237.3))


def slope_svp(T: float) -> float:
    """포화수증기압곡선 기울기 Δ [kPa/℃], 식(13)"""
    return 4098 * svp(T) / (T + 237.3) ** 2


def _declination(J: int) -> float:
    return 0.409 * math.sin(2 * math.pi * J / 365 - 1.39)


def _sunset_angle(lat_deg: float, J: int) -> float:
    phi = math.radians(lat_deg)
    return math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(_declination(J)))))


def extra_radiation(lat_deg: float, J: int) -> float:
    """천문일사 Ra [MJ/m²/d], 식(21)"""
    Gsc = 0.0820
    phi = math.radians(lat_deg)
    dr = 1 + 0.033 * math.cos(2 * math.pi * J / 365)
    dec = _declination(J)
    ws = _sunset_angle(lat_deg, J)
    return (
        (24 * 60 / math.pi)
        * Gsc
        * dr
        * (ws * math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.sin(ws))
    )


def daylight_hours(lat_deg: float, J: int) -> float:
    """가조시간 N [h], 식(34)"""
    return 24 / math.pi * _sunset_angle(lat_deg, J)


def wind_2m(uz: float, zw: float = 10.0) -> float:
    """풍속을 z m → 2 m 높이로 환산, 식(47)"""
    return uz * 4.87 / math.log(67.8 * zw - 5.42)


def eto_penman_monteith(
    Tmax: float,
    Tmin: float,
    Rs: float,
    u2: float,
    ea: float,
    elev: float,
    lat: float,
    J: int,
    Pa: float | None = None,
) -> float:
    """기준 증발산량 ETo [mm/d], 식(6). 일 단위라 지중열류 G = 0.
    Pa: 현지기압(hPa). 없으면 고도로 표준대기압을 추정(식7)."""
    T = (Tmax + Tmin) / 2
    P = Pa * 0.1 if Pa else 101.3 * ((293 - 0.0065 * elev) / 293) ** 5.26  # kPa
    g = 0.000665 * P
    D = slope_svp(T)
    es = (svp(Tmax) + svp(Tmin)) / 2
    Ra = extra_radiation(lat, J)
    Rso = (0.75 + 2e-5 * elev) * Ra
    Rns = 0.77 * Rs
    Rnl = (
        4.903e-9
        * (((Tmax + 273.16) ** 4 + (Tmin + 273.16) ** 4) / 2)
        * (0.34 - 0.14 * math.sqrt(max(ea, 0)))
        * (1.35 * min(Rs / Rso, 1.0) - 0.35)
    )
    Rn = Rns - Rnl
    return (0.408 * D * Rn + g * (900 / (T + 273)) * u2 * (es - ea)) / (D + g * (1 + 0.34 * u2))


def rs_from_sunshine(lat_deg: float, J: int, sunshine_h: float) -> float:
    """일조시간으로 일사량 추정 [MJ/m²/d], 식(35) 앙스트롬 a = 0.25, b = 0.50"""
    return (0.25 + 0.5 * sunshine_h / daylight_hours(lat_deg, J)) * extra_radiation(lat_deg, J)


# ── 작물계수 Kc ─────────────────────────────────────────────────────────
def kc_climate_adjust(
    kc_table: float, u2_mean: float, rhmin_mean: float, h_m: float, is_end: bool = False
) -> float:
    """식(62)/(65) 기후보정. kc_table: 표값(Kc_mid 또는 Kc_end), u2·RHmin: 그 단계 평균,
    h_m: 중기 작물 높이(m).
    is_end=True면 표값이 0.45보다 클 때만 보정(식65). u2는 1~6, RHmin은 20~80으로 제한."""
    if is_end and kc_table <= 0.45:
        return kc_table
    u2c = max(1.0, min(6.0, u2_mean))
    rhc = max(20.0, min(80.0, rhmin_mean))
    return kc_table + (0.04 * (u2c - 2.0) - 0.004 * (rhc - 45.0)) * (h_m / 3.0) ** 0.3


def kc_of_date(
    day_ordinal: int,
    start_ordinal: int,
    L_ini: int,
    L_dev: int,
    L_mid: int,
    L_late: int,
    kc_ini: float,
    kc_mid: float,
    kc_end: float,
) -> float:
    """식(66) 일별 Kc 곡선. 초기: Kc_ini, 발육: Kc_ini→Kc_mid 선형, 중기: Kc_mid, 후기: Kc_mid→Kc_end 선형.
    작기 시작 전과 후기 이후는 0 (물을 쓰지 않음 = 휴면기)."""
    d = day_ordinal - start_ordinal
    if d < 0:
        return 0.0
    if d < L_ini:
        return kc_ini
    if d < L_ini + L_dev:
        return kc_ini + (kc_mid - kc_ini) * (d - L_ini + 1) / L_dev
    if d < L_ini + L_dev + L_mid:
        return kc_mid
    if d < L_ini + L_dev + L_mid + L_late:
        return kc_mid + (kc_end - kc_mid) * (d - L_ini - L_dev - L_mid + 1) / L_late
    return 0.0


# ── 근권 물수지 (8장) ───────────────────────────────────────────────────
def taw_mm(fc: float, wp: float, zr_m: float) -> float:
    """뿌리가 쓸 수 있는 물 TAW [mm] = 1000 × (FC − WP) × Zr, 식(82)"""
    return 1000 * (fc - wp) * zr_m


def raw_mm(p: float, taw: float) -> float:
    """스트레스 없이 쓸 수 있는 물 RAW [mm] = p × TAW, 식(83)"""
    return p * taw


def wb_step(
    dr_prev: float, P: float, etc: float, taw: float, raw: float, irr_net: float = 0.0
) -> tuple[float, float, float, float]:
    """하루 물수지 [식84·85·86·88]. 지표유출 RO = 0, 모관상승 CR = 0.
    dr_prev: 전날 끝 고갈량 Dr,i-1 (mm), P: 강수량 (mm), etc: 스트레스 보정 전 ETc = Kc × ETo (mm),
    irr_net: 근권에 들어간 순관수량 (mm) = 공급 관수량 × 관수효율.
    Ks는 전날 끝 고갈량으로 정한다(FAO-56 원식, cropwater VALIDATION #15).
    반환: (Ks, ETc_adj, DP, Dr,i)"""
    if dr_prev <= raw:
        ks = 1.0
    else:
        ks = max((taw - dr_prev) / (taw - raw), 0.0) if taw > raw else 0.0
    etc_adj = ks * etc
    dp = max(P + irr_net - etc_adj - dr_prev, 0.0)  # [식88] 원식
    dr = min(max(dr_prev - P - irr_net + etc_adj + dp, 0.0), taw)  # [식85·86]
    return ks, etc_adj, dp, dr


# ── 관측 결측일 (cropwater VALIDATION #20) ──────────────────────────────
FILL_DAYS = 7  # 빠진 날의 ETo = 앞 7일 ETo 평균


def missing_dates(dates: Sequence[dt.date]) -> list[dt.date]:
    """첫날~끝날 사이에 빠진 날짜(오름차순). 끝날 뒤(아직 발표 전인 날)는 넣지 않는다."""
    ds = sorted(set(dates))
    out: list[dt.date] = []
    for a, b in zip(ds, ds[1:], strict=False):
        out += [a + dt.timedelta(days=k) for k in range(1, (b - a).days)]
    return out


def prev_mean(vals: Sequence[float | None], i: int, n: int = FILL_DAYS) -> float | None:
    """vals[i] 앞 n개 값(None 제외)의 평균. 앞에 값이 없으면 None."""
    w = [v for v in vals[max(0, i - n) : i] if v is not None]
    return sum(w) / len(w) if w else None
