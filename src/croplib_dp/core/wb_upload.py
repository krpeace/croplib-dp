"""공개 물수지(`water_balance_daily`)에서 올릴 줄 고르기 (이슈 #139, 결정 0042). 계산은 바꾸지 않는다.

끝난 작기 = `scenarios.season_end`가 있고 14일 창 시작일보다 앞. 이런 조건은 지난 줄을 다시 쓰지 않는다:
작형의 단계 일수(`crop_seasons.l_*`)는 해가 달라도 하나라, 나중에 바꾸면 지난 값이 조용히 달라지기 때문이다.
작기 끝이 창 안에 있는 동안은 진행 중과 같게 올려, 늦게 온 관측·실패한 회차를 14일 안에 다시 채운다.
"""

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

DORMANT = "dormant"


def upload_rows(
    rows: Sequence[Mapping[str, Any]],
    season_end: Any,
    window_start: dt.date,
    window_only: bool,
) -> tuple[list, int]:
    """한 계산 조건의 물수지 줄(date, status) → (올릴 줄, 끝난 작기라서 뺀 비-dormant 줄 수).
    season_end: 날짜나 'YYYY-MM-DD'. 비어 있으면 진행 중으로 본다.
    window_only: 진행 중인 작기도 창 줄만 올리나(대표 밖 지점의 평소 회차).
    끝난 작기는 window_only 와 상관없이 창 줄만 올리고(모두 작기 끝 뒤), 그중 dormant 가 아닌 줄은
    단계 일수가 작기 끝보다 긴 것이므로 올리지 않고 수를 센다."""
    ended = bool(season_end) and dt.date.fromisoformat(str(season_end)[:10]) < window_start
    if not (ended or window_only):
        return list(rows), 0
    window = [r for r in rows if r["date"] >= window_start]
    if not ended:
        return window, 0
    kept = [r for r in window if r["status"] == DORMANT]
    return kept, len(window) - len(kept)
