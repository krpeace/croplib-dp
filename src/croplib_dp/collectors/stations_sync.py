"""기상청 ASOS 지점 목록 현행화 자료 만들기 (이슈 #27, 결정 0016). 읽기·파일 만들기만 한다.

기상청 API허브 지점정보(stn_inf, 지상 SFC)를 받아
- `stations_kma_YYYYMMDD.csv`: 지점번호 순 지점 사전 (폐지·휴지 지점은 is_active=false)
- `stations_diff.md`: 화면 시안 96곳·DB 지점 사전 10곳과 비교한 표
를 만든다. DB에는 쓰지 않는다 (DB 구조의 주인은 CropMon, 반영은 CropMon#38).

- 지금 운영 중인 목록 = 오늘 기준 조회.
- 폐지·휴지 지점 = 지난 해마다 1월 1일 기준 목록에는 있고 지금은 없는 지점.
- 한 번이라도 받지 못하거나 형식이 이상하면 파일을 쓰지 않고 실패(종료 코드 1)로 끝낸다.
실행: python -m croplib_dp.collectors.stations_sync [--out docs/data] [--date 2026-10-28] [--since 2000]
"""

import argparse
import datetime as dt
import sys
from collections.abc import Callable
from pathlib import Path

from croplib_dp.collectors.water import _env
from croplib_dp.core import stations as st
from croplib_dp.io import kma_hub
from croplib_dp.io.kma import Http, http_get

KST = dt.timezone(dt.timedelta(hours=9))
SINCE_YEAR = 2000


def collect(
    key: str, day: dt.date, since: int, http: Http, sido_lookup: Callable[[float, float], str] | None
) -> list[dict]:
    """지금 목록 + 지난 해마다 1월 1일 목록 → 지점 줄. 실패하면 HubError/StationDataError"""
    current = kma_hub.fetch_stn_inf(key, f"{day:%Y%m%d}0000", http=http)
    print(f"받음: {day} 기준 {len(current)}곳")
    history: dict[str, list[dict]] = {}
    for y in range(since, day.year + 1):
        d = dt.date(y, 1, 1)
        if d >= day:
            continue
        history[d.isoformat()] = kma_hub.fetch_stn_inf(key, f"{d:%Y%m%d}0000", http=http)
        print(f"받음: {d} 기준 {len(history[d.isoformat()])}곳")
    return st.build_rows(current, history, db_stations=st.DB_STATIONS, sido_lookup=sido_lookup)


def main(
    argv: list[str] | None = None,
    key: str | None = None,
    http: Http | None = None,
    sido_lookup: Callable[[float, float], str] | None = None,
) -> int:
    p = argparse.ArgumentParser(description="기상청 ASOS 지점 목록 현행화 자료 (CSV + 비교표, DB 쓰기 없음)")
    p.add_argument("--out", default="docs/data", help="결과 폴더")
    p.add_argument("--date", help="기준 날짜 YYYY-MM-DD (없으면 오늘, 한국 날짜)")
    p.add_argument("--since", type=int, default=SINCE_YEAR, help="폐지 지점을 찾을 첫 해 (1월 1일 기준)")
    a = p.parse_args(argv)
    day = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(KST).date()
    key = key or _env("KMA_HUB")
    try:
        rows = collect(key, day, a.since, http or http_get, sido_lookup or kma_hub.reverse_sido)
    except (kma_hub.HubError, st.StationDataError) as e:
        print(f"실패: {kma_hub.mask_key(str(e))}")
        return 1
    probs = st.check_rows(rows)
    if probs:
        print("실패: 검사에 걸림")
        for x in probs[:20]:
            print(f"! {x}")
        return 1
    d = st.diff(rows)
    c = st.counts(rows)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"stations_kma_{day:%Y%m%d}.csv"
    md_path = out / "stations_diff.md"
    csv_path.write_text(st.to_csv_text(rows), encoding="utf-8")
    md_path.write_text(st.diff_markdown(d, day.isoformat(), c), encoding="utf-8")
    print(
        f"기상청 현행 {c['kma_active']}곳 (폐지·휴지 {c['kma_closed']}곳) / 시안 {c['draft']}곳 / "
        f"기상청에만 {len(d['missing'])}곳 / 시안·DB에만 {len(d['gone'])}곳 / "
        f"이름·번호 다름 {len(d['renamed'])}곳"
    )
    print(f"결과: {csv_path}, {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
