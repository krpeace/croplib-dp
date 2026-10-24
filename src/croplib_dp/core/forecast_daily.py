"""단기예보(동네예보) 항목 → 대상일별 하루 입력.

원본: cropwater `fcst_archive.py`(OpenAPI 경로)와 `kma_fcst.check_issue`, `kma_fcst.Store.store_issue`
(feat/cycle02-forecast). 기상자료개방포털 CSV 읽기는 서버에서 쓰지 않아 옮기지 않았다.

하는 일
  1) 발표 하나가 쓸 만한지 확인(필수 6요소 TMX·TMN·TMP·REH·WSD·PCP, 선택 2요소 SKY·POP의 행 수)
  2) 여러 발표의 항목을 모아 요소별 표로 만든다(같은 칸이 다시 오면 뒤의 값)
  3) 서비스 발표(02시 아침 D+0~D+3, 17시 저녁 D+1~D+4)마다 대상일의 하루 입력을 만든다.
     발표 시각 이전의 시각(예: 02시 발표의 00~02시)은 그보다 앞선 발표의 값으로 채운다.
  4) 서비스 발표를 쓸 수 없으면 24시간 안의 직전 발표로 대신한다(운영 규칙 #12)
입력은 API 응답의 item(문자열 값) 목록이다. 조회는 io/에서 한다.
"""

import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd

ELEMENTS = ("TMX", "TMN", "TMP", "REH", "WSD", "PCP")  # 필수 6요소
OPTIONAL = ("SKY", "POP")  # 선택: 하늘상태, 강수확률 (없으면 일사 추정을 S3로)
HOURLY = ("TMP", "REH", "WSD", "PCP", "SKY", "POP")
SKY_VALID = (1.0, 3.0, 4.0)  # 하늘상태 코드: 1 맑음, 3 구름많음, 4 흐림
WSD_CODE = {2: 6.5, 3: 11.0}  # 연장기간 풍속 코드. 코드 1은 직전 정량일 평균(최대 3.9)
WSD_CODE1_CAP = 3.9
PCP_CODE_MMH = {1: 1.5, 2: 9.0, 3: 20.0}  # 연장기간 강수 코드 → mm/h (3시간 칸)
RAIN_FLAG_MM = 1.0  # 강수유무: 하루 예보 강수 1 mm 이상
MISSING_ABS = 900.0  # 활용가이드: +900 이상 / −900 이하는 결측
PCP_LT1_MM = 0.5  # "1mm 미만"
PCP_30_50_MM = 40.0  # "30.0~50.0mm"
PCP_GE50_MM = 50.0  # "50.0mm 이상"
API_COLS = ("baseDate", "baseTime", "category", "fcstDate", "fcstTime", "fcstValue")
# 발표시각별 정상 행 수 (활용가이드). 1시간 요소 / 최고기온 / 최저기온
HOURLY_N = {2: 78, 5: 75, 8: 72, 11: 69, 14: 66, 17: 87, 20: 84, 23: 81}
TMX_N = {2: 4, 5: 4, 8: 4, 11: 4, 14: 3, 17: 4, 20: 4, 23: 4}
TMN_N = {2: 4, 5: 3, 8: 3, 11: 3, 14: 3, 17: 4, 20: 4, 23: 4}
SERVICE_RUNS = {"아침": (2, range(0, 4)), "저녁": (17, range(1, 5))}  # 발표시각, 대상일(D+k)
BACKUP_MAX_H = 24  # 직전 발표로 대신할 수 있는 범위(시간)


def _svp(t: float) -> float:
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


def pcp_mm(s: Any) -> float:
    """API 강수(PCP) 값 → mm/h. 읽을 수 없으면 NaN"""
    s = str(s).strip()
    if s in ("강수없음", "-", ""):
        return 0.0
    if "미만" in s:
        return PCP_LT1_MM
    if "~" in s:
        return PCP_30_50_MM
    if "이상" in s:
        return PCP_GE50_MM
    try:
        return float(s.replace("mm", ""))
    except ValueError:
        return float("nan")


# ── 받은 발표 확인 ─────────────────────────────────────────────────────
def items_frame(items: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    """API 항목 목록 → DataFrame[API_COLS] (값은 문자열 그대로, 같은 칸은 뒤의 값)"""
    items = list(items)
    if not items:
        return pd.DataFrame(columns=list(API_COLS))
    d = pd.DataFrame(items)
    for c in API_COLS:
        if c not in d:
            d[c] = ""
    d = d[list(API_COLS)].astype(str)
    d = d.assign(**{c: d[c].str.strip() for c in API_COLS})
    d["baseTime"] = d["baseTime"].str.zfill(4)
    d["fcstTime"] = d["fcstTime"].str.zfill(4)
    keys = ["baseDate", "baseTime", "category", "fcstDate", "fcstTime"]
    return d.drop_duplicates(keys, keep="last").reset_index(drop=True)


def _valid(cat: str, s: pd.Series) -> pd.Series:
    """값 문자열이 쓸 수 있는 값인지(결측 ±900, 하늘상태 코드표 밖 값은 빠진 칸)"""
    v = s.map(pcp_mm) if cat == "PCP" else pd.to_numeric(s, errors="coerce")
    ok = v.notna() & (v.abs() < MISSING_ABS)
    if cat == "SKY":
        ok &= v.round(6).isin(SKY_VALID)
    return ok


def check_issue(df: pd.DataFrame, issue: Any) -> dict:
    """받은 발표 1개의 완결성.
    반환 dict(required_ok, optional_ok, complete8, counts{요소: 유효 행 수}, expected, missing[문구])"""
    issue = pd.Timestamp(issue)
    h = issue.hour
    d = df[(df.baseDate == f"{issue:%Y%m%d}") & (df.baseTime == f"{issue:%H}00")]
    counts, missing = {}, []
    exp = {e: HOURLY_N.get(h, 1) for e in HOURLY}
    exp.update(TMX=TMX_N.get(h, 1), TMN=TMN_N.get(h, 1))
    for cat in ELEMENTS + OPTIONAL:
        s = d.loc[d.category == cat, "fcstValue"]
        n = int(_valid(cat, s).sum()) if len(s) else 0
        counts[cat] = n
        if n < exp[cat]:
            missing.append(f"{cat} {n}/{exp[cat]}")
    req = all(counts[e] >= exp[e] for e in ELEMENTS)
    opt = all(counts[e] >= exp[e] for e in OPTIONAL)
    return dict(
        required_ok=req, optional_ok=opt, complete8=req and opt, counts=counts, expected=exp, missing=missing
    )


def usable_items(df: pd.DataFrame, issue: Any) -> pd.DataFrame:
    """계산에 넣을 발표 1개의 항목. 필수 6요소가 덜 찼으면 빈 표, 선택 2요소가 덜 찼으면 두 요소는 뺀다(→ S3).
    (cropwater kma_fcst.Store.store_issue 규칙)"""
    issue = pd.Timestamp(issue)
    chk = check_issue(df, issue)
    d = df[(df.baseDate == f"{issue:%Y%m%d}") & (df.baseTime == f"{issue:%H}00")]
    if not chk["required_ok"]:
        return d.iloc[0:0]
    if not chk["optional_ok"]:
        d = d[~d.category.isin(OPTIONAL)]
    return d


# ── 요소별 표 ───────────────────────────────────────────────────────────
def _hourly_targets(df: pd.DataFrame) -> pd.DataFrame:
    out = df.rename(columns={"forecast": "lead"}).sort_values(["issue", "lead"]).copy()
    out["target"] = out["issue"] + pd.to_timedelta(out["lead"], unit="h")
    # 3시간 간격 구간(연장기간) 표시: 발표별로 lead 간격이 처음 3이 되는 지점부터
    step = out.groupby("issue")["lead"].diff()
    first3 = out.loc[step == 3].groupby("issue")["lead"].min()
    out["code"] = out["lead"] >= out["issue"].map(first3).fillna(10**6) if len(first3) else False
    return out[["issue", "lead", "target", "value", "code"]]


class ForecastArchive:
    """요소별 예보 묶음.
    hourly[elem]: issue, lead, target, value, code
    daily[elem] (TMX·TMN): issue, target_date, value"""

    def __init__(self) -> None:
        self.hourly: dict[str, pd.DataFrame] = {}
        self.daily: dict[str, pd.DataFrame] = {}
        self.n_missing: dict[str, int] = {}
        self.failed_slots: list[tuple[str, pd.Timestamp]] = []
        self._iss_cache: dict[str, set] = {}

    @classmethod
    def from_items(cls, items: Iterable[Mapping[str, Any]] | pd.DataFrame) -> "ForecastArchive":
        a = cls()
        a.add_items(items)
        return a

    def add_items(self, items: Iterable[Mapping[str, Any]] | pd.DataFrame) -> tuple[str, ...]:
        """API 항목(여러 발표 가능)을 더한다. cropwater read_openapi_csv + Archive._store 와 같은 규칙"""
        self._iss_cache = {}
        d = items if isinstance(items, pd.DataFrame) else items_frame(items)
        if d.empty:
            return ()
        d = d.drop_duplicates(["baseDate", "baseTime", "category", "fcstDate", "fcstTime"], keep="last")
        issue = pd.to_datetime(d["baseDate"] + d["baseTime"].str.zfill(4), format="%Y%m%d%H%M")
        target = pd.to_datetime(d["fcstDate"] + d["fcstTime"].str.zfill(4), format="%Y%m%d%H%M")
        added = []
        for e in ELEMENTS + OPTIONAL:
            m = (d["category"] == e).values
            if not m.any():
                continue
            raw = d.loc[m, "fcstValue"]
            if e == "PCP":
                conv = {s: pcp_mm(s) for s in raw.unique()}
                v = raw.map(conv).astype(float)
            else:
                v = pd.to_numeric(raw, errors="coerce")
            bad = (v.isna() | (v.abs() >= MISSING_ABS)).values
            iss, tgt, val = issue[m][~bad], target[m][~bad], v[~bad]
            if e in ("TMX", "TMN"):
                df = pd.DataFrame(
                    {"issue": iss.values, "target_date": tgt.dt.normalize().values, "value": val.values}
                )
            else:
                lead = ((tgt - iss) / pd.Timedelta(hours=1)).round().astype(int)
                df = pd.DataFrame({"issue": iss.values, "forecast": lead.values, "value": val.values})
            self.n_missing[e] = self.n_missing.get(e, 0) + int(bad.sum())
            self._store(e, df.reset_index(drop=True))
            added.append(e)
        return tuple(added)

    def _store(self, elem: str, df: pd.DataFrame) -> None:
        if elem == "SKY":  # 코드표에 없는 값(예: 0)은 결측
            bad = ~df["value"].round(6).isin(SKY_VALID)
            self.n_missing[elem] = self.n_missing.get(elem, 0) + int(bad.sum())
            df = df[~bad].reset_index(drop=True)
        if elem in ("TMX", "TMN"):
            new = df[["issue", "target_date", "value"]]
            old = self.daily.get(elem)
            self.daily[elem] = (
                new
                if old is None
                else pd.concat([old, new]).drop_duplicates(["issue", "target_date"], keep="last")
            )
        else:
            new = _hourly_targets(df)
            old = self.hourly.get(elem)
            self.hourly[elem] = (
                new if old is None else pd.concat([old, new]).drop_duplicates(["issue", "lead"], keep="last")
            )

    @property
    def issues(self) -> list[pd.Timestamp]:
        s: set = set()
        for d in list(self.hourly.values()) + list(self.daily.values()):
            s |= set(d["issue"])
        return sorted(s)

    def issue_set(self, elem: str) -> set:
        if elem not in self._iss_cache:
            d = self.hourly.get(elem) if elem in self.hourly else self.daily.get(elem)
            self._iss_cache[elem] = set(d["issue"]) if d is not None else set()
        return self._iss_cache[elem]

    @property
    def complete_issues(self) -> list[pd.Timestamp]:
        """필수 6요소에 모두 값이 있는 발표시각 (선택 요소 SKY·POP은 보지 않음)"""
        sets = [
            set(d["issue"]) for e, d in list(self.hourly.items()) + list(self.daily.items()) if e in ELEMENTS
        ]
        return sorted(set.intersection(*sets)) if sets else []


# ── 하루 입력 ───────────────────────────────────────────────────────────
def _latest_as_of(hdf: pd.DataFrame, run: pd.Timestamp) -> pd.DataFrame:
    """발표시각 run 이전(포함) 자료 중 대상시각별 가장 최근 발표 값"""
    sub = hdf[hdf["issue"] <= run].sort_values("issue")
    return sub.drop_duplicates("target", keep="last").set_index("target")


def sun_weights(times: Any, lat: float | None = None, lon: float | None = None) -> np.ndarray:
    """시각별 태양고도의 사인값(밤은 0) — 낮 시간 하늘상태·강수확률을 일사 비중으로 평균할 때의 가중치.
    KST(동경 135°) 시각 → 태양시 = 시각 + (경도 − 135)/15 + 균시차[FAO-56 식32].
    위도가 없으면 06~18시 같은 가중치."""
    t = pd.DatetimeIndex(times)
    if lat is None:
        return np.where((t.hour >= 6) & (t.hour <= 18), 1.0, 0.0)
    J = t.dayofyear.values
    b = 2 * np.pi * (J - 81) / 364
    sc = 0.1645 * np.sin(2 * b) - 0.1255 * np.cos(b) - 0.025 * np.sin(b)
    solar = t.hour.values + t.minute.values / 60 + ((135.0 if lon is None else lon) - 135.0) / 15 + sc
    omega = np.pi / 12 * (solar - 12)
    phi, dec = np.radians(lat), 0.409 * np.sin(2 * np.pi * J / 365 - 1.39)
    return np.maximum(np.sin(phi) * np.sin(dec) + np.cos(phi) * np.cos(dec) * np.cos(omega), 0.0)


def daily_inputs(
    arch: ForecastArchive,
    run: Any,
    targets: Iterable[Any],
    lat: float | None = None,
    lon: float | None = None,
) -> pd.DataFrame:
    """발표 run의 대상일 목록 → 일 입력 DataFrame
    열: target, lead_day, Tmax, Tmin, ea, u10, rain, rain_flag, hours, ext, filled,
        (SKY·POP이 있으면) sky_cloudy, sky_overcast, pop, pop_max, rain_exp"""
    run = pd.Timestamp(run)
    sel = {e: _latest_as_of(arch.hourly[e], run) for e in ("TMP", "REH", "WSD", "PCP") if e in arch.hourly}
    # 선택 요소는 서비스 발표 자체에 있을 때만 쓴다(없는 발표를 이전 발표로 통째로 대신하지 않음)
    osel = {
        e: _latest_as_of(arch.hourly[e], run)
        for e in OPTIONAL
        if e in arch.hourly and run in arch.issue_set(e)
    }
    dsel = {}
    for e in ("TMX", "TMN"):
        d = arch.daily[e]
        d = d[d["issue"] == run]
        dsel[e] = dict(zip(d["target_date"], d["value"], strict=True))
    rows, prev_wsd = [], None
    for tday in targets:
        tday = pd.Timestamp(tday).normalize()

        def win(s, tday=tday):
            return s[(s.index >= tday) & (s.index < tday + pd.Timedelta(days=1))]

        tmp, reh, wsd, pcp = (win(sel[e]) if e in sel else None for e in ("TMP", "REH", "WSD", "PCP"))
        ext = bool(wsd is not None and wsd["code"].any())
        # ea: TMP·REH가 모두 있는 시각의 평균
        ea = None
        if tmp is not None and reh is not None:
            j = tmp[["value"]].join(reh[["value"]], lsuffix="_t", rsuffix="_h", how="inner")
            if len(j):
                ea = float((j["value_t"].map(_svp) * j["value_h"] / 100).mean())
        # 풍속: 연장기간 코드 변환
        u10 = None
        if wsd is not None and len(wsd):
            vals = []
            for v, c in zip(wsd["value"], wsd["code"], strict=True):
                if c:
                    k = int(round(v))
                    if k == 1:
                        vals.append(min(prev_wsd, WSD_CODE1_CAP) if prev_wsd is not None else WSD_CODE1_CAP)
                    else:
                        vals.append(WSD_CODE.get(k, v))
                else:
                    vals.append(v)
            u10 = float(sum(vals) / len(vals))
            if not ext:
                prev_wsd = u10
        # 강수: 1시간 값은 mm, 코드 칸은 3시간 × 대표강도
        rain = None
        if pcp is not None and len(pcp):
            rain = float(
                sum(
                    (3 * PCP_CODE_MMH.get(int(round(v)), 0.0)) if c else v
                    for v, c in zip(pcp["value"], pcp["code"], strict=True)
                )
            )
        hours = int(len(tmp)) if tmp is not None else 0
        filled = int((tmp["issue"] < run).sum()) if tmp is not None else 0
        row = dict(
            target=tday,
            lead_day=int((tday - run.normalize()).days),
            Tmax=dsel["TMX"].get(tday),
            Tmin=dsel["TMN"].get(tday),
            ea=ea,
            u10=u10,
            rain=rain,
            rain_flag=(None if rain is None else int(rain >= RAIN_FLAG_MM)),
            hours=hours,
            ext=ext,
            filled=filled,
        )
        # 하늘상태: 낮 시간 일사 비중으로 가중한 구름많음·흐림 비율 / 강수확률: 같은 가중의 낮 평균, 하루 최대
        for e in OPTIONAL:
            if e not in arch.hourly:
                continue
            x = win(osel[e]) if e in osel else None
            w = sun_weights(x.index, lat, lon) if x is not None and len(x) else np.zeros(0)
            ok = w.sum() > 0
            if e == "SKY":
                v = x["value"].round().values if ok else None
                row["sky_cloudy"] = float((w * (v == 3)).sum() / w.sum()) if ok else None
                row["sky_overcast"] = float((w * (v == 4)).sum() / w.sum()) if ok else None
            else:
                row["pop"] = float((w * x["value"].values).sum() / w.sum() / 100) if ok else None
                row["pop_max"] = float(x["value"].max() / 100) if ok else None
                # 기대 강수량: 시각마다 강수량 × 강수확률 (강수확률이 없는 시각은 1)
                if pcp is not None and len(pcp):
                    amt = np.where(
                        pcp["code"].values,
                        3 * np.array([PCP_CODE_MMH.get(int(round(v)), 0.0) for v in pcp["value"]]),
                        pcp["value"].values.astype(float),
                    )
                    pp = (
                        (x["value"].reindex(pcp.index).values / 100)
                        if x is not None
                        else np.full(len(pcp), np.nan)
                    )
                    row["rain_exp"] = float(np.sum(amt * np.where(np.isnan(pp), 1.0, pp)))
                else:
                    row["rain_exp"] = None
        rows.append(row)
    return pd.DataFrame(rows)


# ── 서비스 발표 표 (운영) ───────────────────────────────────────────────
def error_kind(issue: Any) -> str:
    """발표 종류: 02~14시 발표 → '아침', 17~23시 발표 → '저녁'. 오차표 칸을 고를 때 씀"""
    return "아침" if pd.Timestamp(issue).hour <= 14 else "저녁"


def backup_source(arch: ForecastArchive, run: Any, max_age_h: int = BACKUP_MAX_H) -> pd.Timestamp | None:
    """서비스 발표 run을 쓸 수 있으면 run.
    아니면 run 이전 max_age_h 시간 안에서 필수 6요소가 찬 가장 최근 발표. 없으면 None"""
    run = pd.Timestamp(run)
    comp = arch.complete_issues
    if run in set(comp):
        return run
    cand = [t for t in comp if run - pd.Timedelta(hours=max_age_h) <= t < run]
    return max(cand) if cand else None


def service_table(
    arch: ForecastArchive,
    runs: Mapping[str, tuple[int, range]] = SERVICE_RUNS,
    lat: float | None = None,
    lon: float | None = None,
    max_age_h: int = BACKUP_MAX_H,
    start: Any = None,
    end: Any = None,
) -> pd.DataFrame:
    """서비스 발표(02시·17시)마다 대상일별 하루 입력 (cropwater ops_service_table).
    서비스 발표가 없으면 직전 발표로 대신한다. 대신한 발표는 그 발표 시각까지의 자료로 하루 입력을 만들고,
    선행일을 두 가지로 적는다: lead_day = 서비스 발표 기준, src_lead = 대체 발표 날짜 기준(S4 계수·오차 r).
    추가 열: src_run, src_lead, backup, err_name(오차표 발표 종류), err_lead.
    대신할 발표도 없는 회차는 arch.failed_slots에 (구분, 발표시각)으로 남긴다.
    start·end: 서비스 발표시각 범위"""
    out = []
    arch.failed_slots = []
    iss = arch.issues
    if not iss:
        return pd.DataFrame()
    t0 = pd.Timestamp(start) if start is not None else pd.Timestamp(iss[0]).normalize()
    t1 = pd.Timestamp(end) if end is not None else pd.Timestamp(iss[-1])
    days = pd.date_range(t0.normalize(), t1.normalize(), freq="D")
    for name, (hour, leads) in runs.items():
        for day in days:
            run = day + pd.Timedelta(hours=hour)
            if run < t0 or run > t1:
                continue
            src = backup_source(arch, run, max_age_h)
            if src is None:
                arch.failed_slots.append((name, run))
                continue
            targets = [run.normalize() + pd.Timedelta(days=k) for k in leads]
            df = daily_inputs(arch, src, targets, lat, lon)
            df = df.rename(columns={"lead_day": "src_lead"})
            df.insert(0, "lead_day", [int(k) for k in leads])
            df.insert(0, "run", run)
            df.insert(0, "run_name", name)
            df["src_run"] = src
            df["backup"] = bool(src != run)
            df["err_name"] = name if src == run else error_kind(src)
            df["err_lead"] = df["lead_day"] if src == run else df["src_lead"]
            out.append(df)
    if not out:
        return pd.DataFrame()
    return pd.concat(out, ignore_index=True).sort_values(["run", "lead_day"]).reset_index(drop=True)
