"""기상청 ASOS 지점 목록 현행화 (이슈 #27) 시험. 키 없이 만든 가짜 응답만 쓴다."""

import csv
import io as _io

import pytest

from croplib_dp.collectors import stations_sync
from croplib_dp.core import stations as st
from croplib_dp.io import kma_hub

FAKE_KEY = "dummy-value-hub-key"

HEAD = """#START7777
#--------------------------------------------------------------------------------
#  STN_ID : 지점번호
#  LON : 경도(degree), LAT : 위도(degree)
#  HT_WD : 풍향/풍속계 지상높이(m)
#--------------------------------------------------------------------------------
#  STN_ID  LON  LAT  STN_SP  HT  HT_PA  HT_TA  HT_WD  HT_RN STN_AD STN_KO STN_EN FCT_ID LAW_ID BASIN
#      -   degree  degree   -   m   m   m   m   m   -   -   -   -   -   -
"""
TAIL = "#7777END\n"

NOW_ROWS = """\
    90 128.56473  38.25085  0  18.06  19.6  1.5  10.0  0.6  0  속초  Sokcho  11D20401  5182000000  0
   101 127.73570  37.90262  0  77.75  78.8  1.5  10.0  0.8  0  춘천  Chuncheon  11D10301  5111000000  0
   102 124.63032  37.97396  0 144.86 145.9  1.5  10.0  0.7  0  백령도  Baengnyeongdo  11A00101  -  0
   108 126.96580  37.57142  0  85.67  86.7  1.5  10.0  0.6  0  서울  Seoul  11B10101  1111000000  0
   146 127.11905  35.84092  0  61.40  62.4  1.5  10.0  0.6  0  전주  Jeonju  11F10201  5211300000  0
   184 126.52969  33.51411  0  20.45  21.5  1.5  -999  0.6  0  제주  Jeju  11G00201  5011000000  0
   216 128.98929  37.17038  0 712.82 713.8  1.5  10.0  0.6  0  태백  Taebaek  11D10501  5119000000  0
"""

PAST_ROWS = """\
    90 128.56473  38.25085  0  18.06  19.6  1.5  10.0  0.6  0  속초  Sokcho  11D20401  4282000000  0
   101 127.73570  37.90262  0  77.75  78.8  1.5  10.0  0.8  0  춘천  Chuncheon  11D10301  4211000000  0
   103 128.10000  37.50000  0 300.00 301.0  1.5  10.0  0.6  0  옛지점  Old Station  11D10401  4273000000  0
"""

NOW_TEXT = HEAD + NOW_ROWS + TAIL
PAST_TEXT = HEAD + PAST_ROWS + TAIL


def fake_lookup(lat, lon):
    """좌표 → 행정구역 (가짜). 백령도 좌표만 안다."""
    if abs(lat - 37.97) < 0.1 and abs(lon - 124.63) < 0.1:
        return "인천광역시"
    raise AssertionError(f"모르는 좌표 {lat},{lon}")


def build(now=NOW_TEXT, past=PAST_TEXT, db=None):
    current = kma_hub.parse_stn_inf(now)
    history = {"2015-01-01": kma_hub.parse_stn_inf(past)}
    return st.build_rows(current, history, db_stations=db or st.DB_STATIONS, sido_lookup=fake_lookup)


def by_id(rows):
    return {r["stn_id"]: r for r in rows}


# ---- 열 이름·순서 ----


def test_columns_and_order():
    rows = build()
    assert st.COLUMNS == (
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
    assert all(tuple(r) == st.COLUMNS for r in rows)
    text = st.to_csv_text(rows)
    assert text.splitlines()[0] == ",".join(st.COLUMNS)


def test_csv_sorted_by_station_number_and_no_duplicates():
    rows = build()
    ids = [r["stn_id"] for r in rows]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    back = list(csv.DictReader(_io.StringIO(st.to_csv_text(rows))))
    assert [int(r["stn_id"]) for r in back] == ids
    assert st.check_rows(rows) == []


def test_values_from_response():
    r = by_id(build())[90]
    assert r["name_ko"] == "속초"
    assert r["lat"] == 38.25085 and r["lon"] == 128.56473
    assert r["elev_m"] == 18.06 and r["wind_height_m"] == 10.0


def test_missing_number_becomes_blank():
    """-999 같은 결측 표시는 빈 칸 (0이나 -999를 그대로 쓰지 않는다)"""
    assert by_id(build())[184]["wind_height_m"] is None
    row = [r for r in st.to_csv_text(build()).splitlines() if r.startswith("184,")][0]
    assert row.split(",")[6] == ""


# ---- 시도 약칭 통일 ----


@pytest.mark.parametrize(
    "full, short",
    [
        ("서울특별시", "서울"),
        ("부산광역시", "부산"),
        ("세종특별자치시", "세종"),
        ("경기도", "경기"),
        ("강원도", "강원"),
        ("강원특별자치도", "강원"),
        ("충청북도", "충북"),
        ("충청남도", "충남"),
        ("전라북도", "전북"),
        ("전북특별자치도", "전북"),
        ("전라남도", "전남"),
        ("경상북도", "경북"),
        ("경상남도", "경남"),
        ("제주특별자치도", "제주"),
        ("강원", "강원"),
        (" 충북 ", "충북"),
    ],
)
def test_sido_short(full, short):
    assert st.sido_short(full) == short


def test_sido_short_unknown_fails():
    with pytest.raises(ValueError):
        st.sido_short("어딘가도")


def test_sido_from_law_id_old_and_new_codes():
    assert st.sido_from_law_id("4211000000") == "강원"  # 옛 강원도
    assert st.sido_from_law_id("5111000000") == "강원"  # 강원특별자치도
    assert st.sido_from_law_id("4511300000") == "전북"
    assert st.sido_from_law_id("5211300000") == "전북"  # 전북특별자치도
    assert st.sido_from_law_id("1111000000") == "서울"
    assert st.sido_from_law_id("-") is None
    assert st.sido_from_law_id("") is None


def test_sido_all_short_and_method_noted():
    rows = by_id(build())
    assert {r["sido"] for r in rows.values()} <= set(st.SIDO_SHORT)
    assert rows[101]["sido"] == "강원" and rows[146]["sido"] == "전북" and rows[184]["sido"] == "제주"
    assert "법정동코드" in rows[101]["source_note"]
    # 기상청 자료에 행정구역이 없으면 좌표 → 행정구역으로 채우고 방법을 적는다
    assert rows[102]["sido"] == "인천"
    assert "좌표" in rows[102]["source_note"]


def test_sido_lookup_missing_fails_loudly():
    current = kma_hub.parse_stn_inf(NOW_TEXT)
    with pytest.raises(st.StationDataError):
        st.build_rows(current, {}, db_stations=st.DB_STATIONS, sido_lookup=None)


# ---- 폐지 지점 ----


def test_closed_station_kept_inactive():
    rows = by_id(build())
    assert rows[103]["is_active"] is False
    assert rows[103]["name_ko"] == "옛지점" and rows[103]["sido"] == "강원"
    assert "2015-01-01" in rows[103]["source_note"]
    assert all(r["is_active"] for k, r in rows.items() if k != 103)
    line = [x for x in st.to_csv_text(list(rows.values())).splitlines() if x.startswith("103,")][0]
    assert line.split(",")[8] == "false"


# ---- 일사 관측 여부: 확인된 것만, 나머지는 빈 칸 ----


def test_measures_sunlight_only_when_known():
    rows = by_id(build())
    assert rows[101]["measures_sunlight"] is True
    assert rows[216]["measures_sunlight"] is False
    assert rows[90]["measures_sunlight"] is None
    line = [x for x in st.to_csv_text(list(rows.values())).splitlines() if x.startswith("90,")][0]
    assert line.split(",")[7] == ""


# ---- 기준 목록 ----


def test_reference_lists():
    assert len(st.DRAFT_96) == 96 and len(set(st.DRAFT_96)) == 96
    assert len(st.DB_STATIONS) == 10
    assert set(st.DB_STATIONS) <= set(st.DRAFT_96)


# ---- 비교표 ----


def test_diff_three_groups():
    rows = build(db={101: ("춘천", True), 108: ("서을", None)})
    d = st.diff(rows, draft_ids=(90, 101, 103, 999), db_stations={101: ("춘천", True), 108: ("서을", None)})
    assert [r["stn_id"] for r in d["missing"]] == [102, 146, 184, 216]  # 기상청에만 (108은 DB에 있음)
    assert {r["stn_id"]: r["kma"] for r in d["gone"]} == {103: "closed", 999: "absent"}
    assert [(r["stn_id"], r["ref_name"], r["kma_name"]) for r in d["renamed"]] == [(108, "서을", "서울")]
    md = st.diff_markdown(d, run_date="2026-10-28", counts=st.counts(rows, (90, 101, 103, 999)))
    for word in ("(가)", "(나)", "(다)", "102", "백령도", "999", "서을"):
        assert word in md


def test_check_rows_finds_duplicates_and_bad_values():
    rows = build()
    bad = rows + [dict(rows[0])]
    assert any("중복" in p for p in st.check_rows(bad))
    wrong = [dict(rows[0], sido="강원도")]
    assert any("시도" in p for p in st.check_rows(wrong))


# ---- 응답이 비거나 형식이 다르면 실패 ----


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n",
        HEAD + TAIL,  # 머리줄만 있고 지점이 없음
        "<html><body>Service Unavailable</body></html>",
        '{"result":{"status":403,"message":"인증 실패"}}',
        "#START7777\n#  NAME  X  Y\n    90  1  2\n#7777END\n",  # 필요한 열이 없음
        HEAD + "    90 abc 38.2 0 18 19 1.5 10 0.6 0 속초 Sokcho 11D 4282000000 0\n" + TAIL,  # 숫자 아님
        HEAD + "    90 128.5\n" + TAIL,  # 열이 모자람
    ],
)
def test_bad_response_fails(text):
    with pytest.raises(kma_hub.HubFormatError):
        kma_hub.parse_stn_inf(text)


def test_duplicate_number_in_response_fails():
    dup = HEAD + NOW_ROWS + NOW_ROWS.splitlines()[0] + "\n" + TAIL
    with pytest.raises(st.StationDataError):
        st.build_rows(kma_hub.parse_stn_inf(dup), {}, db_stations=st.DB_STATIONS, sido_lookup=fake_lookup)


def test_name_with_space_in_english():
    rows = kma_hub.parse_stn_inf(PAST_TEXT)
    old = [r for r in rows if r["STN_ID"] == "103"][0]
    assert old["STN_KO"] == "옛지점" and old["STN_EN"] == "Old Station" and old["LAW_ID"] == "4273000000"


def test_euc_kr_body_is_read():
    def http(url, timeout):
        return 200, NOW_TEXT.encode("cp949")

    rows = kma_hub.fetch_stn_inf(FAKE_KEY, "202610281700", http=http)
    assert rows[0]["STN_KO"] == "속초"


def test_http_error_fails_and_hides_key():
    def http(url, timeout):
        assert f"authKey={FAKE_KEY}" in url
        return 401, b'{"result":{"status":401,"message":"bad key"}}'

    with pytest.raises(kma_hub.HubError) as e:
        kma_hub.fetch_stn_inf(FAKE_KEY, "202610281700", http=http)
    assert FAKE_KEY not in str(e.value)
    assert "authKey=***" in str(e.value)


def test_connection_error_hides_key():
    def http(url, timeout):
        raise OSError(f"timeout for {url}")

    with pytest.raises(kma_hub.HubError) as e:
        kma_hub.fetch_stn_inf(FAKE_KEY, "202610281700", http=http)
    assert FAKE_KEY not in str(e.value)


# ---- 실행 (파일만 쓰고 DB는 쓰지 않음) ----


def fake_http(url, timeout):
    assert "inf=SFC" in url
    tm = url.split("tm=")[1][:8]
    if tm.endswith("0101") and tm[:4] < "2026":
        return 200, (
            PAST_TEXT if tm[:4] >= "2010" else HEAD + PAST_ROWS.splitlines()[0] + "\n" + TAIL
        ).encode()
    return 200, NOW_TEXT.encode("cp949")


def test_run_writes_csv_and_diff(tmp_path, capsys):
    code = stations_sync.main(
        ["--out", str(tmp_path), "--date", "2026-10-28", "--since", "2009"],
        key=FAKE_KEY,
        http=fake_http,
        sido_lookup=fake_lookup,
    )
    assert code == 0
    csv_path = tmp_path / "stations_kma_20261028.csv"
    md_path = tmp_path / "stations_diff.md"
    assert csv_path.exists() and md_path.exists()
    text = csv_path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(st.COLUMNS)
    assert "103," in text  # 폐지 지점도 남김
    md = md_path.read_text(encoding="utf-8")
    assert "기상청" in md and "(가)" in md
    out = capsys.readouterr().out
    assert FAKE_KEY not in out and FAKE_KEY not in text and FAKE_KEY not in md


def test_run_fails_loudly_and_writes_nothing(tmp_path, capsys):
    def http(url, timeout):
        return 200, b""

    code = stations_sync.main(
        ["--out", str(tmp_path), "--date", "2026-10-28", "--since", "2025"],
        key=FAKE_KEY,
        http=http,
        sido_lookup=fake_lookup,
    )
    assert code == 1
    assert list(tmp_path.iterdir()) == []
    captured = capsys.readouterr()
    assert "실패" in captured.out + captured.err
    assert FAKE_KEY not in captured.out + captured.err


def test_collector_does_not_touch_db():
    src = stations_sync.__file__
    text = open(src, encoding="utf-8").read()
    assert "supabase" not in text.lower()
