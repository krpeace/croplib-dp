"""일사(Rs) 점검 계산 (이슈 #48, #28 S0-b). 순수 함수만 둔다: DB·API·파일을 모른다.

한 지점의 ASOS 일자료(지난 365일)로 다음을 구한다.
  1. 분류: 정상 / 0.0·무효 / 일사 없음·일조 있음 / 모두 없음 (기준은 유효·값·일조 일수 100일)
  2. 실측 kRs: 유효한 날의 Rs / (Ra × √(Tmax−Tmin)) (식50의 역산). 전체 중앙값·사분위 범위,
     계절별(3~5, 6~8, 9~11, 12~2월) 중앙값은 그 계절 유효일이 20일 이상일 때만 낸다
  3. ETo 오차: (가) 관측 일사로 구한 ETo 와 (나) 일사를 빼고 기온차식(kRs 0.16), (다) 같은 방식 kRs 0.19
     의 차이(mm/d). 지점별 평균 오차(편향)·평균 절대 오차(MAE), 상반기(1~6월)·하반기(7~12월) 따로
  4. 의심 일조: 일조 0.0시간 + 강수 없음 + 일사 무효·없음인 날 수 (규칙은 만들지 않고 개수만)
그리고 해안 경계 후보(거리·섬·고도)마다 "그 기준으로 kRs를 나눴을 때" 지점 평균 절대 오차를 비교한다.

ETo 세 가지는 새 식을 만들지 않고 `daily_weather()`를 세 번 부른다:
(가) 원자료 그대로, (나)·(다) 일사·일조를 비운 자료 + `StationMeta.krs`만 바꿈 (기온차식 경로).
"""

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import numpy as np

from croplib_dp.core.daily_weather import StationMeta, daily_weather, num
from croplib_dp.core.fao56 import extra_radiation
from croplib_dp.core.radiation import OBSERVED, TEMP_RANGE, clean_observed_rs, rs_floor

CLASS_NORMAL = "정상"
CLASS_ZERO_INVALID = "0.0·무효"
CLASS_NO_RS_SUNSHINE = "일사 없음·일조 있음"
CLASS_ALL_MISSING = "모두 없음"
CLASSES = (CLASS_NORMAL, CLASS_ZERO_INVALID, CLASS_NO_RS_SUNSHINE, CLASS_ALL_MISSING)

MIN_VALID_DAYS = 100  # 정상 지점·'값 있음'·'일조 있음' 기준 (S0-a 평년선 100일 기준과 같은 숫자)
MIN_SEASON_DAYS = 20  # 계절별 중앙값을 내는 최소 유효일
SEASONS = {"3~5": (3, 4, 5), "6~8": (6, 7, 8), "9~11": (9, 10, 11), "12~2": (12, 1, 2)}
KRS_LOW, KRS_HIGH = 0.16, 0.19  # 내륙·해안
DEFAULT_CANDIDATES_KM = (10, 20, 30, 50)  # 5 km 는 해안선 자료 해상도(수 km)와 비슷해 비교하지 않는다
DEFAULT_ELEV_CANDIDATES_M = (100, 200, 300)
MEANINGLESS_GAIN = 0.01  # 가장 좋은 기준이 "전부 0.16"보다 이만큼(mm/d) 못 좋으면 구분이 무의미


def _date(item: Mapping[str, Any]) -> dt.date:
    return dt.date.fromisoformat(str(item["tm"]).strip()[:10])


def _season(month: int) -> str:
    return next(k for k, months in SEASONS.items() if month in months)


def _stats(values: Sequence[float]) -> dict:
    a = np.asarray(values, float)
    return dict(
        n=len(a),
        median=float(np.median(a)),
        q1=float(np.percentile(a, 25)),
        q3=float(np.percentile(a, 75)),
    )


def _eto_errors(items: Sequence[Mapping[str, Any]], st: StationMeta) -> dict | None:
    """(가)·(나)·(다) ETo 오차. 비교하는 날: (가)가 관측 일사, (나)·(다)가 기온차식, 셋 다 채움이 아닌 날."""
    stripped = [{**it, "sumGsr": "", "sumSsHr": ""} for it in items]
    obs = {r["date"]: r for r in daily_weather(items, st)}
    low = {r["date"]: r for r in daily_weather(stripped, replace(st, krs=KRS_LOW))}
    high = {r["date"]: r for r in daily_weather(stripped, replace(st, krs=KRS_HIGH))}
    d16: list[tuple[int, float]] = []  # (월, 오차)
    d19: list[tuple[int, float]] = []
    for day, o in obs.items():
        a, b = low.get(day), high.get(day)
        if a is None or b is None:
            continue
        if o["rs_source"] != OBSERVED or o["filled"] or o["eto_mm"] is None:
            continue
        if any(x["rs_source"] != TEMP_RANGE or x["filled"] or x["eto_mm"] is None for x in (a, b)):
            continue
        d16.append((day.month, a["eto_mm"] - o["eto_mm"]))
        d19.append((day.month, b["eto_mm"] - o["eto_mm"]))
    if not d16:
        return None

    def agg(pairs, month_ok=lambda m: True):
        v = [e for m, e in pairs if month_ok(m)]
        return (sum(v) / len(v), sum(abs(e) for e in v) / len(v)) if v else (None, None)

    def block(month_ok):
        n = sum(1 for m, _ in d16 if month_ok(m))
        return dict(n=n, mae_016=agg(d16, month_ok)[1], mae_019=agg(d19, month_ok)[1])

    b16, m16 = agg(d16)
    b19, m19 = agg(d19)
    return dict(
        n=len(d16),
        bias_016=b16,
        mae_016=m16,
        bias_019=b19,
        mae_019=m19,
        best_krs=KRS_HIGH if m19 < m16 else KRS_LOW,
        h1=block(lambda m: m <= 6),
        h2=block(lambda m: m >= 7),
    )


def audit_station(items: Sequence[Mapping[str, Any]], st: StationMeta) -> dict:
    """한 지점의 ASOS 일자료 item 목록 → 점검 결과(분류·일수·실측 kRs·ETo 오차·의심 일조)"""
    by_day: dict[dt.date, Mapping[str, Any]] = {}
    for it in items:
        by_day[_date(it)] = it  # 같은 날이 두 번 오면 마지막 값
    rs_raw = {d: num(it.get("sumGsr")) for d, it in by_day.items()}
    clean = clean_observed_rs(rs_raw, st.lat)

    zero = below = month_invalid = valid = 0
    for d, v in rs_raw.items():
        if v is None:
            continue
        if clean[d] is not None:
            valid += 1
        elif v == 0.0:
            zero += 1
        elif not v >= rs_floor(st.lat, d):  # 하한 미만(음수·NaN 포함)
            below += 1
        else:
            month_invalid += 1  # 하루 값은 유효했으나 그 달이 월 규칙으로 무효
    values = valid + zero + below + month_invalid
    ss_days = sum(1 for it in by_day.values() if num(it.get("sumSsHr")) is not None)
    temp_days = sum(
        1 for it in by_day.values() if num(it.get("maxTa")) is not None and num(it.get("minTa")) is not None
    )
    suspect = sum(
        1
        for d, it in by_day.items()
        if num(it.get("sumSsHr")) == 0.0 and (num(it.get("sumRn")) or 0.0) == 0.0 and clean[d] is None
    )

    if valid >= MIN_VALID_DAYS:
        klass = CLASS_NORMAL
    elif values >= MIN_VALID_DAYS:
        klass = CLASS_ZERO_INVALID
    elif ss_days >= MIN_VALID_DAYS:
        klass = CLASS_NO_RS_SUNSHINE
    else:
        klass = CLASS_ALL_MISSING

    out = dict(
        days_total=len(by_day),
        rs_values=values,
        rs_zero=zero,
        rs_below_floor=below,
        rs_month_invalid=month_invalid,
        rs_valid=valid,
        rs_sparse=0 < values < MIN_VALID_DAYS,
        ss_days=ss_days,
        temp_days=temp_days,
        suspect_sunshine_days=suspect,
        klass=klass,
        krs_n=0,
        krs_median=None,
        krs_q1=None,
        krs_q3=None,
        krs_season={k: None for k in SEASONS},
        eto=None,
    )
    if klass != CLASS_NORMAL:
        return out

    krs: list[tuple[int, float]] = []
    for d, rs in clean.items():
        if rs is None:
            continue
        tmax, tmin = num(by_day[d].get("maxTa")), num(by_day[d].get("minTa"))
        if tmax is None or tmin is None or tmax <= tmin:
            continue
        krs.append((d.month, rs / (extra_radiation(st.lat, d.timetuple().tm_yday) * math.sqrt(tmax - tmin))))
    if krs:
        s = _stats([k for _, k in krs])
        out.update(krs_n=s["n"], krs_median=s["median"], krs_q1=s["q1"], krs_q3=s["q3"])
        seasonal = defaultdict(list)
        for m, k in krs:
            seasonal[_season(m)].append(k)
        out["krs_season"] = {
            k: (float(np.median(seasonal[k])) if len(seasonal[k]) >= MIN_SEASON_DAYS else None)
            for k in SEASONS
        }
    out["eto"] = _eto_errors(items, st)
    return out


# ── 경계 후보 비교 ────────────────────────────────────────────────────────
def _criteria(candidates: Sequence[float], elev_candidates: Sequence[float]):
    """(이름, 해안(kRs 0.19) 여부 판정 함수) 목록. 거리 경계는 '이하'(같으면 해안)."""
    rows = [("전부 0.16", lambda s: False), ("전부 0.19", lambda s: True)]
    rows += [(f"거리 ≤ {d:g} km", lambda s, d=d: s["coast_km"] <= d) for d in candidates]
    rows.append(("섬만", lambda s: bool(s.get("island"))))
    rows += [
        (f"거리 ≤ {d:g} km 또는 섬", lambda s, d=d: s["coast_km"] <= d or bool(s.get("island")))
        for d in candidates
    ]
    rows += [(f"고도 ≤ {h:g} m", lambda s, h=h: s["elev_m"] <= h) for h in elev_candidates]
    return rows


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _row(label: str, picks: list[tuple[Mapping[str, Any], bool]]) -> dict:
    """picks: (지점, kRs 0.19를 쓰나). 지점 동일 가중 평균과 일수 가중 평균, 반기별 평균."""

    def val(e, high):
        return e["mae_019" if high else "mae_016"]

    whole = [(val(s["eto"], h), s["eto"]["n"]) for s, h in picks]
    halves = {}
    for half in ("h1", "h2"):
        halves[half] = [
            val(s["eto"][half], h)
            for s, h in picks
            if s["eto"][half]["n"] and val(s["eto"][half], h) is not None
        ]
    total_n = sum(n for _, n in whole)
    return dict(
        label=label,
        n=len(picks),
        n_high=sum(1 for _, h in picks if h),
        mean_mae=_mean([v for v, _ in whole]),
        weighted_mae=sum(v * n for v, n in whole) / total_n if total_n else None,
        mean_mae_h1=_mean(halves["h1"]),
        mean_mae_h2=_mean(halves["h2"]),
    )


def compare_boundaries(
    stations: Sequence[Mapping[str, Any]],
    candidates: Sequence[float] = DEFAULT_CANDIDATES_KM,
    elev_candidates: Sequence[float] = DEFAULT_ELEV_CANDIDATES_M,
) -> dict:
    """정상 지점(eto 있음)과 해안선 거리(coast_km 있음)로, 기준마다 "kRs 를 그렇게 나눴을 때" 오차를 비교한다.
    stations 항목: stn_id, coast_km(km), island(bool), elev_m, eto(audit_station 의 eto 칸).
    반환: rows(기준별 지점 수·평균 절대 오차(지점 동일 가중·일수 가중)·반기별), best(가장 작은 기준),
    best_h1·best_h2(반기별 최선), meaningless(가장 좋은 기준이 '전부 0.16'과 0.01 mm/d 미만 차이)."""
    use = [s for s in stations if s.get("eto") and s.get("coast_km") is not None]
    rows = [
        _row(label, [(s, bool(is_high(s))) for s in use])
        for label, is_high in _criteria(candidates, elev_candidates)
    ]
    oracle = []
    for s in use:
        e = s["eto"]
        oracle.append((s, e["mae_019"] < e["mae_016"]))
    # 이론값은 지점별로 오차가 작은 쪽을 고른다(반기도 따로 고름). 기준이 아니라 하한을 보이는 참고 줄이다.
    o = _row("지점별 최적(이론값)", oracle)
    for half, key in (("h1", "mean_mae_h1"), ("h2", "mean_mae_h2")):
        v = [
            min(x for x in (s["eto"][half]["mae_016"], s["eto"][half]["mae_019"]) if x is not None)
            for s in use
            if s["eto"][half]["n"]
        ]
        o[key] = _mean(v)
    rows.append(o)

    def pick(key):
        best = None
        for r in rows[:-1]:
            if r[key] is not None and (best is None or r[key] < best[key] - 1e-12):
                best = r
        return best

    base = next(r for r in rows if r["label"] == "전부 0.16")
    b, b1, b2 = pick("mean_mae"), pick("mean_mae_h1"), pick("mean_mae_h2")
    gain = (base["mean_mae"] - b["mean_mae"]) if b and base["mean_mae"] is not None else None
    return dict(
        n_stations=len(use),
        rows=rows,
        best=dict(label=b["label"], mean_mae=b["mean_mae"], gain_vs_all_016=gain) if b else None,
        best_h1=dict(label=b1["label"], mean_mae=b1["mean_mae_h1"]) if b1 else None,
        best_h2=dict(label=b2["label"], mean_mae=b2["mean_mae_h2"]) if b2 else None,
        meaningless=bool(gain is not None and gain < MEANINGLESS_GAIN),
    )
