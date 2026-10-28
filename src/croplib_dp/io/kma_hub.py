"""기상청 API허브 조회: 지점정보(stn_inf) (이슈 #27).

- 키는 환경변수(KMA_HUB)에서 받아 URL에만 넣는다. 오류 문구·기록에는 mask_key로 감춘 주소만 남긴다.
- 응답은 글(텍스트) 표. '#' 줄은 설명이고, 열 이름 줄(STN_ID LON LAT ...)을 찾아 열 이름으로 읽는다.
  글자는 UTF-8이 아니면 EUC-KR(cp949)로 읽는다.
- 응답이 비었거나, 열 이름 줄이 없거나, 숫자가 아니거나, 지점이 하나도 없으면 HubFormatError로 멈춘다
  (조용히 넘어가지 않음). HTTP 오류·연결 오류는 HubError.
HTTP 함수(http)는 바꿔 끼울 수 있어서, 시험은 가짜 응답으로 한다.
"""

import re
import time
from datetime import datetime

from croplib_dp.io.kma import TIMEOUT_S, Http, http_get

STN_INF_URL = "https://apihub.kma.go.kr/api/typ01/url/stn_inf.php"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "croplib-dp (https://github.com/krpeace/croplib-dp)"
REQUIRED = ("STN_ID", "LON", "LAT", "STN_KO")


class HubError(RuntimeError):
    """기상청 API허브 조회 실패"""


class HubFormatError(HubError):
    """응답이 비었거나 지점 목록 형식이 아님"""


def mask_key(text: str) -> str:
    """주소·오류 문구에서 키를 감춘다"""
    return re.sub(r"authKey=[^&\s'\"]*", "authKey=***", str(text))


def stn_inf_url(key: str, tm: str | datetime, inf: str = "SFC") -> str:
    """지점정보 주소. tm: 기준 시각(YYYYMMDDHHMM) — 그때 운영 중인 지점이 나온다"""
    tm = tm.strftime("%Y%m%d%H%M") if isinstance(tm, datetime) else str(tm)
    return f"{STN_INF_URL}?inf={inf}&stn=&tm={tm}&help=1&authKey={(key or '').strip()}"


def decode(body: bytes | str) -> str:
    if isinstance(body, str):
        return body
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp949", "replace")


def _header(lines: list[str]) -> tuple[int, list[str]]:
    for i, line in enumerate(lines):
        toks = line.lstrip("#").split()
        if "STN_ID" in toks and "LON" in toks and "LAT" in toks:
            return i, toks
        if "STN" in toks and "LON" in toks and "LAT" in toks and "STN_KO" in toks:
            # 2줄 헤더(# STN ... STN STN_KO ... / # ID ... AD ...)를 열 이름으로 바꾼다
            cols: list[str] = []
            for j, tok in enumerate(toks):
                if tok == "STN":
                    cols.append("STN_ID" if j == 0 else "STN_AD")
                else:
                    cols.append(tok)
            skip = 1 if i + 1 < len(lines) and lines[i + 1].lstrip().startswith("#") else 0
            return i + skip, cols
    return -1, []


def _split(toks: list[str], cols: list[str]) -> list[str] | None:
    """한 줄의 값 → 열 수에 맞춘 값. 영문 이름(STN_EN)·법정동주소(LAW_ADDR) 띄어쓰기를 모은다"""
    n = len(cols)
    if cols and cols[-1] == "LAW_ADDR" and "BASIN" in cols and "STN_KO" in cols and "STN_EN" in cols:
        k = cols.index("STN_KO")
        if cols.index("BASIN") >= k + 4 and len(toks) >= k + 5:
            for pos in range(k + 4, len(toks) + 1):
                law = toks[pos - 2]
                if (len(law) == 10 and law.isdigit()) or law in ("-", "----"):
                    en = " ".join(toks[k + 1 : pos - 3])
                    addr = " ".join(toks[pos:])
                    if en:
                        return toks[: k + 1] + [en, toks[pos - 3], law, toks[pos - 1], addr]
        return None
    if len(toks) == n:
        return toks
    if len(toks) > n and "STN_KO" in cols and "STN_EN" in cols:
        k = cols.index("STN_KO")
        if cols.index("STN_EN") == k + 1:
            m = n - k - 2  # STN_EN 뒤 열 수
            mid = toks[k : len(toks) - m]
            return toks[:k] + [mid[0], " ".join(mid[1:])] + toks[len(toks) - m :]
    return None


def parse_stn_inf(text: str) -> list[dict]:
    """지점정보 응답 글 → 줄마다 {열 이름: 값(글자)}"""
    t = (text or "").strip()
    if not t:
        raise HubFormatError("응답이 비어 있음")
    if t[0] in "{<":
        raise HubFormatError(f"지점 목록이 아닌 응답: {mask_key(t[:160])!r}")
    lines = t.splitlines()
    at, cols = _header(lines)
    if at < 0:
        raise HubFormatError(f"열 이름 줄(STN_ID LON LAT)을 찾지 못함: {mask_key(t[:160])!r}")
    lack = [c for c in REQUIRED if c not in cols]
    if lack:
        raise HubFormatError(f"필요한 열이 없음: {lack}")
    rows: list[dict] = []
    for line in lines[at + 1 :]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        vals = _split(s.split(), cols)
        if vals is None:
            raise HubFormatError(f"열 수가 맞지 않는 줄 (열 {len(cols)}개): {s[:120]!r}")
        row = dict(zip(cols, vals, strict=True))
        try:
            int(row["STN_ID"])
            float(row["LAT"])
            float(row["LON"])
        except ValueError as e:
            raise HubFormatError(f"번호·좌표가 숫자가 아닌 줄: {s[:120]!r}") from e
        rows.append(row)
    if not rows:
        raise HubFormatError("지점이 하나도 없음")
    return rows


def fetch_stn_inf(
    key: str, tm: str | datetime, http: Http = http_get, timeout: float = TIMEOUT_S
) -> list[dict]:
    """기준 시각 tm에 운영 중인 지상(ASOS) 지점 목록. 실패하면 HubError"""
    url = stn_inf_url(key, tm)
    try:
        status, body = http(url, timeout)
    except Exception as e:  # 시간 초과·연결 오류 (문구에 주소가 들어 있을 수 있어 키를 감춘다)
        raise HubError(mask_key(f"지점정보 조회 실패 {type(e).__name__}: {str(e)[:160]} ({url})")) from None
    text = decode(body)
    if status != 200:
        raise HubError(mask_key(f"지점정보 조회 실패 HTTP {status}: {text[:160]!r} ({url})"))
    try:
        return parse_stn_inf(text)
    except HubFormatError as e:
        raise HubFormatError(mask_key(f"{e} ({url})")) from None


def reverse_sido(lat: float, lon: float, timeout: float = TIMEOUT_S) -> str:
    """좌표 → 시·도 이름 (OpenStreetMap Nominatim, 키 없음). 기상청 자료에 법정동코드가 없을 때만 쓴다.
    사용 규칙(초당 1회)을 지키려고 부를 때마다 1초 쉰다. 찾지 못하면 HubError"""
    import requests

    time.sleep(1.1)
    r = requests.get(
        NOMINATIM_URL,
        params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 5, "accept-language": "ko"},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise HubError(f"좌표→행정구역 실패 HTTP {r.status_code}")
    addr = (r.json() or {}).get("address", {}) or {}
    for k in ("province", "state", "city"):
        if addr.get(k):
            return str(addr[k])
    raise HubError(f"좌표→행정구역 결과에 시·도가 없음 ({lat}, {lon})")
