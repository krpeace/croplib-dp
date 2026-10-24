"""cropwater(feat/cycle02-forecast 스냅샷) 코드로 대조 기준값을 만든다. 가짜 입력만 쓴다."""

import datetime as dt
import json
import sys

sys.path.insert(0, sys.argv[1])

import fcst_wb as W  # noqa: E402
import pandas as pd  # noqa: E402
from cropwater_multi import compute_station, compute_water_balance  # noqa: E402
from fao56_core import kc_climate_adjust  # noqa: E402


def items():
    out = []
    start = dt.date(2026, 5, 1)
    for i in range(30):
        d = start + dt.timedelta(days=i)
        if i == 11:  # 행 자체가 없음
            continue
        it = dict(
            tm=d.isoformat(),
            maxTa=f"{22 + (i % 7) * 1.3:.1f}",
            minTa=f"{10 + (i % 5) * 0.9:.1f}",
            avgTa=f"{16 + (i % 6) * 1.1:.1f}",
            avgRhm=f"{55 + (i % 9) * 3:.1f}",
            minRhm=f"{30 + (i % 8) * 2.5:.1f}",
            avgWs=f"{1.2 + (i % 4) * 0.6:.1f}",
            avgPv=f"{11 + (i % 5) * 0.8:.1f}",
            avgTd=f"{8 + (i % 5) * 0.7:.1f}",
            avgPa=f"{1003 - (i % 3) * 2.5:.1f}",
            sumGsr=f"{14 + (i % 10) * 1.4:.2f}",
            sumSsHr=f"{5 + (i % 6) * 1.2:.1f}",
            sumLrgEv="",
            sumRn="",
        )
        if i == 4:
            it["sumRn"] = "25.5"
        if i == 14:
            it["sumGsr"] = ""  # 일사 없음 → 일조로 추정
        if i == 17:
            it["avgPv"] = ""  # 증기압 없음 → 이슬점
        if i == 18:
            it["avgPv"] = ""
            it["avgTd"] = ""  # → 평균습도
        if i == 19:
            it["maxTa"] = ""  # ETo 계산 불가 → 앞 7일 평균
        if i == 21:
            it["sumRn"] = "80.0"
        if i == 25:
            it["sumRn"] = "3.0"
        out.append(it)
    return out


LAT, ELEV, ANEM = 37.90262, 75.82, 10.0
rows = items()
recs, _, _ = compute_station(rows, LAT, ELEV, ANEM)
recs = compute_water_balance(recs, 120.0, 60.0)
multi = [
    dict(
        date=r["date"],
        eto=r["PM"],
        eto_wb=r["ETo_wb"],
        filled=r["filled"],
        rain=r["rain"],
        dr=r["Dr"],
        dp=r["DP"],
        ks=r["Ks"],
    )
    for r in recs
]

# Kc 곡선 + 관측 물수지 + 상태 (결측 없는 기간, 사과 시나리오 3 기후보정)
kc_mid = kc_climate_adjust(1.20, 1.5, 55, 3.2)
kc_end = kc_climate_adjust(0.95, 1.5, 55, 3.2, is_end=True)
kp = dict(bud=dt.date(2026, 4, 1), L=(20, 70, 90, 30), kc_ini=0.50, kc_mid=kc_mid, kc_end=kc_end)
soil = dict(fc=0.22, wp=0.10, zr=1.0, p=0.5, taw=120.0, raw=60.0, dr0=0.0, ea=0.95)
dates = pd.date_range("2026-03-25", "2026-11-05")
eto = [3.0 + 2.0 * ((i * 7) % 11) / 10 for i in range(len(dates))]
rain = [0.0] * len(dates)
for k, v in {20: 15.0, 45: 40.0, 70: 8.0, 100: 120.0, 130: 30.0, 160: 55.0, 190: 12.0}.items():
    rain[k] = v
obs = pd.DataFrame({"date": dates, "ETo_obs": eto, "rain": rain})
irr = {dt.date(2026, 7, 1): 30.0}
w = W.observed_wb(obs, kp, soil, irrig=irr)
wb = [
    dict(
        date=str(r.date.date()),
        kc=float(r.Kc),
        etc=float(r.ETc),
        dr=float(r.Dr),
        ks=float(r.Ks),
        dp=float(r.DP),
        status=W.soil_status(r.Dr, 60.0, r.Kc, True),
    )
    for r in w.itertuples()
]
json.dump(
    dict(multi=multi, kc_mid=kc_mid, kc_end=kc_end, wb=wb, eto=eto, rain=rain), sys.stdout, ensure_ascii=False
)
