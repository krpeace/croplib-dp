"""지난 해 ASOS 일자료 캐시 (io/asos_cache.py). 가짜 값으로 임시 폴더에서 돈다."""

from pathlib import Path

from croplib_dp.io.asos_cache import KEEP_FIELDS, AsosCache, is_complete

ITEMS = [{"tm": "2025-01-01", "maxTa": "3.1", "minTa": "-4.0"}, {"tm": "2025-12-31", "maxTa": "2.0"}]


def test_roundtrip_and_path(tmp_path):
    c = AsosCache(tmp_path)
    assert c.get("101", 2025) is None  # 없으면 None
    c.put("101", 2025, ITEMS)
    assert (tmp_path / "asos" / "101" / "2025.json").is_file()  # <cache>/asos/<stn_id>/<year>.json
    assert c.get("101", 2025) == ITEMS  # 받은 items 그대로
    assert c.get("101", 2024) is None and c.get("108", 2025) is None


def test_only_calculation_fields_are_kept(tmp_path):
    """계산에 쓰는 칸(tm·avgTa·maxTa·minTa·sumGsr)만 남긴다 — 응답의 60여 칸을 다 저장하지 않는다."""
    assert KEEP_FIELDS == ("tm", "avgTa", "maxTa", "minTa", "sumGsr")
    full = dict(
        tm="2025-03-01", avgTa="5.0", maxTa="9.0", minTa="1.0", sumGsr="12.3", avgWs="2.1", stnId="101"
    )
    c = AsosCache(tmp_path)
    c.put("101", 2025, [full])
    assert c.get("101", 2025) == [dict(tm="2025-03-01", avgTa="5.0", maxTa="9.0", minTa="1.0", sumGsr="12.3")]
    c.put("101", 2025, [dict(tm="2025-03-02", maxTa="1.0", avgWs="3")])  # 없는 칸은 만들지 않는다
    assert c.get("101", 2025) == [dict(tm="2025-03-02", maxTa="1.0")]


def test_is_complete_year():
    """마지막 날짜가 12/31 이거나 그해 관측이 아예 없을 때만 다 받은 해로 본다."""
    assert is_complete([], 2025)  # nodata
    assert is_complete([{"tm": "2025-01-01"}, {"tm": "2025-12-31"}], 2025)
    assert is_complete([{"tm": "2025-12-31"}, {"tm": "2025-01-01"}], 2025)  # 순서와 상관없이
    assert not is_complete([{"tm": "2025-01-01"}, {"tm": "2025-12-30"}], 2025)  # 12/31 이 아직 안 올라옴
    assert not is_complete([{"tm": "깨진 날짜"}], 2025)
    assert not is_complete([{"tm": "2024-12-31"}], 2025)  # 다른 해 자료만 있으면 12/31 로 치지 않는다


def test_empty_list_is_a_valid_cache_entry(tmp_path):
    """nodata(그해 관측 없음)도 빈 목록으로 남긴다 — None(캐시 없음)과 구별되어야 한다."""
    c = AsosCache(tmp_path)
    c.put("101", 2018, [])
    assert c.get("101", 2018) == []


def test_broken_file_counts_as_missing(tmp_path):
    c = AsosCache(tmp_path)
    c.put("101", 2025, ITEMS)
    p = tmp_path / "asos" / "101" / "2025.json"
    for bad in ("{깨진 JSON", "", '{"tm": "2025-01-01"}', '"문자열"', "null"):
        p.write_text(bad, encoding="utf-8")
        assert c.get("101", 2025) is None, bad  # 오류를 내지 않고 '없음'으로 본다
    p.write_bytes(b"\xff\xfe\x00")  # 글자로 읽을 수 없는 파일도
    assert c.get("101", 2025) is None


def test_put_overwrites_broken_file(tmp_path):
    c = AsosCache(tmp_path)
    c.put("101", 2025, ITEMS)
    (tmp_path / "asos" / "101" / "2025.json").write_text("{깨짐", encoding="utf-8")
    c.put("101", 2025, ITEMS)
    assert c.get("101", 2025) == ITEMS


def test_put_failure_does_not_raise(tmp_path):
    """캐시 때문에 실패하지 않는다: 쓸 수 없는 곳이어도 오류 없이 넘어간다."""
    blocker = tmp_path / "file"
    blocker.write_text("폴더가 아니라 파일", encoding="utf-8")
    c = AsosCache(blocker)  # 이 아래에는 폴더를 만들 수 없다
    c.put("101", 2025, ITEMS)
    assert c.get("101", 2025) is None


def test_cleanup_failure_does_not_raise_either(tmp_path, monkeypatch):
    """리눅스에서는 '폴더가 아님'(NotADirectoryError)이 나서 임시 파일 정리도 실패할 수 있다.
    missing_ok 로는 못 막는다."""

    def boom(self, missing_ok=False):
        raise NotADirectoryError("폴더가 아님")

    monkeypatch.setattr(Path, "unlink", boom)
    blocker = tmp_path / "file"
    blocker.write_text("폴더가 아니라 파일", encoding="utf-8")
    AsosCache(blocker).put("101", 2025, ITEMS)  # 쓰기도, 정리도 실패해도 오류 없이 끝난다
