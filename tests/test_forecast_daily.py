"""단기예보 항목 → 하루 입력 (core/forecast_daily.py).

cropwater `tests/test_fcst.py`·`tests/test_ops.py`(feat/cycle02-forecast)의 해당 테스트를 가져왔다.
포털 CSV 읽기 테스트는 그 코드를 옮기지 않아 뺐고, OpenAPI 파일 테스트는 같은 항목을 바로 넣는 방식으로
바꿨다.
입력은 모두 가짜 값이다.
"""

import math

import numpy as np
import pandas as pd
import pytest

from croplib_dp.core.fao56 import svp
from croplib_dp.core.forecast_daily import (
    HOURLY_N,
    PCP_LT1_MM,
    TMN_N,
    TMX_N,
    ForecastArchive,
    _hourly_targets,
    check_issue,
    daily_inputs,
    error_kind,
    items_frame,
    pcp_mm,
    service_table,
    sun_weights,
    usable_items,
)

TS = pd.Timestamp


def _hourly(issue, leads, values):
    return _hourly_targets(pd.DataFrame({"issue": TS(issue), "forecast": leads, "value": values}))


def _daily(issue, first, values):
    """TMX·TMN 표: 발표일 + first 일부터 값마다 하루씩"""
    d = TS(issue).normalize()
    return pd.DataFrame(
        {
            "issue": TS(issue),
            "target_date": [d + pd.Timedelta(days=first + k) for k in range(len(values))],
            "value": values,
        }
    )


def _archive():
    """전날 17시 발표(00~07시 채움용) + 02시 서비스 발표(08시~D+3, 마지막 날 3시간·코드)"""
    a = ForecastArchive()
    run = "2026-04-02 02:00"
    lead_q = list(range(6, 71))  # 08시 ~ D+3 00시 (1시간)
    lead_c = list(range(73, 92, 3))  # D+3 03~21시 (3시간, 코드)
    leads = lead_q + lead_c
    prev = ("2026-04-01 17:00", list(range(7, 15)))  # 04-02 00~07시
    a.hourly["TMP"] = pd.concat(
        [_hourly(prev[0], prev[1], [10.0] * 8), _hourly(run, leads, [20.0] * len(leads))]
    )
    a.hourly["REH"] = pd.concat(
        [_hourly(prev[0], prev[1], [80.0] * 8), _hourly(run, leads, [50.0] * len(leads))]
    )
    wsd = [2.0] * len(lead_q) + [2.0] * len(lead_c)  # 코드 2 = 6.5 m/s
    a.hourly["WSD"] = pd.concat([_hourly(prev[0], prev[1], [1.0] * 8), _hourly(run, leads, wsd)])
    pcp = [0.0] * len(leads)
    pcp[leads.index(10)] = 1.5  # 04-02 12시 1.5 mm
    pcp[leads.index(76)] = 1.0  # D+3 06시 코드 1 → 1.5 mm/h × 3 h
    a.hourly["PCP"] = pd.concat([_hourly(prev[0], prev[1], [0.0] * 8), _hourly(run, leads, pcp)])
    a.daily["TMX"] = _daily(run, 0, [18.0, 19.0, 20.0, 21.0])
    a.daily["TMN"] = _daily(run, 0, [5.0, 6.0, 7.0, 8.0])
    return a, TS(run)


def _issue_items(issue, sky="1", drop=()):
    """발표 1개의 API 항목(8요소, 정상 행 수). 1시간 요소는 lead 1부터, TMX·TMN은 발표시각별 대상일 수"""
    issue = TS(issue)
    h = issue.hour
    vals = dict(TMP="20", REH="60", WSD="2.1", PCP="강수없음", SKY=sky, POP="20")
    out = []
    for cat, v in vals.items():
        for k in range(1, HOURLY_N[h] + 1):
            t = issue + pd.Timedelta(hours=k)
            out.append(
                dict(
                    baseDate=f"{issue:%Y%m%d}",
                    baseTime=f"{issue:%H}00",
                    category=cat,
                    fcstDate=f"{t:%Y%m%d}",
                    fcstTime=f"{t:%H}00",
                    fcstValue=v,
                )
            )
    first_x = 0 if h <= 11 else 1
    first_n = 0 if h == 2 else 1
    for cat, first, cnt, hh, v in (
        ("TMX", first_x, TMX_N[h], "1500", "25"),
        ("TMN", first_n, TMN_N[h], "0600", "12"),
    ):
        for k in range(cnt):
            d = issue.normalize() + pd.Timedelta(days=first + k)
            out.append(
                dict(
                    baseDate=f"{issue:%Y%m%d}",
                    baseTime=f"{issue:%H}00",
                    category=cat,
                    fcstDate=f"{d:%Y%m%d}",
                    fcstTime=hh,
                    fcstValue=v,
                )
            )
    return [x for x in out if x["category"] not in drop]


def _arch_from_issues(issues):
    a = ForecastArchive()
    for iss in issues:
        a.add_items(usable_items(items_frame(_issue_items(iss)), iss))
    return a


# ── 강수 문자열, 받은 발표 확인 ────────────────────────────────────────
def test_pcp_strings():
    assert (pcp_mm("30.0~50.0mm"), pcp_mm("50.0mm 이상"), pcp_mm("0.3"), pcp_mm("1.0mm 미만")) == (
        40.0,
        50.0,
        0.3,
        PCP_LT1_MM,
    )
    assert pcp_mm("강수없음") == 0.0 and pcp_mm("2.0mm") == 2.0
    assert math.isnan(pcp_mm("?"))


def test_check_issue_counts():
    iss = TS("2026-06-10 02:00")
    c = check_issue(items_frame(_issue_items(iss)), iss)
    assert c["complete8"] and c["counts"]["TMP"] == 78 and c["counts"]["TMX"] == 4
    it = _issue_items(iss)
    it.pop(3)  # TMP 한 칸 빠짐
    c = check_issue(items_frame(it), iss)
    assert not c["required_ok"] and "TMP 77/78" in c["missing"]
    it = _issue_items(iss)
    it[[i for i, x in enumerate(it) if x["category"] == "SKY"][0]]["fcstValue"] = (
        "0"  # 코드표 밖 값 → 빠진 칸
    )
    c = check_issue(items_frame(it), iss)
    assert c["required_ok"] and not c["optional_ok"]
    it = _issue_items(iss)
    it[0]["fcstValue"] = "-999"  # 결측값
    assert not check_issue(items_frame(it), iss)["required_ok"]
    c = check_issue(items_frame(_issue_items(TS("2026-06-10 17:00"))), TS("2026-06-10 17:00"))
    assert c["complete8"] and c["expected"]["TMP"] == 87


def test_usable_items_drops_optional_or_whole_issue():
    iss = TS("2026-06-10 02:00")
    it = _issue_items(iss)
    it[[i for i, x in enumerate(it) if x["category"] == "SKY"][0]]["fcstValue"] = "0"
    u = usable_items(items_frame(it), iss)
    assert set(u.category) == {
        "TMX",
        "TMN",
        "TMP",
        "REH",
        "WSD",
        "PCP",
    }  # 선택 요소가 덜 차면 두 요소를 뺌 → S3
    assert usable_items(
        items_frame(_issue_items(iss, drop=("WSD",))), iss
    ).empty  # 필수 요소가 없으면 쓰지 않음


# ── 요소별 표 ───────────────────────────────────────────────────────────
def _api(issue, rows):
    issue = TS(issue)
    out = []
    for cat, t, v in rows:
        t = TS(t)
        out.append(
            dict(
                baseDate=f"{issue:%Y%m%d}",
                baseTime=f"{issue:%H%M}",
                category=cat,
                fcstDate=f"{t:%Y%m%d}",
                fcstTime=f"{t:%H%M}",
                fcstValue=v,
            )
        )
    return out


def _api_items():
    prev = _api(
        "2025-03-31 23:00",
        [(c, f"2025-04-01 {h:02d}:00", v) for h in range(6) for c, v in (("TMP", "10"), ("PCP", "강수없음"))],
    )
    run = _api(
        "2025-04-01 02:00",
        [
            ("TMP", "2025-04-01 03:00", "20"),
            ("PCP", "2025-04-01 03:00", "1mm 미만"),
            ("TMP", "2025-04-01 04:00", "20"),
            ("PCP", "2025-04-01 04:00", "2.0mm"),
            ("TMP", "2025-04-01 05:00", "20"),
            ("PCP", "2025-04-01 05:00", "강수없음"),
            ("TMN", "2025-04-01 06:00", "5"),
            ("TMX", "2025-04-01 15:00", "18"),
            ("WAV", "2025-04-01 03:00", "-999"),
        ],
    )
    again = _api("2025-04-01 02:00", [("TMP", "2025-04-01 05:00", "21")])  # 같은 칸이 다시 오면 뒤의 값
    return prev + run + again


def test_items_to_tables_strings_and_duplicates():
    a = ForecastArchive()
    assert set(a.add_items(_api_items())) == {"TMP", "PCP", "TMX", "TMN"}  # 쓰지 않는 요소(WAV)는 읽지 않음
    tmp = a.hourly["TMP"][a.hourly["TMP"].issue == TS("2025-04-01 02:00")]
    assert list(tmp.lead) == [1, 2, 3] and list(tmp.value) == [20, 20, 21]  # lead = 예보시각 − 발표시각
    pcp = a.hourly["PCP"][a.hourly["PCP"].issue == TS("2025-04-01 02:00")]
    assert list(pcp.value) == [PCP_LT1_MM, 2.0, 0.0]
    assert list(a.daily["TMX"].target_date) == [TS("2025-04-01")] and list(a.daily["TMN"].value) == [5]


def test_archive_fills_only_hours_before_first_lead():
    a = ForecastArchive.from_items(_api_items())
    run = TS("2025-04-01 02:00")
    d = daily_inputs(a, run, [TS("2025-04-01")]).iloc[0]
    assert d.hours == 6 and d.filled == 3  # 00~02시만 전날 23시 발표, 03시부터는 02시 발표 자체의 값
    assert (d.Tmax, d.Tmin) == (18, 5) and d.rain == pytest.approx(PCP_LT1_MM + 2.0) and d.rain_flag == 1


def test_invalid_sky_code_is_missing():
    rows = _api("2026-06-01 02:00", [("SKY", "2026-06-01 03:00", "3"), ("SKY", "2026-06-01 04:00", "0")])
    a = ForecastArchive.from_items(rows)
    assert len(a.hourly["SKY"]) == 1 and a.n_missing["SKY"] == 1  # 코드표에 없는 0은 결측


# ── 하루 입력 ───────────────────────────────────────────────────────────
def test_daily_inputs_fill_and_codes():
    a, run = _archive()
    out = daily_inputs(a, run, [run.normalize() + pd.Timedelta(days=k) for k in range(4)]).set_index(
        "lead_day"
    )
    d0 = out.loc[0]
    assert d0.hours == 24 and d0.filled == 8  # 00~07시는 전날 17시 발표로 채움
    assert d0.ea == pytest.approx((8 * svp(10.0) * 0.80 + 16 * svp(20.0) * 0.50) / 24)
    assert d0.u10 == pytest.approx((8 * 1.0 + 16 * 2.0) / 24)
    assert d0.rain == pytest.approx(1.5) and d0.rain_flag == 1
    assert (d0.Tmax, d0.Tmin) == (18.0, 5.0)
    d3 = out.loc[3]
    assert d3.ext and d3.hours == 8  # 00시(1시간) + 03~21시(3시간)
    assert d3.u10 == pytest.approx((2.0 + 7 * 6.5) / 8)
    assert d3.rain == pytest.approx(4.5) and d3.rain_flag == 1


def test_sun_weights_daytime_shape():
    t = pd.date_range("2026-06-21 00:00", periods=24, freq="h")
    w = sun_weights(t, 37.9, 127.74)
    assert w[0] == 0 and w[23] == 0 and w[12] > w[9] > w[7] > 0  # 태양시 정오 ≈ 12:30
    assert abs(w[12] - w[13]) < 0.02
    w0 = sun_weights(t)  # 위도 없으면 06~18시 같은 가중치
    assert w0.sum() == 13 and w0[5] == 0 and w0[6] == 1


def test_daily_inputs_sky_fractions_and_rain_exp():
    a, run = _archive()
    lead = a.hourly["TMP"][a.hourly["TMP"].issue == run].lead.tolist()
    t = [run + pd.Timedelta(hours=h) for h in lead]
    val = [
        4.0 if tt.normalize() == run.normalize() + pd.Timedelta(days=1) else 1.0 for tt in t
    ]  # D+1 종일 흐림
    a.hourly["SKY"] = _hourly(str(run), lead, val)
    a.hourly["POP"] = _hourly(str(run), lead, [60.0] * len(lead))
    a._iss_cache = {}
    out = daily_inputs(a, run, [run.normalize() + pd.Timedelta(days=k) for k in range(4)], 37.9, 127.74)
    out = out.set_index("lead_day")
    assert out.loc[1, "sky_overcast"] == pytest.approx(1.0) and out.loc[1, "sky_cloudy"] == 0
    assert out.loc[2, "sky_overcast"] == 0 and out.loc[2, "pop"] == pytest.approx(0.60)
    assert out.loc[0, "rain"] == pytest.approx(1.5) and out.loc[0, "rain_exp"] == pytest.approx(
        0.9
    )  # 1.5 mm × 60%
    assert out.loc[3, "rain"] == pytest.approx(4.5) and out.loc[3, "rain_exp"] == pytest.approx(
        2.7
    )  # 코드 1 → 4.5 mm
    assert out.loc[1, "rain_exp"] == 0.0
    assert run in a.complete_issues  # 선택 요소가 없어도 필수 6요소로 완전 발표


# ── 서비스 발표 표 (직전 발표로 대신) ───────────────────────────────────
def test_service_table_uses_previous_issue():
    a = _arch_from_issues([TS("2026-06-09 23:00"), TS("2026-06-10 02:00"), TS("2026-06-10 14:00")])
    st = service_table(a, start=TS("2026-06-10"), end=TS("2026-06-10 17:00"))
    m = st[st.run == TS("2026-06-10 02:00")]
    assert not m.backup.any() and list(m.src_lead) == list(m.lead_day) == [0, 1, 2, 3]
    ref = daily_inputs(a, TS("2026-06-10 02:00"), [TS("2026-06-10") + pd.Timedelta(days=k) for k in range(4)])
    assert np.allclose(m.ea.values, ref.ea.values) and np.allclose(m.u10.values, ref.u10.values)
    e = st[st.run == TS("2026-06-10 17:00")]  # 17시 없음 → 14시 발표로(D+4는 없음)
    assert e.backup.all() and set(e.src_run) == {TS("2026-06-10 14:00")}
    assert list(e.lead_day) == [1, 2, 3, 4] and list(e.src_lead) == [1, 2, 3, 4]
    assert pd.isna(e.set_index("lead_day").loc[4, "Tmax"])
    assert set(e.err_name) == {"아침"} and error_kind(TS("2026-06-09 23:00")) == "저녁"
    a2 = _arch_from_issues([TS("2026-06-09 23:00")])
    m2 = service_table(a2, start=TS("2026-06-10"), end=TS("2026-06-10 02:00"))
    assert m2.backup.all() and list(m2.src_lead) == [1, 2, 3, 4] and set(m2.err_name) == {"저녁"}
    a3 = _arch_from_issues([TS("2026-06-08 23:00")])  # 27시간 전 → 대신할 발표 없음
    assert service_table(a3, start=TS("2026-06-10"), end=TS("2026-06-10 02:00")).empty
    assert ("아침", TS("2026-06-10 02:00")) in a3.failed_slots
