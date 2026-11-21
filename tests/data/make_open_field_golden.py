"""노지(사과·배추) 물수지·전망 고정 값 만들기 (이슈 #128, 결정 0039).

시설 재배(is_protected) 갈래를 넣기 전 `main`(2db9fb0)의 코드로 같은 입력의 결과를 뽑아
`open_field_golden.txt`에 저장했다. `tests/test_water_protected.py`가 지금 코드의 결과를 이 값과 비교해,
노지 값이 한 자리도 안 바뀌었음을 보인다.
입력은 `tests/test_collector.py`의 가짜 API·가짜 시계·가짜 조건(사과 k3·k1, 배추 k1)이며 모두 가짜 값이다.

노지 계산을 일부러 바꿀 때 다시 뽑는 법 (저장소 루트에서):
    python tests/data/make_open_field_golden.py
그리고 바뀐 값이 의도한 것인지 `git diff tests/data/open_field_golden.txt`으로 확인한 뒤 같은 PR에 넣는다.
"""

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))  # tests/ (test_collector 의 가짜 API)
sys.path.insert(0, str(HERE))

import test_collector as T  # noqa: E402

GOLDEN = HERE / "open_field_golden.txt"
NL = chr(10)
TABLES = ("forecast_runs", "water_balance_daily", "water_outlook", "water_outlook_summary")


def cases() -> dict[str, dict]:
    """이름 → run_slot 입력. 아침 정시 · 어제 관측이 없는 지연일 · 저녁 회차"""
    evening_issues = T.ISSUES + [T.TS("2026-06-10 05:00") + pd.Timedelta(hours=3 * k) for k in range(5)]
    return {
        "morning": dict(start="2026-06-10 02:20"),
        "lag": dict(start="2026-06-10 02:20", asos_last=dt.date(2026, 6, 8)),
        "evening": dict(
            start="2026-06-10 17:20", issues=evening_issues, issue=T.TS("2026-06-10 17:00"), name="저녁"
        ),
    }


def load_golden() -> dict:
    """저장된 고정 값 → {경우: {표: 줄 목록}}"""
    out: dict = {}
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        case, table, rows = line.split(":", 2)
        out.setdefault(json.loads(case), {})[json.loads(table)] = json.loads(rows)
    return out


def snapshot(dicts=None, run=None) -> dict:
    """세 경우의 결과 줄 (JSON 으로 바꿔도 같은 값). dicts: 사전 자료(기본 test_collector._dicts)"""
    out = {}
    for name, kw in cases().items():
        kw = dict(kw)
        clock = T.FakeClock(kw.pop("start"))
        issue = kw.pop("issue", T.ISSUE)
        run_name = kw.pop("name", "아침")
        issues = kw.pop("issues", T.ISSUES)
        api = T.FakeApi(clock, issues, **kw)
        d = dicts() if dicts else T._dicts()
        r = T.W.run_slot(run_name, issue, d, T.KEY, http=api, clock=clock, stations=T.BOTH)
        out[name] = {t: json.loads(json.dumps(getattr(r, t), default=str)) for t in TABLES}
    return out


if __name__ == "__main__":
    snap = snapshot()
    # 표마다 한 줄(줄 하나가 한 표의 모든 줄) — git diff 로 어느 표가 바뀌었는지 보이게
    lines = [
        f"{json.dumps(c)}:{json.dumps(t)}:{json.dumps(snap[c][t], ensure_ascii=False, separators=(',', ':'))}"
        for c in sorted(snap)
        for t in sorted(snap[c])
    ]
    GOLDEN.write_text(NL.join(lines) + NL, encoding="utf-8")
    print(f"wrote {GOLDEN} ({GOLDEN.stat().st_size} bytes)")
