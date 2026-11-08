"""KREI 농업관측 수집기 2단계 파서 (순수 함수): 글자 좌표(pages.json.gz)에서 표·헤드라인 추출 및 정형화.

결정 기록: docs/decisions/0023-krei-parse.md
"""

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from croplib_dp.core.krei_check import (
    check_area_yield_prod,
    check_dist_sum,
    check_yoy_recalc,
)

PARSER_VERSION = "0.2.0"

# 정형 숫자 추출 대상 부류 (과일, 과채)
STRUCTURED_NUM_SCOPE = {"OVR0000000013", "OVR0000000031"}

# 준비(표 원형 + 헤드라인만) 대상 부류 (쌀, 콩, 감자)
PREP_SCOPE = {"OVR0000000070", "OVR0000000072", "OVR0000000021"}

CORE_TABLE_TYPES = {
    "production_outlook",
    "shipment_outlook",
    "monthly_price",
    "growth_survey",
}

SECTION_NAMES = ["가격동향", "생육동향", "생산전망", "저장의향", "출하전망", "가격전망"]
DIRECTION_KEYWORDS = ["증가", "감소", "비슷", "상승", "하락", "양호", "부진"]


@dataclass
class RawTable:
    page: int
    seq: int
    title: str
    unit: str | None
    header: list[Any]
    rows: list[list[str]]
    note: str | None
    source: str | None
    table_type: str
    check_status: str = "not_checked"  # pass, fail, not_checked
    check_log: list[dict[str, Any]] | None = None
    item_code: str | None = None
    item_label: str | None = None
    id: int | None = None


@dataclass
class SectionText:
    page: int
    section: str
    headline: str
    direction: str | None
    bullets: list[str] = field(default_factory=list)
    item_code: str | None = None
    item_label: str | None = None


@dataclass
class Observation:
    file_id: int
    metric: str
    basis: str
    value: float
    unit: str
    table_raw_id: int = 0
    table_seq: int = 1
    item_code: str | None = None
    item_label: str | None = None
    variety: str | None = None
    crop_year: int | None = None
    period_label: str | None = None
    period_start: dt.date | None = None
    period_end: dt.date | None = None


@dataclass
class SurveyDist:
    file_id: int
    aspect: str
    table_raw_id: int = 0
    table_seq: int = 1
    good: float | None = None
    same: float | None = None
    bad: float | None = None
    item_code: str | None = None
    item_label: str | None = None


@dataclass
class ParsedReport:
    file_id: int
    yyyy: int
    mm: int
    parser_version: str
    parse_status: str  # parsed, needs_review, skipped
    raw_tables: list[RawTable] = field(default_factory=list)
    section_texts: list[SectionText] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    survey_dists: list[SurveyDist] = field(default_factory=list)


def reconstruct_lines(words: list[list[Any]]) -> list[list[list[Any]]]:
    """글자 좌표 단어 목록 [[x0, top, x1, bottom, text]] 을 줄 단위로 재구성한다."""
    sorted_words = sorted(words, key=lambda w: (w[1], w[0]))
    lines: list[list[list[Any]]] = []
    for w in sorted_words:
        top = float(w[1])
        bottom = float(w[3])
        placed = False
        for line in reversed(lines[-5:]):
            ltop = min(float(lw[1]) for lw in line)
            lbot = max(float(lw[3]) for lw in line)
            overlap = min(bottom, lbot) - max(top, ltop)
            min_h = min(bottom - top, lbot - ltop)
            if overlap > 0.4 * min_h or abs(bottom - lbot) <= 3.0:
                line.append(w)
                placed = True
                break
        if not placed:
            lines.append([w])

    for line in lines:
        line.sort(key=lambda w: float(w[0]))
    lines.sort(key=lambda ln: min(float(w[1]) for w in ln))
    return lines


def build_item_dict(
    items: list[dict[str, Any]], ovr_item_code: str
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """해당 부류(ovr_item_code) 하위 level 3 품목 사전 구축.

    반환: (name_to_code, sorted_name_code_pairs)
    """
    sub_items = [it for it in items if str(it.get("prt_ovr_item_code")) == ovr_item_code]
    name_to_code: dict[str, str] = {}
    for it in sub_items:
        code = str(it["ovr_item_code"])
        name = str(it["item_name"]).strip()
        name_to_code[name] = code

    # 긴 이름부터 매칭되도록 정렬
    pairs = sorted(name_to_code.items(), key=lambda p: len(p[0]), reverse=True)
    return name_to_code, pairs


def resolve_item(
    text: str,
    default_item: tuple[str | None, str | None],
    item_pairs: list[tuple[str, str]],
) -> tuple[str | None, str | None]:
    """텍스트에서 품목명을 찾고 (item_code, item_label) 반환. 못 찾으면 default_item."""
    clean = text.replace(" ", "")

    # 특수 복합 품목명 우선 매칭
    aliases = [
        ("노지감귤", "감귤"),
        ("하우스감귤", "감귤"),
        ("일반토마토", "토마토"),
        ("대추형방울토마토", "토마토"),
        ("대추방울토마토", "토마토"),
        ("방울토마토", "토마토"),
        ("신선토마토", "토마토"),
        ("청양계풋고추", "풋고추"),
        ("오이맛고추", "풋고추"),
        ("백다다기오이", "오이"),
        ("취청오이", "오이"),
        ("쥬키니호박", "애호박"),
    ]
    for alias_label, target_name in aliases:
        if alias_label in clean:
            for name, code in item_pairs:
                if name == target_name:
                    return code, alias_label

    for name, code in item_pairs:
        if name in clean:
            return code, name

    return default_item


def classify_table_title(title: str) -> str:
    """표 제목 기반 table_type 분류 (§2-2 규칙)."""
    t = title.replace(" ", "")
    # 도매가격(상품) 단독이나 반입량 단독은 그래프
    if ("도매가격(상품)" in t or "반입량" in t) and "월별" not in t:
        return "graph"
    if "생산량전망" in t:
        return "production_outlook"
    if "출하량전망" in t:
        if "증감률" in t:
            return "other"
        return "shipment_outlook"
    if "월별도매가격" in t:
        return "monthly_price"
    if "생육상황" in t:
        if "품질" in t or "병해충" in t or "생리장해" in t:
            return "other"
        return "growth_survey"
    return "other"


def extract_direction(headline: str) -> str | None:
    """헤드라인 끝부분에서 방향(증가/감소/비슷/상승/하락/양호/부진) 추출."""
    h_clean = headline.strip()
    for kw in DIRECTION_KEYWORDS:
        if re.search(rf"{kw}\s*$", h_clean):
            return kw
    return None


def extract_section_texts(
    pages: list[dict[str, Any]],
    items: list[dict[str, Any]],
    ovr_item_code: str,
) -> list[SectionText]:
    """글자 좌표에서 섹션 헤드라인과 근거 문장(bullets) 추출."""
    _, item_pairs = build_item_dict(items, ovr_item_code)
    sections: list[SectionText] = []

    current_item: tuple[str | None, str | None] = (None, None)

    for pg_idx, pg in enumerate(pages, 1):
        words = pg.get("words", [])
        lines = reconstruct_lines(words)

        # 1. 쪽 최상단 큰 글씨 품목 제목 감지 (h >= 18)
        for ln in lines[:5]:
            large_words = [w for w in ln if (float(w[3]) - float(w[1])) >= 18.0]
            if large_words:
                ltxt = "".join(str(w[4]) for w in large_words)
                current_item = resolve_item(ltxt, current_item, item_pairs)

        i = 0
        while i < len(lines):
            ln = lines[i]
            # 섹션 이름은 왼쪽 열(x0 < 120)에 위치
            left_words = [w for w in ln if float(w[0]) < 120.0]
            left_text = "".join(str(w[4]) for w in left_words).replace(" ", "")

            matched_sec: str | None = None
            for s in SECTION_NAMES:
                if s in left_text:
                    matched_sec = s
                    break

            # 상단 배너(여러 섹션이 한 줄에 나열된 경우) 제외
            right_words = [w for w in ln if float(w[0]) >= 120.0]
            right_text = " ".join(str(w[4]) for w in right_words).strip()

            if matched_sec and right_text:
                # 같은 줄 오른쪽에 다른 섹션 이름이 들어 있으면 배너로 보고 제외
                if any(other_s in right_text.replace(" ", "") for other_s in SECTION_NAMES):
                    i += 1
                    continue

                headline = right_text
                direction = extract_direction(headline)
                item_code, item_label = resolve_item(headline, current_item, item_pairs)

                # 그 아래 불릿 줄 수집 (●, - 로 시작)
                bullets: list[str] = []
                j = i + 1
                current_bullet = ""

                while j < len(lines):
                    next_l = lines[j]
                    next_txt = " ".join(str(w[4]) for w in next_l).strip()

                    # 표 시작선이거나 다음 섹션이면 불릿 수집 종료
                    if "단위:" in next_txt or "단위 :" in next_txt or next_txt.startswith("|"):
                        break

                    next_left = [w for w in next_l if float(w[0]) < 120.0]
                    next_left_txt = "".join(str(w[4]) for w in next_left).replace(" ", "")
                    if any(s in next_left_txt for s in SECTION_NAMES):
                        if any(float(w[0]) >= 120.0 for w in next_l):
                            break

                    # 불릿 시작 확인
                    if next_txt.startswith("●") or next_txt.startswith("-"):
                        if current_bullet:
                            bullets.append(current_bullet)
                        current_bullet = next_txt
                    elif current_bullet:
                        # 이전 불릿에 이어진 문장 합침
                        current_bullet += " " + next_txt
                    elif next_txt.startswith("※"):
                        break

                    j += 1

                if current_bullet:
                    bullets.append(current_bullet)

                sections.append(
                    SectionText(
                        page=pg_idx,
                        section=matched_sec,
                        headline=headline,
                        direction=direction,
                        bullets=bullets,
                        item_code=item_code,
                        item_label=item_label,
                    )
                )
                i = j
            else:
                i += 1

    return sections


def is_numeric_token(t: str) -> bool:
    clean = t.replace(",", "").replace("%", "").strip()
    try:
        float(clean)
        return True
    except ValueError:
        return False


def is_note_start(txt: str) -> bool:
    """주석 시작 판단 ('주:', '주 1)', '주:1)' 등)."""
    t = txt.strip()
    return bool(
        re.match(r"^주\s*[\d\:\)\.]", t)
        or re.match(r"^주\s*$", t)
        or t.startswith("주:")
        or t.startswith("주 :")
    )


def is_source_start(txt: str) -> bool:
    """출처 시작 판단 ('자료:', '자료 :')."""
    t = txt.strip()
    return bool(re.match(r"^자료\s*[\:\)]", t) or t.startswith("자료:") or t.startswith("자료 :"))


def build_table_grid(
    body_lines: list[list[Any]],
    table_type: str,
) -> tuple[list[Any], list[list[str]]]:
    """단어 좌표로부터 열 경계를 계산하고 2D 그리드 (header, rows) 생성."""
    if not body_lines:
        return [], []

    # 1. 머리글 줄과 데이터 줄 분리
    header_lines: list[list[Any]] = []
    data_lines: list[list[Any]] = []
    data_started = False

    for ln in body_lines:
        first_txt = str(ln[0][4]) if ln else ""
        has_num = any(is_numeric_token(str(w[4])) for w in ln)

        if not data_started:
            if (
                re.match(r"^\d{4}년", first_txt)
                or first_txt in ["평년", "전년"]
                or (len(ln) > 1 and is_numeric_token(str(ln[1][4])))
                or (table_type == "growth_survey" and has_num)
            ):
                data_started = True
                data_lines.append(ln)
            else:
                header_lines.append(ln)
        else:
            data_lines.append(ln)

    # 2. 열 중심(col_centers) 찾기
    best_row: list[Any] = []
    max_nums = -1
    for ln in data_lines:
        n_count = sum(1 for w in ln if is_numeric_token(str(w[4])))
        if n_count > max_nums:
            max_nums = n_count
            best_row = ln

    num_tokens = [w for w in best_row if is_numeric_token(str(w[4]))]
    if len(num_tokens) < 1 and header_lines:
        best_row = header_lines[-1]
        num_tokens = best_row[1:]

    if not num_tokens:
        return [
            [{"text": " ".join(str(w[4]) for w in ln), "x0": float(ln[0][0]), "x1": float(ln[-1][2])}]
            for ln in header_lines
        ], [[" ".join(str(w[4]) for w in ln)] for ln in data_lines]

    c0 = (float(best_row[0][0]) + float(best_row[0][2])) / 2.0
    c_others = [(float(w[0]) + float(w[2])) / 2.0 for w in num_tokens]
    col_centers = [c0] + c_others
    col_centers.sort()

    cuts = [(col_centers[k - 1] + col_centers[k]) / 2.0 for k in range(1, len(col_centers))]

    # 3. 데이터 행을 열 구획으로 분할
    rows: list[list[str]] = []
    for ln in data_lines:
        row_cells = [[] for _ in col_centers]
        for w in ln:
            cx = (float(w[0]) + float(w[2])) / 2.0
            b_idx = 0
            for k, cut in enumerate(cuts):
                if cx >= cut:
                    b_idx = k + 1
                else:
                    break
            row_cells[b_idx].append(str(w[4]))

        row_strs = [" ".join(cell).strip() for cell in row_cells]
        if row_strs[0] == "증감률" and not any(row_strs[1:]):
            continue
        if any(row_strs):
            rows.append(row_strs)

    # 4. 머리글 정보 구성 (열 구획별 [{text, x0, x1}])
    header_json: list[Any] = []
    for hl in header_lines:
        hl_cells = [[] for _ in col_centers]
        for w in hl:
            cx = (float(w[0]) + float(w[2])) / 2.0
            b_idx = 0
            for k, cut in enumerate(cuts):
                if cx >= cut:
                    b_idx = k + 1
                else:
                    break
            hl_cells[b_idx].append(w)

        line_objs = []
        for cell_words in hl_cells:
            if cell_words:
                txt = " ".join(str(w[4]) for w in cell_words).strip()
                min_x = round(min(float(w[0]) for w in cell_words), 1)
                max_x = round(max(float(w[2]) for w in cell_words), 1)
                line_objs.append({"text": txt, "x0": min_x, "x1": max_x})
            else:
                line_objs.append({"text": "", "x0": 0.0, "x1": 0.0})

        header_json.append(line_objs)

    return header_json, rows


def extract_raw_tables(
    pages: list[dict[str, Any]],
    items: list[dict[str, Any]],
    ovr_item_code: str,
) -> list[RawTable]:
    """글자 좌표에서 모든 표의 원형(table_raw)을 추출하고 그래프는 제외."""
    _, item_pairs = build_item_dict(items, ovr_item_code)
    tables: list[RawTable] = []
    seq = 1

    current_item: tuple[str | None, str | None] = (None, None)

    for pg_idx, pg in enumerate(pages, 1):
        words = pg.get("words", [])
        lines = reconstruct_lines(words)

        # 품목명 갱신
        for ln in lines[:5]:
            large_words = [w for w in ln if (float(w[3]) - float(w[1])) >= 18.0]
            if large_words:
                ltxt = "".join(str(w[4]) for w in large_words)
                current_item = resolve_item(ltxt, current_item, item_pairs)

        i = 0
        while i < len(lines):
            ln = lines[i]
            line_text = " ".join(str(w[4]) for w in ln)

            if "단위:" in line_text or "단위 :" in line_text:
                parts = re.split(r"단위\s*:", line_text)
                title = parts[0].strip().lstrip("|").strip()
                unit = parts[1].strip() if len(parts) > 1 else None

                table_type = classify_table_title(title)
                item_code, item_label = resolve_item(title, current_item, item_pairs)

                # 표 본문 줄 수집
                table_lines: list[list[Any]] = []
                note: str | None = None
                source: str | None = None

                j = i + 1
                while j < len(lines):
                    cur_line = lines[j]
                    cur_text = " ".join(str(w[4]) for w in cur_line).strip()

                    # 다음 표 시작이거나 다음 섹션이면 중단
                    if "단위:" in cur_text or "단위 :" in cur_text:
                        break

                    left_words = [w for w in cur_line if float(w[0]) < 120.0]
                    left_text = "".join(str(w[4]) for w in left_words).replace(" ", "")
                    if any(s in left_text for s in SECTION_NAMES) and any(
                        float(w[0]) >= 120.0 for w in cur_line
                    ):
                        break

                    if is_note_start(cur_text):
                        note_lines = [cur_text]
                        j += 1
                        while j < len(lines):
                            nxt_line = lines[j]
                            nxt_text = " ".join(str(w[4]) for w in nxt_line).strip()
                            if is_source_start(nxt_text):
                                source = re.sub(r"^자료\s*:\s*", "", nxt_text)
                                j += 1
                                break
                            if "단위:" in nxt_text or "단위 :" in nxt_text:
                                break
                            nxt_left = [w for w in nxt_line if float(w[0]) < 120.0]
                            nxt_left_txt = "".join(str(w[4]) for w in nxt_left).replace(" ", "")
                            if any(s in nxt_left_txt for s in SECTION_NAMES) and any(
                                float(w[0]) >= 120.0 for w in nxt_line
                            ):
                                break
                            note_lines.append(nxt_text)
                            j += 1
                        note = "\n".join(note_lines)
                        break

                    if is_source_start(cur_text):
                        source = re.sub(r"^자료\s*:\s*", "", cur_text)
                        j += 1
                        break

                    table_lines.append(cur_line)
                    j += 1

                # 그래프 제외: table_type 이 graph 이거나 숫자 행이 2개 미만인 경우
                numeric_row_count = 0
                for tl in table_lines:
                    if sum(1 for w in tl if is_numeric_token(str(w[4]))) >= 2:
                        numeric_row_count += 1

                if table_type == "graph" or numeric_row_count < 2:
                    i = j
                    continue

                header_json, grid_rows = build_table_grid(table_lines, table_type)

                tables.append(
                    RawTable(
                        page=pg_idx,
                        seq=seq,
                        title=title,
                        unit=unit,
                        header=header_json,
                        rows=grid_rows,
                        note=note,
                        source=source,
                        table_type=table_type,
                        check_status="not_checked",
                        check_log=None,
                        item_code=item_code,
                        item_label=item_label,
                    )
                )
                seq += 1
                i = j
            else:
                i += 1

    return tables


def parse_period_dates(label: str, issue_y: int, issue_m: int) -> tuple[dt.date | None, dt.date | None]:
    """period_label 로부터 period_start, period_end 계산 (§2-3)."""
    lbl = label.strip()

    m_range = re.match(r"(\d+)월\s*~\s*익년\s*(\d+)월", lbl)
    if m_range:
        m1, m2 = int(m_range.group(1)), int(m_range.group(2))
        y1 = issue_y
        y2 = issue_y + 1
        last_d = calendar.monthrange(y2, m2)[1]
        return dt.date(y1, m1, 1), dt.date(y2, m2, last_d)

    m_after = re.match(r"(\d+)월\s*이후", lbl)
    if m_after:
        m = int(m_after.group(1))
        y = issue_y if m >= issue_m else issue_y + 1
        return dt.date(y, m, 1), None

    m_single = re.match(r"(\d+)월", lbl)
    if m_single:
        m = int(m_single.group(1))
        y = issue_y if m >= issue_m else issue_y + 1
        last_d = calendar.monthrange(y, m)[1]
        return dt.date(y, m, 1), dt.date(y, m, last_d)

    return None, None


def parse_clean_float(val_str: str) -> float | None:
    """쉼표 제거 후 float 변환. '-', 빈칸은 None."""
    s = val_str.replace(",", "").replace("%", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_production_outlook(
    table: RawTable,
    file_id: int,
    yyyy: int,
) -> tuple[list[Observation], list[dict[str, Any]]]:
    """production_outlook 표 검사 및 observation 행 추출."""
    errors: list[dict[str, Any]] = []
    obs: list[Observation] = []

    rows = table.rows
    if len(rows) < 2:
        return obs, [{"check": "shape", "error": "데이터 행 부족"}]

    row_map: dict[str, list[str]] = {}
    for r in rows:
        label = r[0].strip()
        if re.search(r"\d{4}년", label) and "대비" not in label:
            yr = int(re.search(r"(\d{4})년", label).group(1))
            if yr == yyyy:
                row_map["current"] = r
            elif yr == yyyy - 1:
                row_map["prev"] = r
        elif "전년" in label and "대비" in label:
            row_map["yoy_pct"] = r
        elif "평년" in label and "대비" in label:
            row_map["vs_normal_pct"] = r
        elif "평년" in label:
            row_map["normal"] = r

    if "current" not in row_map or "prev" not in row_map:
        return obs, [{"check": "shape", "error": "필수 행(당해년, 전년) 누락"}]

    metrics = [
        ("area", "ha", 1),
        ("yield", "kg/10a", 2),
        ("production", "천 톤", 3),
    ]

    # 1. 면적 × 단수 ≈ 생산량 검사 (current)
    try:
        c_row = row_map["current"]
        a_val = parse_clean_float(c_row[1])
        y_val = parse_clean_float(c_row[2])
        p_val = parse_clean_float(c_row[3])
        if a_val and y_val and p_val:
            ok, msg = check_area_yield_prod(a_val, y_val, p_val, table.unit)
            if not ok:
                errors.append({"check": "area_yield_prod", "error": msg})
    except Exception as e:
        errors.append({"check": "area_yield_prod", "error": str(e)})

    # 2. 증감률 재계산 검사
    if "yoy_pct" in row_map:
        yoy_row = row_map["yoy_pct"]
        prev_row = row_map["prev"]
        for m_name, _, col_idx in metrics:
            if col_idx < len(c_row) and col_idx < len(prev_row) and col_idx < len(yoy_row):
                c_str = c_row[col_idx]
                p_str = prev_row[col_idx]
                y_str = yoy_row[col_idx]
                if c_str and p_str and y_str and y_str != "-":
                    ok, msg = check_yoy_recalc(c_str, p_str, y_str)
                    if not ok:
                        errors.append({"check": "yoy_recalc", "metric": m_name, "error": msg})

    # 평년 대비 재계산 검사
    if "normal" in row_map and "vs_normal_pct" in row_map:
        norm_row = row_map["normal"]
        vs_norm_row = row_map["vs_normal_pct"]
        for m_name, _, col_idx in metrics:
            if col_idx < len(c_row) and col_idx < len(norm_row) and col_idx < len(vs_norm_row):
                c_str = c_row[col_idx]
                n_str = norm_row[col_idx]
                vn_str = vs_norm_row[col_idx]
                if c_str and n_str and vn_str and vn_str != "-":
                    ok, msg = check_yoy_recalc(c_str, n_str, vn_str)
                    if not ok:
                        errors.append({"check": "vs_normal_recalc", "metric": m_name, "error": msg})

    if errors:
        return obs, errors

    raw_id = table.id or 0
    basis_rows = [
        ("current", yyyy, row_map["current"]),
        ("prev", yyyy - 1, row_map["prev"]),
    ]
    if "normal" in row_map:
        basis_rows.append(("normal", None, row_map["normal"]))
    if "yoy_pct" in row_map:
        basis_rows.append(("yoy_pct", None, row_map["yoy_pct"]))
    if "vs_normal_pct" in row_map:
        basis_rows.append(("vs_normal_pct", None, row_map["vs_normal_pct"]))

    for basis, c_year, r in basis_rows:
        for m_name, default_unit, col_idx in metrics:
            if col_idx < len(r):
                val = parse_clean_float(r[col_idx])
                if val is not None:
                    u = "%" if "pct" in basis else default_unit
                    obs.append(
                        Observation(
                            file_id=file_id,
                            table_raw_id=raw_id,
                            table_seq=table.seq,
                            item_code=table.item_code,
                            item_label=table.item_label,
                            metric=m_name,
                            basis=basis,
                            crop_year=c_year,
                            period_label=f"{yyyy}년",
                            value=val,
                            unit=u,
                        )
                    )

    return obs, []


def parse_shipment_outlook(
    table: RawTable,
    file_id: int,
    yyyy: int,
    mm: int,
) -> tuple[list[Observation], list[dict[str, Any]]]:
    """shipment_outlook 표 검사 및 observation 행 추출."""
    errors: list[dict[str, Any]] = []
    obs: list[Observation] = []

    rows = table.rows
    if len(rows) < 2:
        return obs, [{"check": "shape", "error": "데이터 행 부족"}]

    # 머리글에서 기간 라벨 추출 (table.header 의 innermost 줄 사용)
    period_labels: list[str] = []
    if table.header:
        innermost = table.header[-1]
        for cell in innermost[1:]:
            txt = str(cell.get("text", "")).strip() if isinstance(cell, dict) else str(cell).strip()
            if txt:
                period_labels.append(txt)

    if not period_labels:
        return obs, [{"check": "shape", "error": "기간 머리글 누락"}]

    row_map: dict[str, list[str]] = {}
    for r in rows:
        label = r[0].strip()
        if re.search(r"\d{4}년", label) and "대비" not in label:
            yr = int(re.search(r"(\d{4})년", label).group(1))
            if yr == yyyy:
                row_map["current"] = r
            elif yr == yyyy - 1:
                row_map["prev"] = r
        elif "전년" in label and "대비" in label:
            row_map["yoy_pct"] = r
        elif "평년" in label and "대비" in label:
            row_map["vs_normal_pct"] = r
        elif "평년" in label:
            row_map["normal"] = r

    if "current" not in row_map or "prev" not in row_map:
        return obs, [{"check": "shape", "error": "필수 행(당해년, 전년) 누락"}]

    c_row = row_map["current"]
    prev_row = row_map["prev"]

    # 증감률 재계산 검사
    if "yoy_pct" in row_map:
        yoy_row = row_map["yoy_pct"]
        for idx, plabel in enumerate(period_labels, 1):
            if idx < len(c_row) and idx < len(prev_row) and idx < len(yoy_row):
                c_str = c_row[idx]
                p_str = prev_row[idx]
                y_str = yoy_row[idx]
                if c_str and p_str and y_str and y_str != "-":
                    ok, msg = check_yoy_recalc(c_str, p_str, y_str)
                    if not ok:
                        errors.append(
                            {
                                "check": "yoy_recalc",
                                "period": plabel,
                                "error": msg,
                            }
                        )

    # 평년 대비 재계산 검사
    if "normal" in row_map and "vs_normal_pct" in row_map:
        norm_row = row_map["normal"]
        vs_norm_row = row_map["vs_normal_pct"]
        for idx, plabel in enumerate(period_labels, 1):
            if idx < len(c_row) and idx < len(norm_row) and idx < len(vs_norm_row):
                c_str = c_row[idx]
                n_str = norm_row[idx]
                vn_str = vs_norm_row[idx]
                if c_str and n_str and vn_str and vn_str != "-":
                    ok, msg = check_yoy_recalc(c_str, n_str, vn_str)
                    if not ok:
                        errors.append(
                            {
                                "check": "vs_normal_recalc",
                                "period": plabel,
                                "error": msg,
                            }
                        )

    if errors:
        return obs, errors

    raw_id = table.id or 0
    unit_str = "천 톤"
    if table.unit and "톤" in table.unit:
        unit_str = "천 톤" if "천 톤" in table.unit else "톤"

    basis_rows = [
        ("current", yyyy, row_map["current"]),
        ("prev", yyyy - 1, row_map["prev"]),
    ]
    if "normal" in row_map:
        basis_rows.append(("normal", None, row_map["normal"]))
    if "yoy_pct" in row_map:
        basis_rows.append(("yoy_pct", None, row_map["yoy_pct"]))
    if "vs_normal_pct" in row_map:
        basis_rows.append(("vs_normal_pct", None, row_map["vs_normal_pct"]))

    for basis, c_year, r in basis_rows:
        for idx, plabel in enumerate(period_labels, 1):
            if idx < len(r):
                val = parse_clean_float(r[idx])
                if val is not None:
                    p_start, p_end = parse_period_dates(plabel, yyyy, mm)
                    u = "%" if "pct" in basis else unit_str
                    obs.append(
                        Observation(
                            file_id=file_id,
                            table_raw_id=raw_id,
                            table_seq=table.seq,
                            item_code=table.item_code,
                            item_label=table.item_label,
                            metric="shipment",
                            basis=basis,
                            crop_year=c_year,
                            period_label=plabel,
                            period_start=p_start,
                            period_end=p_end,
                            value=val,
                            unit=u,
                        )
                    )

    return obs, []


def parse_monthly_price(
    table: RawTable,
    file_id: int,
    yyyy: int,
) -> tuple[list[Observation], list[dict[str, Any]]]:
    """monthly_price 표 검사 및 observation 행 추출."""
    errors: list[dict[str, Any]] = []
    obs: list[Observation] = []

    rows = table.rows
    if len(rows) < 1:
        return obs, [{"check": "shape", "error": "데이터 행 부족"}]

    num_cols = len(rows[0])
    col_months: list[tuple[str, int] | None] = [None] * num_cols
    col_varieties: list[str | None] = [None] * num_cols

    # 1. 월 머리글 탐색
    for hl in table.header:
        for c in range(1, num_cols):
            if c < len(hl):
                cell = hl[c]
                txt = str(cell.get("text", "")).strip() if isinstance(cell, dict) else str(cell).strip()
                m_match = re.search(r"(\d+)월", txt)
                if m_match:
                    col_months[c] = (f"{m_match.group(1)}월", int(m_match.group(1)))

    # 머리글 셀 병합 등으로 빈 열이 있을 경우 인접 열에서 채움 (예: 사과 8월 쓰가루/홍로 병합)
    for c in range(1, num_cols):
        if col_months[c] is None:
            if c + 1 < num_cols and col_months[c + 1] is not None:
                col_months[c] = col_months[c + 1]
            elif c - 1 >= 1 and col_months[c - 1] is not None:
                col_months[c] = col_months[c - 1]

    # 최소 1개 이상의 월이 식별되어야 함
    valid_months = [m for m in col_months if m is not None]
    if not valid_months:
        return obs, [{"check": "shape", "error": "월 머리글 누락"}]

    m_first = valid_months[0][1]

    # 2. 품종 머리글 탐색
    all_header_text = " ".join(
        str(cell.get("text", "")) if isinstance(cell, dict) else str(cell)
        for hl in table.header
        for cell in hl
    )

    if "사과" in table.title and num_cols >= 14:
        for c in range(1, num_cols):
            if c in (1, 2):
                col_varieties[c] = "쓰가루"
            elif c in (3, 4):
                col_varieties[c] = "홍로"
            else:
                col_varieties[c] = "후지"
    elif "배" in table.title and "원황" in all_header_text and "신고" in all_header_text:
        for c in range(1, num_cols):
            if c == 1:
                col_varieties[c] = "원황"
            else:
                col_varieties[c] = "신고"
    elif "단감" in table.title and "서촌조생" in all_header_text and "부유" in all_header_text:
        for c in range(1, num_cols):
            if c == 1:
                col_varieties[c] = "서촌조생"
            else:
                col_varieties[c] = "부유"
    elif "포도" in table.title and num_cols == 16:
        for c in range(1, num_cols):
            if 1 <= c <= 4:
                col_varieties[c] = "거봉"
            elif 5 <= c <= 8:
                col_varieties[c] = "캠벨얼리"
            else:
                col_varieties[c] = "샤인머스캣"

    raw_id = table.id or 0
    unit_str = table.unit or "원"

    for r in rows:
        label = r[0].strip()
        c_year: int | None = None
        basis: str | None = None

        y_match = re.search(r"(\d{4})년산", label)
        if y_match:
            yr = int(y_match.group(1))
            c_year = yr
            if yr == yyyy:
                basis = "current"
            elif yr == yyyy - 1:
                basis = "prev"
            else:
                continue
        elif re.search(r"’(\d{2})/’(\d{2})년", label):
            m_s = re.search(r"’(\d{2})/’(\d{2})년", label)
            assert m_s is not None
            yr = 2000 + int(m_s.group(2))
            c_year = yr
            if yr == yyyy:
                basis = "current"
            elif yr == yyyy - 1:
                basis = "prev"
            else:
                continue
        elif re.search(r"(\d{4})년", label):
            yr = int(re.search(r"(\d{4})년", label).group(1))
            c_year = yr
            if yr == yyyy:
                basis = "current"
            elif yr == yyyy - 1:
                basis = "prev"
            else:
                continue
        elif label == "평년":
            basis = "normal"
            c_year = None
        elif label == "상품":
            basis = "current"
            c_year = yyyy
        else:
            continue

        for col_idx in range(1, min(len(r), num_cols)):
            val = parse_clean_float(r[col_idx])
            if val is not None:
                if val <= 0:
                    errors.append(
                        {
                            "check": "positive_price",
                            "error": f"도매가격이 0 이하: {val}",
                        }
                    )
                    continue

                if col_months[col_idx] is None:
                    continue
                m_label, m_num = col_months[col_idx]

                # crop_year 와 period_dates 계산
                if c_year is not None:
                    p_year = c_year if m_num >= m_first else c_year + 1
                else:
                    p_year = yyyy if m_num <= 12 else yyyy

                last_day = calendar.monthrange(p_year, m_num)[1]
                p_start = dt.date(p_year, m_num, 1)
                p_end = dt.date(p_year, m_num, last_day)

                obs.append(
                    Observation(
                        file_id=file_id,
                        table_raw_id=raw_id,
                        table_seq=table.seq,
                        item_code=table.item_code,
                        item_label=table.item_label,
                        variety=col_varieties[col_idx],
                        metric="wholesale_price",
                        basis=basis,
                        crop_year=c_year,
                        period_label=m_label,
                        period_start=p_start,
                        period_end=p_end,
                        value=val,
                        unit=unit_str,
                    )
                )

    return obs, errors


def parse_growth_survey(
    table: RawTable,
    file_id: int,
) -> tuple[list[SurveyDist], list[dict[str, Any]]]:
    """growth_survey 표 검사 및 survey_dist 행 추출."""
    errors: list[dict[str, Any]] = []
    dists: list[SurveyDist] = []

    rows = table.rows
    if len(rows) < 1:
        return dists, [{"check": "shape", "error": "데이터 행 부족"}]

    raw_id = table.id or 0

    for r in rows:
        aspect = r[0].strip()
        if not aspect or aspect in ["구분", "주:", "자료:"]:
            continue

        if len(r) >= 4:
            good_val = parse_clean_float(r[1])
            same_val = parse_clean_float(r[2])
            bad_val = parse_clean_float(r[3])

            if good_val is not None and same_val is not None and bad_val is not None:
                ok, msg = check_dist_sum(good_val, same_val, bad_val)
                if not ok:
                    errors.append({"check": "dist_sum", "aspect": aspect, "error": msg})
                else:
                    dists.append(
                        SurveyDist(
                            file_id=file_id,
                            table_raw_id=raw_id,
                            table_seq=table.seq,
                            item_code=table.item_code,
                            item_label=table.item_label,
                            aspect=aspect,
                            good=good_val,
                            same=same_val,
                            bad=bad_val,
                        )
                    )

    if errors:
        return [], errors

    return dists, []


def parse_report_pages(
    file_id: int,
    yyyy: int,
    mm: int,
    ovr_item_code: str,
    pages: list[dict[str, Any]],
    items: list[dict[str, Any]],
    text_status: str | None = "ok",
) -> ParsedReport:
    """단일 발행호의 pages.json.gz 데이터 전체 파싱 오케스트레이터."""
    # 제외 조건 (§0)
    if text_status == "no_text" or (
        ovr_item_code not in STRUCTURED_NUM_SCOPE and ovr_item_code not in PREP_SCOPE
    ):
        return ParsedReport(
            file_id=file_id,
            yyyy=yyyy,
            mm=mm,
            parser_version=PARSER_VERSION,
            parse_status="skipped",
        )

    # 1. 헤드라인 및 원형 표 추출
    sections = extract_section_texts(pages, items, ovr_item_code)
    raw_tables = extract_raw_tables(pages, items, ovr_item_code)

    # 2. 준비 대상 (쌀, 콩, 감자) 은 원형 표와 헤드라인만 남김 (숫자 파싱 없음)
    if ovr_item_code in PREP_SCOPE:
        return ParsedReport(
            file_id=file_id,
            yyyy=yyyy,
            mm=mm,
            parser_version=PARSER_VERSION,
            parse_status="parsed",
            raw_tables=raw_tables,
            section_texts=sections,
            observations=[],
            survey_dists=[],
        )

    # 3. 과일·과채 정형 숫자 파싱 및 검사
    all_observations: list[Observation] = []
    all_survey_dists: list[SurveyDist] = []
    has_failed_core_table = False

    for t in raw_tables:
        if t.table_type == "production_outlook":
            obs, errs = parse_production_outlook(t, file_id, yyyy)
            if errs:
                t.check_status = "fail"
                t.check_log = errs
                has_failed_core_table = True
            else:
                t.check_status = "pass"
                all_observations.extend(obs)

        elif t.table_type == "shipment_outlook":
            obs, errs = parse_shipment_outlook(t, file_id, yyyy, mm)
            if errs:
                t.check_status = "fail"
                t.check_log = errs
                has_failed_core_table = True
            else:
                t.check_status = "pass"
                all_observations.extend(obs)

        elif t.table_type == "monthly_price":
            obs, errs = parse_monthly_price(t, file_id, yyyy)
            if errs:
                t.check_status = "fail"
                t.check_log = errs
                has_failed_core_table = True
            else:
                t.check_status = "pass"
                all_observations.extend(obs)

        elif t.table_type == "growth_survey":
            dists, errs = parse_growth_survey(t, file_id)
            if errs:
                t.check_status = "fail"
                t.check_log = errs
                has_failed_core_table = True
            else:
                t.check_status = "pass"
                all_survey_dists.extend(dists)

        else:
            t.check_status = "not_checked"

    report_status = "needs_review" if has_failed_core_table else "parsed"

    return ParsedReport(
        file_id=file_id,
        yyyy=yyyy,
        mm=mm,
        parser_version=PARSER_VERSION,
        parse_status=report_status,
        raw_tables=raw_tables,
        section_texts=sections,
        observations=all_observations,
        survey_dists=all_survey_dists,
    )
