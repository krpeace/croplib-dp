"""지난 해 ASOS 일자료 파일 캐시 (적산온도 수집기용).

지난 해 일자료는 바뀌지 않으므로, 받은 응답의 items 를 `<폴더>/asos/<지점>/<해>.json` 으로 남겨 두고
다음 실행에서는 올해만 받는다.
- 계산에 쓰는 칸(KEEP_FIELDS)만 남긴다. 응답은 한 줄에 칸이 60개 가까이라
  그대로 두면 97곳 × 10해로 수백 MB 가 된다.
- 다 받은 해만 남기는 것은 부르는 쪽이 `is_complete` 로 가린다
  (1/1 실행처럼 12/31 관측이 아직 없는 해를 막으려고).
- 캐시는 속도를 위한 것이라 실패의 이유가 되지 않는다: 읽을 수 없는 파일(깨진 JSON 등)은 '없음'으로 보고,
  쓰지 못해도(임시 파일 정리까지) 오류를 내지 않는다.
"""

import datetime as dt
import json
import os
from pathlib import Path

KEEP_FIELDS = ("tm", "avgTa", "maxTa", "minTa", "sumGsr")  # collectors/heat_sun.build_station 이 읽는 칸


def is_complete(items: list[dict], year: int) -> bool:
    """캐시해도 되는 해인가: 마지막 날짜가 12/31 이거나, 그해 관측이 아예 없을 때(nodata, 빈 목록)"""
    if not items:
        return True
    for it in items:
        try:
            if dt.date.fromisoformat(str(it.get("tm", ""))[:10]) == dt.date(year, 12, 31):
                return True
        except ValueError:
            continue
    return False


class AsosCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, stn_id: str, year: int) -> Path:
        return self.root / "asos" / str(stn_id) / f"{year}.json"

    def get(self, stn_id: str, year: int) -> list[dict] | None:
        """캐시된 items. 없거나 깨져 있으면 None (빈 목록은 'nodata 로 이미 물어봄'이라 None 과 다르다)"""
        try:
            items = json.loads(self.path(stn_id, year).read_text(encoding="utf-8"))
        except (OSError, ValueError):  # 파일 없음·읽기 오류, JSON 오류·글자 깨짐(UnicodeDecodeError 포함)
            return None
        if not isinstance(items, list) or not all(isinstance(it, dict) for it in items):
            return None
        return items

    def put(self, stn_id: str, year: int, items: list[dict]) -> None:
        """계산에 쓰는 칸만 남긴다. 쓰는 도중 끊겨도 반쪽 파일이 남지 않게 임시 파일에 쓴 뒤 바꾼다.
        못 써도 넘어간다."""
        p = self.path(stn_id, year)
        tmp = p.with_name(p.name + ".tmp")
        slim = [{k: it[k] for k in KEEP_FIELDS if k in it} for it in items]
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(slim, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            try:  # missing_ok 는 '파일 없음'만 막는다 (리눅스는 '폴더가 아님'도 난다)
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
