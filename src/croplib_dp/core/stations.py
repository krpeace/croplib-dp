"""기상청 ASOS 지점 목록 현행화 (이슈 #27): 지점 줄 만들기, 시도 약칭, 검사, 기준 목록과 비교.

바깥(API·파일·DB)을 모르는 순수 함수만 둔다.
기상청 응답 해석은 io/kma_hub.py, 실행은 collectors/stations_sync.py.
- 지점 줄은 COLUMNS 순서의 dict. 모르는 값은 None(CSV 빈 칸). 추측해서 채우지 않는다.
- 이상한 자료(번호 중복, 시도를 정하지 못함)는 조용히 넘기지 않고 StationDataError로 멈춘다.
"""

import csv
import io
import re
from collections.abc import Callable, Iterable

COLUMNS = (
    "stn_id",
    "name_ko",
    "sido",
    "lat",
    "lon",
    "elev_m",
    "wind_height_m",
    "measures_sunlight",
    "is_active",
    "source_note",
)

# 시·도 약칭 (CSV에는 이것만 쓴다)
SIDO_SHORT = (
    "서울",
    "부산",
    "대구",
    "인천",
    "광주",
    "대전",
    "울산",
    "세종",
    "경기",
    "강원",
    "충북",
    "충남",
    "전북",
    "전남",
    "경북",
    "경남",
    "제주",
)

# 법정동코드 앞 2자리 → 시·도 약칭.
# 강원(42→51, 2023)·전북(45→52, 2024)은 특별자치도가 되며 번호가 바뀌어 둘 다 둔다.
SIDO_BY_LAW = {
    "11": "서울",
    "26": "부산",
    "27": "대구",
    "28": "인천",
    "29": "광주",
    "30": "대전",
    "31": "울산",
    "36": "세종",
    "41": "경기",
    "42": "강원",
    "51": "강원",
    "43": "충북",
    "44": "충남",
    "45": "전북",
    "52": "전북",
    "46": "전남",
    "47": "경북",
    "48": "경남",
    "50": "제주",
}

# 긴 이름 → 약칭 (좌표 → 행정구역 결과 등)
_SIDO_FULL = {
    "서울특별시": "서울",
    "부산광역시": "부산",
    "대구광역시": "대구",
    "인천광역시": "인천",
    "광주광역시": "광주",
    "대전광역시": "대전",
    "울산광역시": "울산",
    "세종특별자치시": "세종",
    "경기도": "경기",
    "강원도": "강원",
    "강원특별자치도": "강원",
    "충청북도": "충북",
    "충청남도": "충남",
    "전라북도": "전북",
    "전북특별자치도": "전북",
    "전라남도": "전남",
    "경상북도": "경북",
    "경상남도": "경남",
    "제주도": "제주",
    "제주특별자치도": "제주",
}

# 비교 기준 1: 화면 시안의 지점 96곳 (이슈 #27 본문)
DRAFT_96 = (
    90, 93, 95, 98, 99, 100, 101, 102, 104, 105, 106, 108, 112, 114, 115, 119, 121, 127, 129, 130,
    131, 133, 135, 136, 137, 138, 140, 143, 146, 152, 155, 156, 159, 162, 165, 168, 169, 170, 172, 174,
    175, 177, 184, 185, 188, 189, 192, 201, 202, 203, 211, 212, 216, 217, 221, 226, 232, 235, 236, 238,
    239, 243, 244, 245, 247, 248, 251, 252, 253, 254, 255, 257, 258, 259, 260, 261, 262, 263, 264, 266,
    268, 271, 272, 273, 276, 277, 278, 279, 281, 283, 284, 285, 288, 289, 294, 295,
)  # fmt: skip

# 비교 기준 2: DB 지점 사전 10곳 = CropMon `src/infra/stations.ts` (번호: 이름, 일사 관측 여부)
DB_STATIONS: dict[int, tuple[str, bool | None]] = {
    101: ("춘천", True),
    216: ("태백", False),
    119: ("수원", True),
    131: ("청주", True),
    129: ("서산", True),
    146: ("전주", True),
    156: ("광주", True),
    136: ("안동", True),
    192: ("진주", True),
    189: ("서귀포", True),
}

MISSING_BELOW = -99.0  # 기상청 결측 표시(-999 등). 이보다 작으면 빈 칸


class StationDataError(ValueError):
    """지점 자료가 이상해서 결과를 만들 수 없음 (조용히 넘어가지 않는다)"""


def sido_short(name: str) -> str:
    """시·도 이름(긴 이름·약칭) → 약칭. 모르는 이름이면 ValueError"""
    n = (name or "").strip()
    if n in SIDO_SHORT:
        return n
    if n in _SIDO_FULL:
        return _SIDO_FULL[n]
    raise ValueError(f"모르는 시·도 이름: {name!r}")


def sido_from_law_id(law_id: str | None) -> str | None:
    """법정동코드(10자리) → 시·도 약칭. 코드가 없거나 모르면 None"""
    s = (law_id or "").strip()
    if len(s) < 2 or not s[:2].isdigit():
        return None
    return SIDO_BY_LAW.get(s[:2])


def sido_from_addr(addr: str | None) -> str | None:
    """법정동주소(LAW_ADDR) → 시·도 약칭. '(산지)' 말머리와 '전남광주통합특별시' 표기도 처리한다"""
    toks = re.sub(r"^\([^)]*\)\s*", "", (addr or "").strip()).split()
    if not toks:
        return None
    first = toks[0]
    if first in _SIDO_FULL:
        return _SIDO_FULL[first]
    if first == "전남광주통합특별시":
        return "광주" if len(toks) > 1 and toks[1].endswith("구") else "전남"
    return None


def _num(v: str | None) -> float | None:
    s = (v or "").strip()
    if s in ("", "-", "--", "----"):
        return None
    x = float(s)
    return None if x < MISSING_BELOW else x


def _station(raw: dict, sido_lookup: Callable[[float, float], str] | None) -> dict:
    """응답 한 줄(dict) → 지점 줄 (is_active·일사 여부는 build_rows가 채움)"""
    stn = int(raw["STN_ID"])
    lat, lon = float(raw["LAT"]), float(raw["LON"])
    notes = ["기상청 API허브 지점정보(stn_inf SFC)"]
    sido = sido_from_law_id(raw.get("LAW_ID"))
    if sido:
        notes.append("시도: 법정동코드 앞 2자리")
    else:
        sido = sido_from_addr(raw.get("LAW_ADDR"))
        if sido:
            notes.append("시도: 법정동주소(LAW_ADDR)")
        else:
            if sido_lookup is None:
                raise StationDataError(
                    f"{stn} {raw.get('STN_KO', '')}: 법정동코드가 없고 좌표로 찾을 방법도 없음"
                )
            try:
                sido = sido_short(sido_lookup(lat, lon))
            except Exception as e:  # 좌표로도 못 찾으면 멈춘다
                raise StationDataError(f"{stn} {raw.get('STN_KO', '')}: 좌표로 시도를 찾지 못함 ({e})") from e
            notes.append("시도: 좌표→행정구역(OpenStreetMap)")
    return dict(
        stn_id=stn,
        name_ko=(raw.get("STN_KO") or "").strip(),
        sido=sido,
        lat=lat,
        lon=lon,
        elev_m=_num(raw.get("HT")),
        wind_height_m=_num(raw.get("HT_WD")),
        measures_sunlight=None,
        is_active=True,
        source_note=notes,
    )


def _unique(raws: list[dict], label: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in raws:
        stn = int(r["STN_ID"])
        if stn in out:
            raise StationDataError(f"{label} 목록에 지점번호 {stn}이 두 번 있음")
        out[stn] = r
    return out


def build_rows(
    current: list[dict],
    history: dict[str, list[dict]],
    db_stations: dict[int, tuple[str, bool | None]] = DB_STATIONS,
    sido_lookup: Callable[[float, float], str] | None = None,
) -> list[dict]:
    """지금 목록 + 지난 날짜 목록들 → 지점 줄 (지점번호 순).

    current: 지금 기준 응답 줄. history: {"YYYY-MM-DD": 그날 기준 응답 줄}.
    지난 목록에만 있는 지점은 is_active=False로 남기고, 마지막으로 보인 날짜를 적는다.
    일사 관측 여부는 DB 사전(db_stations)에 있는 지점만 채우고 나머지는 None.
    """
    if not current:
        raise StationDataError("지금 기준 지점 목록이 비어 있음")
    now = _unique(current, "지금")
    rows = {stn: _station(r, sido_lookup) for stn, r in now.items()}
    last_seen: dict[int, tuple[str, dict]] = {}
    for day in sorted(history):
        for stn, r in _unique(history[day], day).items():
            if stn not in now:
                last_seen[stn] = (day, r)
    for stn, (day, r) in last_seen.items():
        row = _station(r, sido_lookup)
        row["is_active"] = False
        row["source_note"].append(f"폐지·휴지: {day} 기준 목록에 있었고 지금 목록에 없음")
        rows[stn] = row
    for stn, row in rows.items():
        if stn in db_stations and db_stations[stn][1] is not None:
            row["measures_sunlight"] = db_stations[stn][1]
            row["source_note"].append("일사: CropMon 지점 사전(stations.ts)")
        row["source_note"] = "; ".join(row["source_note"])
    return [{c: rows[k][c] for c in COLUMNS} for k in sorted(rows)]


def check_rows(rows: list[dict]) -> list[str]:
    """CSV로 쓰기 전 검사. 문제 문구 목록(비어 있으면 통과)"""
    probs: list[str] = []
    seen: set[int] = set()
    for r in rows:
        if tuple(r) != COLUMNS:
            probs.append(f"{r.get('stn_id')}: 열 이름·순서가 다름 {tuple(r)}")
            continue
        stn = r["stn_id"]
        if stn in seen:
            probs.append(f"지점번호 {stn} 중복")
        seen.add(stn)
        if r["sido"] not in SIDO_SHORT:
            probs.append(f"{stn}: 시도가 약칭이 아님 ({r['sido']!r})")
        if not r["name_ko"]:
            probs.append(f"{stn}: 이름이 비어 있음")
        if not (32.0 <= r["lat"] <= 39.0 and 124.0 <= r["lon"] <= 132.0):
            probs.append(f"{stn}: 좌표가 우리나라 밖 ({r['lat']}, {r['lon']})")
        if not isinstance(r["is_active"], bool):
            probs.append(f"{stn}: is_active가 참·거짓이 아님")
        if r["measures_sunlight"] not in (True, False, None):
            probs.append(f"{stn}: measures_sunlight가 참·거짓·빈 칸이 아님")
    ids = [r.get("stn_id") for r in rows]
    if ids != sorted(ids, key=lambda x: (x is None, x)):
        probs.append("지점번호 순서가 아님")
    return probs


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def to_csv_text(rows: list[dict]) -> str:
    """지점 줄 → CSV 글 (UTF-8로 저장, 첫 줄은 열 이름)"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([_cell(r[c]) for c in COLUMNS])
    return buf.getvalue()


def diff(
    rows: list[dict],
    draft_ids: Iterable[int] = DRAFT_96,
    db_stations: dict[int, tuple[str, bool | None]] = DB_STATIONS,
) -> dict:
    """기준 목록(시안·DB)과 비교.

    missing (가): 기상청 현행인데 시안에도 DB에도 없는 지점
    gone    (나): 시안·DB에 있는데 기상청 현행 목록에 없는 지점
                  (kma: closed=폐지·휴지로 확인, absent=기상청 목록에 없음)
    renamed (다): 같은 번호인데 이름이 다르거나, 같은 이름이 다른 번호인 지점 (이름이 있는 DB 사전만 비교)
    """
    draft = set(draft_ids)
    ref = draft | set(db_stations)
    by_id = {r["stn_id"]: r for r in rows}
    active = {k: r for k, r in by_id.items() if r["is_active"]}
    missing = [dict(r, in_draft=False, in_db=False) for k, r in sorted(active.items()) if k not in ref]
    gone = []
    for k in sorted(ref - set(active)):
        r = by_id.get(k)
        gone.append(
            dict(
                stn_id=k,
                in_draft=k in draft,
                in_db=k in db_stations,
                kma="closed" if r else "absent",
                name_ko=(r["name_ko"] if r else db_stations.get(k, ("",))[0]),
                note=(r["source_note"] if r else ""),
            )
        )
    renamed = []
    active_by_name = {r["name_ko"]: k for k, r in active.items()}
    for k in sorted(db_stations):
        name = db_stations[k][0]
        if k in active and active[k]["name_ko"] != name:
            renamed.append(dict(stn_id=k, ref_name=name, kma_name=active[k]["name_ko"], kma_id=k))
        elif k not in active and name in active_by_name:
            renamed.append(dict(stn_id=k, ref_name=name, kma_name=name, kma_id=active_by_name[name]))
    return dict(missing=missing, gone=gone, renamed=renamed)


def counts(rows: list[dict], draft_ids: Iterable[int] = DRAFT_96) -> dict:
    return dict(
        kma_active=sum(1 for r in rows if r["is_active"]),
        kma_closed=sum(1 for r in rows if not r["is_active"]),
        draft=len(set(draft_ids)),
        db=len(DB_STATIONS),
    )


def _ox(b: bool) -> str:
    return "○" if b else "×"


def diff_markdown(d: dict, run_date: str, counts: dict) -> str:
    """비교표 (stations_diff.md)"""
    c = counts
    L = [
        f"# 기상청 ASOS 지점과 시안·DB 지점 비교 ({run_date})",
        "",
        "만든 방법: `python -m croplib_dp.collectors.stations_sync` (이슈 #27, 결정 0016). "
        "기상청 API허브 지점정보(stn_inf, 지상 SFC)를 읽기만 했다.",
        "",
        f"- 기상청 전체 지점: **{c['kma_active'] + c['kma_closed']}곳** "
        f"(현행 운영 **{c['kma_active']}곳** · 폐지·휴지 **{c['kma_closed']}곳**, "
        "폐지·휴지 지점은 CSV에 `is_active=false`로 남김)",
        f"- 화면 시안: **{c['draft']}곳**, DB 지점 사전(CropMon `stations.ts`): **{c['db']}곳**",
        f"- (가) 기상청에만 있는 지점 {len(d['missing'])}곳 · "
        f"(나) 시안·DB에만 있는 지점 {len(d['gone'])}곳 · "
        f"(다) 이름·번호가 다른 지점 {len(d['renamed'])}곳",
        "",
        "## (가) 기상청에는 있고 시안·DB에 없는 지점",
        "",
    ]
    if d["missing"]:
        L += ["| 번호 | 이름 | 시도 | 시안 | DB |", "|---:|---|---|:-:|:-:|"]
        L += [
            f"| {r['stn_id']} | {r['name_ko']} | {r['sido']} | {_ox(r['in_draft'])} | {_ox(r['in_db'])} |"
            for r in d["missing"]
        ]
    else:
        L.append("없음")
    L += ["", "## (나) 시안·DB에는 있고 기상청 현행 목록에는 없는 지점 (폐지·번호 변경)", ""]
    if d["gone"]:
        L += ["| 번호 | 이름 | 시안 | DB | 기상청 |", "|---:|---|:-:|:-:|---|"]
        for r in d["gone"]:
            if r["kma"] == "closed":
                k = "폐지·휴지 (" + r["note"].split("폐지·휴지: ")[-1].split(";")[0] + ")"
            else:
                k = "지점정보에 없음 (번호 확인 필요)"
            L.append(
                f"| {r['stn_id']} | {r['name_ko'] or '-'} | {_ox(r['in_draft'])} | {_ox(r['in_db'])} | {k} |"
            )
    else:
        L.append("없음")
    L += [
        "",
        "## (다) 이름·번호가 다른 지점",
        "",
        "시안 목록은 번호만 있어서, 이름이 있는 DB 지점 사전(10곳)만 비교했다.",
        "",
    ]
    if d["renamed"]:
        L += ["| 번호 (시안·DB) | 이름 (DB) | 기상청 번호 | 기상청 이름 |", "|---:|---|---:|---|"]
        L += [f"| {r['stn_id']} | {r['ref_name']} | {r['kma_id']} | {r['kma_name']} |" for r in d["renamed"]]
    else:
        L.append("없음")
    return "\n".join(L) + "\n"
