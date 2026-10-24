"""예보 대조 테스트의 가짜 입력 (단기예보 API 항목, 관측 ETo·강수). cropwater 없이 읽을 수 있다.

make_forecast_golden.py(기준값 만들기)와 tests/test_forecast_parity.py(대조)가 함께 쓴다.
"""

import math

import pandas as pd

HOURLY_N = {
    2: 78,
    5: 75,
    8: 72,
    11: 69,
    14: 66,
    17: 87,
    20: 84,
    23: 81,
}  # 발표시각별 1시간 요소 행 수(활용가이드)
TMX_N = {2: 4, 5: 4, 8: 4, 11: 4, 14: 3, 17: 4, 20: 4, 23: 4}
TMN_N = {2: 4, 5: 3, 8: 3, 11: 3, 14: 3, 17: 4, 20: 4, 23: 4}
TS = pd.Timestamp
LAT, LON, ELEV = 37.90262, 127.7357, 75.82
SKIP = {TS("2026-06-10 17:00"), TS("2026-06-08 14:00")}  # 빠진 발표 → 직전 발표로 대신
NO_SKY = {TS("2026-06-09 17:00")}  # 하늘상태·강수확률이 빠진 발표 → 일사 추정 S3로 대신


def pcp_text(v):
    if v <= 0:
        return "강수없음"
    if v < 1:
        return "1mm 미만"
    if v >= 50:
        return "50.0mm 이상"
    if v >= 30:
        return "30.0~50.0mm"
    return f"{v:.1f}mm"


def issue_items(issue):
    """발표 1개의 API 항목 8요소. 값은 발표·대상 시각에 따라 결정적으로 바뀐다"""
    issue = TS(issue)
    h = issue.hour
    n = HOURLY_N[h]
    seed = issue.day * 24 + h
    out = []
    for k in range(1, n + 1):
        t = issue + pd.Timedelta(hours=k)
        hr = t.hour
        tmp = 19 + 7 * math.sin((hr - 9) / 24 * 2 * math.pi) + (seed % 5) * 0.3 + t.day % 3
        reh = 55 + 25 * math.cos((hr - 4) / 24 * 2 * math.pi) + (seed % 4) * 2
        wsd = 1.0 + ((hr + seed) % 7) * 0.4
        rain = 0.0
        if t.day == 11 and 9 <= hr <= 14:
            rain = [0.5, 2.0, 4.0, 12.0, 35.0, 0.3][hr - 9]
        if t.day == 12 and hr == 16:
            rain = 60.0
        sky = "4" if rain > 0 else ("3" if (hr + seed) % 5 == 0 else "1")
        pop = 70 if rain > 0 else (30 if sky == "3" else 10)
        cats = (
            ("TMP", f"{tmp:.0f}"),
            ("REH", f"{min(reh, 100):.0f}"),
            ("WSD", f"{wsd:.1f}"),
            ("PCP", pcp_text(rain)),
            ("SKY", sky),
            ("POP", str(pop)),
        )
        for cat, v in cats:
            if issue in NO_SKY and cat in ("SKY", "POP"):
                continue
            out.append(
                dict(
                    baseDate=f"{issue:%Y%m%d}",
                    baseTime=f"{issue:%H}00",
                    category=cat,
                    fcstDate=f"{t:%Y%m%d}",
                    fcstTime=f"{t:%H}00",
                    fcstValue=v,
                    nx=73,
                    ny=134,
                )
            )
    first_x = 0 if h <= 11 else 1
    first_n = 0 if h == 2 else 1
    for cat, first, cnt, hh in (("TMX", first_x, TMX_N[h], "1500"), ("TMN", first_n, TMN_N[h], "0600")):
        for k in range(cnt):
            d = issue.normalize() + pd.Timedelta(days=first + k)
            base = 27 + d.day % 4 + (seed % 3) * 0.5 if cat == "TMX" else 14 + d.day % 3
            out.append(
                dict(
                    baseDate=f"{issue:%Y%m%d}",
                    baseTime=f"{issue:%H}00",
                    category=cat,
                    fcstDate=f"{d:%Y%m%d}",
                    fcstTime=hh,
                    fcstValue=f"{base:.1f}",
                    nx=73,
                    ny=134,
                )
            )
    return out


def all_issues():
    t = TS("2026-06-07 02:00")
    out = []
    while t <= TS("2026-06-10 17:00"):
        if t not in SKIP:
            out.append(t)
        t += pd.Timedelta(hours=3)
    return out


def obs_frame():
    dates = pd.date_range("2026-03-25", "2026-06-08")  # 6/9 관측은 아직 없음(관측 지연)
    eto = [2.5 + 2.5 * ((i * 5) % 9) / 8 for i in range(len(dates))]
    rain = [0.0] * len(dates)
    for k, v in {10: 12.0, 30: 45.0, 50: 6.0, 60: 22.0, 70: 3.0, 73: 38.0}.items():
        rain[k] = v
    return pd.DataFrame(
        dict(
            date=dates, ETo_obs=eto, Tmax=26.0, Tmin=14.0, ea_obs=1.4, u10=1.6, Rs=18.0, rain=rain, pa=1002.0
        )
    )
