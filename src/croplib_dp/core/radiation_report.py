"""일사 점검 결과 + 해안선 거리 → 실태표(csv)·보고서(md)·관계 그림(svg) (이슈 #48 PR-B, 결정 0021).

순수 함수만 둔다: 파일·API를 모른다. 입력은 점검 수집기가 만든 지점별 결과(audit_station)와
지점별 해안선 거리·섬 판정(core/coast.classify_station)이다. 그림은 표준 라이브러리 문자열로 직접 그린다(SVG).
"""

# ruff: noqa: E501  (한글 문장·SVG 문자열이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import csv
import io
import statistics
from collections.abc import Mapping, Sequence
from typing import Any
from xml.sax.saxutils import escape

import numpy as np

from croplib_dp.core.radiation import COASTAL_MAX_KM, KRS_COASTAL, KRS_INLAND
from croplib_dp.core.radiation_audit import (
    CLASS_NORMAL,
    CLASSES,
    DEFAULT_CANDIDATES_KM,
    KRS_HIGH,
    KRS_LOW,
    MEANINGLESS_GAIN,
    MIN_SEASON_DAYS,
    MIN_VALID_DAYS,
    SEASONS,
)

CSV_COLUMNS = (
    "stn_id", "name", "lat", "lon", "elev_m", "island", "coast_km", "resolution_corrected", "klass",
    "days_total", "rs_values", "rs_zero", "rs_below_floor", "rs_month_invalid", "rs_valid", "rs_sparse",
    "ss_days", "temp_days", "suspect_sunshine_days", "db_measures_sunlight",
    "krs_n", "krs_median", "krs_q1", "krs_q3", "krs_3_5", "krs_6_8", "krs_9_11", "krs_12_2",
    "eto_n", "eto_bias_016", "eto_mae_016", "eto_bias_019", "eto_mae_019", "eto_best_krs",
)  # fmt: skip
COASTAL_CSV_COLUMNS = ("stn_id", "name_ko", "coast_km", "island", "resolution_corrected", "krs", "basis")
FAILED = "수신 실패"
INF = float("inf")
BANDS = ((0, 10, "0~10 km"), (10, 30, "10~30 km"), (30, 60, "30~60 km"), (60, INF, "60 km 이상"))
Station = Mapping[str, Any]


def merge_geo(audit: Sequence[Station], geo: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    """점검 결과에 해안선 거리·섬 여부·해상도 보정 표시를 붙인다. 수신 실패 지점은 그대로 둔다."""
    out = []
    for s in audit:
        d = dict(s)
        g = geo.get(str(s["stn_id"]))
        if g is not None and not s.get("error"):
            d.update(coast_km=g["coast_km"], island=g["island"], resolution_corrected=bool(g["corrected"]))
        out.append(d)
    return out


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(round(v, 6))
    return str(v)


def _flat(s: Station) -> dict:
    if s.get("error"):
        return {"stn_id": s["stn_id"], "name": s.get("name", ""), "klass": FAILED}
    e = s.get("eto") or {}
    season = s.get("krs_season") or {}
    d = dict(s)
    d.update(
        krs_3_5=season.get("3~5"),
        krs_6_8=season.get("6~8"),
        krs_9_11=season.get("9~11"),
        krs_12_2=season.get("12~2"),
        eto_n=e.get("n"),
        eto_bias_016=e.get("bias_016"),
        eto_mae_016=e.get("mae_016"),
        eto_bias_019=e.get("bias_019"),
        eto_mae_019=e.get("mae_019"),
        eto_best_krs=e.get("best_krs"),
    )
    return d


def to_csv(stations: Sequence[Station]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for s in stations:
        f = _flat(s)
        w.writerow([_cell(f.get(c)) for c in CSV_COLUMNS])
    return buf.getvalue()


def coastal_csv(stations: Sequence[Station], basis: str) -> str:
    """지점별 kRs 목록(`data/coastal_stations.csv`): 해안선 거리 10 km 이하(같으면 포함) 0.19, 아니면 0.16.
    거리가 있는 지점만 넣는다(수신 실패 지점은 목록에 없으면 내륙 0.16으로 읽힌다). basis = 점검 기준 날짜."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator=chr(10))
    w.writerow(COASTAL_CSV_COLUMNS)
    for s in stations:
        if s.get("error") or s.get("coast_km") is None:
            continue
        krs = KRS_COASTAL if s["coast_km"] <= COASTAL_MAX_KM else KRS_INLAND
        w.writerow(
            [
                s["stn_id"],
                s.get("name", ""),
                _cell(s["coast_km"]),
                _cell(bool(s.get("island"))),
                _cell(bool(s.get("resolution_corrected"))),
                krs,
                basis,
            ]
        )
    return buf.getvalue()


def _normal(stations: Sequence[Station]) -> list[Station]:
    """실측 kRs·거리가 있는 정상 지점"""
    return [
        s
        for s in stations
        if s.get("klass") == CLASS_NORMAL
        and s.get("krs_median") is not None
        and s.get("coast_km") is not None
    ]


def _summ(values: list[float]) -> dict:
    if not values:
        return dict(n=0, median=None, q1=None, q3=None)
    a = np.asarray(values, float)
    q1, med, q3 = (float(x) for x in np.percentile(a, [25, 50, 75]))
    return dict(n=len(a), median=med, q1=q1, q3=q3)


def krs_groups(stations: Sequence[Station]) -> dict:
    """정상 지점의 실측 kRs(지점 중앙값)를 거리 구간·섬/내륙·계절별로 요약한다."""
    ok = _normal(stations)
    bands = [
        dict(label=label, **_summ([s["krs_median"] for s in ok if lo <= s["coast_km"] < hi]))
        for lo, hi, label in BANDS
    ]
    seasonal = {
        k: _summ([s["krs_season"][k] for s in stations if (s.get("krs_season") or {}).get(k) is not None])
        for k in SEASONS
    }
    return dict(
        overall=_summ([s["krs_median"] for s in ok]),
        island=_summ([s["krs_median"] for s in ok if s.get("island")]),
        inland=_summ([s["krs_median"] for s in ok if not s.get("island")]),
        bands=bands,
        seasonal=seasonal,
    )


# ── 관계 그림 (SVG) ──────────────────────────────────────────────────────
def svg_scatter(stations: Sequence[Station], candidates: Sequence[float] = DEFAULT_CANDIDATES_KM) -> str:
    """가로 해안선 거리(km, 로그 눈금), 세로 실측 kRs 중앙값, 세로선은 사분위 범위. 내륙 ●, 섬 ◆."""
    ok = _normal(stations)
    w, h, ml, mr, mt, mb = 820, 500, 70, 30, 70, 60
    xmax = max([120.0] + [s["coast_km"] for s in ok])
    ymin = min([0.14] + [s["krs_q1"] for s in ok if s.get("krs_q1") is not None]) - 0.005
    ymax = max([0.24] + [s["krs_q3"] for s in ok if s.get("krs_q3") is not None]) + 0.005
    lx = float(np.log10(xmax + 1))

    def px(km: float) -> float:
        return ml + (w - ml - mr) * float(np.log10(km + 1)) / lx

    def py(v: float) -> float:
        return mt + (h - mt - mb) * (ymax - v) / (ymax - ymin)

    cx, cy = (ml + w - mr) / 2, (mt + h - mb) / 2
    sub = "점 = 지점 중앙값, 세로선 = 사분위 범위(Q1~Q3). 회색 띠 = 해안선 해상도 한계(10 km 미만 거리는 믿기 어려움)"
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        'font-family="sans-serif" font-size="12">',
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        f'<text x="{ml}" y="24" font-size="15" font-weight="bold">'
        f"해안선 거리와 실측 kRs (정상 지점 {len(ok)}곳)</text>",
        f'<text x="{ml}" y="44" fill="#444444">{sub}</text>',
        f'<rect x="{px(0):.1f}" y="{mt}" width="{px(10) - px(0):.1f}" height="{h - mt - mb}" fill="#eeeeee"/>',
    ]
    for v in np.arange(np.ceil(ymin * 50) / 50, ymax, 0.02):  # 0.02 간격 눈금
        y = py(float(v))
        out.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{w - mr}" y2="{y:.1f}" stroke="#dddddd"/>')
        out.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end">{float(v):.2f}</text>')
    for km in (0, 1, 3, 10, 30, 100):
        if km <= xmax:
            x = px(km)
            out.append(f'<line x1="{x:.1f}" y1="{h - mb}" x2="{x:.1f}" y2="{h - mb + 5}" stroke="#333333"/>')
            out.append(f'<text x="{x:.1f}" y="{h - mb + 20}" text-anchor="middle">{km}</text>')
    out.append(f'<text x="{cx:.0f}" y="{h - 14}" text-anchor="middle">해안선까지 거리 (km, 로그 눈금)</text>')
    out.append(
        f'<text x="18" y="{cy:.0f}" transform="rotate(-90 18 {cy:.0f})" text-anchor="middle">'
        "실측 kRs (중앙값)</text>"
    )
    for v, label in ((KRS_LOW, "0.16 (내륙)"), (KRS_HIGH, "0.19 (해안)")):
        y = py(v)
        out.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{w - mr}" y2="{y:.1f}" stroke="#777777" stroke-dasharray="6 4"/>'
        )
        out.append(f'<text x="{w - mr - 4}" y="{y - 4:.1f}" text-anchor="end" fill="#555555">{label}</text>')
    for c in candidates:
        x = px(c)
        out.append(
            f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{h - mb}" stroke="#999999" stroke-dasharray="2 4"/>'
        )
        out.append(f'<text x="{x:.1f}" y="{mt - 4}" text-anchor="middle" fill="#555555">{c:g} km</text>')
    for s in sorted(ok, key=lambda t: str(t["stn_id"])):
        x, y = px(s["coast_km"]), py(s["krs_median"])
        if s.get("krs_q1") is not None and s.get("krs_q3") is not None:
            y1, y3 = py(s["krs_q1"]), py(s["krs_q3"])
            out.append(f'<line x1="{x:.1f}" y1="{y1:.1f}" x2="{x:.1f}" y2="{y3:.1f}" stroke="#bbbbbb"/>')
        tip = escape(f"{s.get('name', '')} {s['stn_id']}: {s['coast_km']:.1f} km, kRs {s['krs_median']:.3f}")
        if s.get("island"):
            pts = f"{x:.1f},{y - 6:.1f} {x + 6:.1f},{y:.1f} {x:.1f},{y + 6:.1f} {x - 6:.1f},{y:.1f}"
            out.append(
                f'<polygon class="island" points="{pts}" fill="#d95f02"><title>{tip}</title></polygon>'
            )
        else:
            out.append(
                f'<circle class="inland" cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#1b6ca8"><title>{tip}</title></circle>'
            )
    lg = w - mr - 200
    out.append(f'<circle cx="{lg}" cy="22" r="4" fill="#1b6ca8"/><text x="{lg + 10}" y="26">내륙</text>')
    out.append(
        f'<polygon points="{lg + 70},16 {lg + 76},22 {lg + 70},28 {lg + 64},22" fill="#d95f02"/>'
        f'<text x="{lg + 84}" y="26">섬</text>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ── 보고서 (md) ──────────────────────────────────────────────────────────
def _f(v: Any, n: int = 3) -> str:
    return "-" if v is None else f"{v:.{n}f}"


def _table(head: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    return lines + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]


def _names(rows: Sequence[Station]) -> str:
    return ", ".join(f"{s['name']}({s['stn_id']})" for s in rows) or "없음"


def _proposal(comparison: Mapping[str, Any], cand_min: float) -> list[str]:
    best, b1, b2 = comparison.get("best"), comparison.get("best_h1"), comparison.get("best_h2")
    if not best:
        return ["- 비교할 정상 지점이 없다."]
    rows = comparison["rows"]
    base = next(r for r in rows if r["label"] == "전부 0.16")
    oracle = next(r for r in rows if r["label"].startswith("지점별 최적"))
    out = []
    if comparison.get("meaningless"):
        out.append(
            f"- **구분이 무의미하다.** 가장 좋은 기준({best['label']}, MAE {_f(best['mean_mae'])})과 "
            f"'전부 0.16'(MAE {_f(base['mean_mae'])})의 차이가 {_f(best['gain_vs_all_016'])} mm/d로 "
            f"{MEANINGLESS_GAIN} 미만이다. 단순한 기준인 '전부 0.16 유지'를 권한다."
        )
    else:
        room = base["mean_mae"] - oracle["mean_mae"]
        share = (
            f", 지점별 최적(이론값 {_f(oracle['mean_mae'])})이 줄일 수 있는 {room:.3f}의 " if room > 0 else ""
        )
        share += f"{best['gain_vs_all_016'] / room * 100:.0f}%" if room > 0 else ""
        out.append(
            f"- **제안: {best['label']}** → 지점 평균 MAE {_f(best['mean_mae'])} mm/d, "
            f"'전부 0.16'({_f(base['mean_mae'])})보다 {_f(best['gain_vs_all_016'])} 작다{share}."
        )
    if b1 and b2:
        same = " (같은 기준)." if b1["label"] == b2["label"] else " (다름: 한 해 자료의 한계)."
        out.append(f"- 상반기(1~6월) 최선: {b1['label']}, 하반기(7~12월) 최선: {b2['label']}{same}")
    if best["label"].startswith(f"거리 ≤ {cand_min:g} km"):
        out.append(
            f"- 제안 기준이 **가장 작은 후보({cand_min:g} km)**다. 더 가까운 거리에서 더 좋을 수 있으나 해안선 해상도 한계로 "
            f"{cand_min:g} km 미만은 구분할 수 없어 후보를 더 줄여 비교하지 않았다."
        )
    return out


def markdown_report(
    stations: Sequence[Station],
    comparison: Mapping[str, Any],
    meta: Mapping[str, Any],
    stamp: str,
    sources: str = "",
) -> str:
    ok = [s for s in stations if not s.get("error")]
    failed = [s for s in stations if s.get("error")]
    g = krs_groups(stations)
    cand_min = min(DEFAULT_CANDIDATES_KM)
    count = {c: sum(1 for s in ok if s["klass"] == c) for c in CLASSES}
    head = (
        f"- 자료: 기상청 ASOS 일자료 {meta['start']} ~ {meta['end']}({meta['days']}일), 지점 {meta['stations']}곳, "
        f"호출(재시도 포함) {meta['calls']}번. 해안선·땅: Natural Earth 10m(퍼블릭 도메인, Made with Natural Earth). "
        f"{sources}"
    ).rstrip()
    L: list[str] = [
        f"# 지점별 일사 실태표와 해안(kRs) 경계 후보 ({stamp})",
        "",
        f"- 파일: `station_radiation_{stamp}.csv`(지점별 표), `radiation_kRs_vs_coast_{stamp}.svg`(관계 그림). 결정 0021.",
        head,
        "",
        "## 1. 분류",
        "",
    ]
    n = MIN_VALID_DAYS
    L += _table(
        ["분류", "지점 수", "기준"],
        [
            (CLASSES[0], count[CLASSES[0]], f"유효 일사 {n}일 이상"),
            (CLASSES[1], count[CLASSES[1]], f"일사 값 {n}일 이상인데 유효 {n}일 미만"),
            (CLASSES[2], count[CLASSES[2]], f"일사 값 {n}일 미만, 일조 {n}일 이상"),
            (CLASSES[3], count[CLASSES[3]], f"일사·일조 모두 {n}일 미만"),
            (FAILED, len(failed), "ASOS를 받지 못함"),
        ],
    )
    sparse = [s for s in ok if s.get("rs_sparse")]
    L += [
        "",
        f"- 일사 값이 1~{n - 1}일뿐인 지점(`rs_sparse`): {len(sparse)}곳. "
        + ", ".join(f"{s['name']}({s['rs_values']}일)" for s in sparse),
        "",
    ]
    no_rs = [s for s in ok if s["klass"] != CLASS_NORMAL]
    ss = [s["ss_days"] for s in no_rs]
    L += ["## 2. 일조시간 확인 (일사 값이 없는 지점)", ""]
    if no_rs:
        short = sum(1 for v in ss if v < n)
        L.append(
            f"- 일사를 못 쓰는 {len(no_rs)}곳의 일조시간 일수: 최소 {min(ss)}일, 중앙값 "
            f"{int(statistics.median(ss))}일, 최대 {max(ss)}일(조회 {meta['days']}일 중). 일조 {n}일 미만 지점 {short}곳."
        )
        L.append("- 지점: " + ", ".join(s["name"] for s in no_rs))
    L += ["", "## 3. 실측 kRs (정상 지점)", ""]
    row = lambda label, v: (label, v["n"], _f(v["median"]), _f(v["q1"]), _f(v["q3"]))  # noqa: E731
    L += _table(
        ["구분", "지점 수", "중앙값", "Q1", "Q3"],
        [row("전체", g["overall"])]
        + [row(f"계절 {k}월", v) for k, v in g["seasonal"].items()]
        + [row(b["label"], b) for b in g["bands"]]
        + [row("섬", g["island"]), row("내륙", g["inland"])],
    )
    L += [
        "",
        "- 값은 지점별 중앙값(또는 계절 중앙값)을 다시 요약한 것이다. 계절 중앙값은 그 계절 유효일이 "
        f"{MIN_SEASON_DAYS}일 이상일 때만 지점 값이 있다.",
        "",
        "## 4. 경계 후보 비교 (ETo 오차, mm/d)",
        "",
        "정상 지점마다 (가) 관측 일사 ETo와 일사를 뺀 기온차식 ETo((나) kRs 0.16, (다) 0.19)의 평균 절대 오차(MAE)를 "
        "구한 뒤, 기준을 만족하는 지점은 0.19, 아니면 0.16의 오차를 쓴 평균이다(지점 동일 가중). 거리 경계는 '이하'.",
        "",
    ]
    L += _table(
        ["기준", "지점 수", "0.19 지점", "평균 MAE", "일수 가중 MAE", "상반기", "하반기"],
        [
            (
                r["label"],
                r["n"],
                r["n_high"],
                _f(r["mean_mae"]),
                _f(r["weighted_mae"]),
                _f(r["mean_mae_h1"]),
                _f(r["mean_mae_h2"]),
            )
            for r in comparison["rows"]
        ],
    )
    L += ["", "### 제안 (승인 요청)", ""] + _proposal(comparison, cand_min)
    total = sum(s.get("suspect_sunshine_days", 0) for s in ok)
    L += [
        "",
        "## 5. 의심 일조 (규칙 없이 개수만)",
        "",
        f"- 일조 0.0시간 + 강수 없음 + 일사 무효·없음인 날의 합계: {total}일 "
        f"(지점 {len(ok)}곳, 지점당 평균 {total / max(len(ok), 1):.1f}일).",
        "",
    ]
    top = sorted(ok, key=lambda s: (-s.get("suspect_sunshine_days", 0), str(s["stn_id"])))[:10]
    L += _table(
        ["지점", "분류", "의심 일조 일수"],
        [(f"{s['stn_id']} {s['name']}", s["klass"], s.get("suspect_sunshine_days", 0)) for s in top],
    )
    corrected = [s for s in ok if s.get("resolution_corrected")]
    islands = [s for s in ok if s.get("island")]
    L += [
        "",
        "## 6. 해상도 한계와 보정",
        "",
        "- 해안선 자료(Natural Earth 10m)는 축척 1:1천만이라 위치 오차가 수 km이다. **10 km 미만 거리는 "
        "신뢰할 수 없으므로** 경계 후보는 10 km 이상부터 비교했다(5 km 제외).",
        f"- **해상도 보정**: 땅 다각형 밖에 찍혔지만 가장 가까운 땅까지 3 km 이내인 지점은 거리 0 km로 두고 "
        f"표시했다 — {len(corrected)}곳: {_names(corrected)}",
        f"- **섬**(지리적 섬, 다리로 이어진 섬 포함) {len(islands)}곳: {_names(islands)}",
        "- 정상 지점이 약 65곳·한 해 자료라 경계 선택이 과적합일 수 있다. ETo 오차는 일사·일조가 있는 날에서만 "
        "재므로, 일사·일조가 모두 없는 날의 진짜 오차는 알 수 없다.",
        "",
    ]
    return "\n".join(L) + "\n"
