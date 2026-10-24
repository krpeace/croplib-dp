"""예보 계산에 쓰는 지점 계수 파일 읽기 (패키지 안 `croplib_dp/data/`).

파일은 cropwater(feat/cycle02-forecast)에서 그대로 가져왔다. 모두 공공 자료(단기예보·ASOS)로 만든 값이다.
  rs_coef.csv         S3 일사 계수 (지점별, 없으면 stn 0 = FAO-56 기본값 S1)
  rs_sky_coef.csv     S4 일사 계수 (지점 × 선행일, 강수 입력 = 강수확률 하루 최대)
  fcst_error_table.csv 예보 ETo 오차표 (지점 × 해 × 발표 × 선행일 × 월)
지금은 춘천(101)만 있다. 다른 지점은 예보 지점을 늘릴 때(물6) 계수를 만들어 더한다.
"""

import csv
from importlib import resources

import pandas as pd

from croplib_dp.core.outlook import ERR_COLS
from croplib_dp.core.solar import S4_NAMES, S4_RAIN

DATA = "croplib_dp.data"


def _rows(name: str) -> list[dict]:
    with resources.files(DATA).joinpath(name).open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_s3_coef(stn: str) -> dict:
    """지점 S3 계수(없으면 FAO-56 기본값 행). 반환: dict(method, krs, a, b, c, stn)"""
    rows = _rows("rs_coef.csv")
    pick = [r for r in rows if str(r.get("stn")) == str(stn)] or [r for r in rows if str(r.get("stn")) == "0"]
    if not pick:
        return dict(method="S1", krs=0.16, a=None, b=None, c=None, stn=None)
    r = pick[-1]

    def num(k):
        return float(r[k]) if r.get(k) not in (None, "") else None

    return dict(
        method=r.get("method") or ("S3" if num("a") is not None else "S1"),
        krs=num("krs") or 0.16,
        a=num("a"),
        b=num("b"),
        c=num("c"),
        stn=r.get("stn"),
    )


def load_s4_coef(stn: str) -> pd.DataFrame:
    """지점 S4 계수 DataFrame[lead_day, a~e] (지점 행이 없으면 빈 표 → S3로 계산)"""
    rows = [r for r in _rows("rs_sky_coef.csv") if str(r.get("stn")).strip() == str(stn)]
    for r in rows:
        got = r.get("rain_input") or "rain_flag"
        if got != S4_RAIN:
            raise ValueError(f"rs_sky_coef.csv: S4 계수의 강수 입력이 {got}입니다({S4_RAIN} 필요)")
    t = pd.DataFrame(
        [dict(lead_day=int(r["lead_day"]), **{k: float(r[k]) for k in S4_NAMES}) for r in rows],
        columns=["lead_day", *S4_NAMES],
    )
    return t.sort_values("lead_day").reset_index(drop=True)


def load_error_rows() -> pd.DataFrame:
    """예보 ETo 오차표 전체"""
    with resources.files(DATA).joinpath("fcst_error_table.csv").open(encoding="utf-8-sig") as f:
        t = pd.read_csv(f, dtype={"stn": str})
    return t[ERR_COLS]
