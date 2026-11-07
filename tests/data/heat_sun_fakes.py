"""적산온도·적산일사 시험용 가짜 ASOS·가짜 시계·가짜 지점 사전.

`tests/test_heat_sun_collector.py`와 `tests/test_monitor.py`가 함께 쓴다. 입력은 모두 가짜 값이다.
기존 `test_heat_sun_collector.py`에 있던 것을 옮겼고, 동작은 같다. 새로 더한 것은 하나뿐이다:
시작일이 종료일보다 늦은 요청에는 오류 응답을 돌려준다 (이슈 #40).
"""

import datetime as dt
import json
import urllib.parse

import pandas as pd

from croplib_dp.io.dictionaries import Dictionaries

KEY = "FAKE+KEY/dummy-value=="


def _item(d: dt.date, gsr=True) -> dict:
    doy = d.timetuple().tm_yday
    warm = 12 + 12 * (1 - abs(doy - 200) / 200)  # 여름에 따뜻한 가짜 기온
    bump = 1.0 if d.year == 2026 else 0.0  # 올해는 평년보다 1도 따뜻
    return dict(
        tm=d.isoformat(),
        maxTa=f"{warm + 5 + bump:.1f}",
        minTa=f"{warm - 5 + bump:.1f}",
        avgTa=f"{warm + bump:.1f}",
        sumGsr=(f"{10 + doy % 7}" if gsr else ""),
    )


class FakeAsos:
    def __init__(self, missing=(), no_gsr=("216",), fail_stn=None, nodata_years=()):
        self.missing, self.no_gsr, self.fail_stn = set(missing), set(no_gsr), fail_stn
        self.nodata_years = set(nodata_years)  # 이 해는 관측이 없다 (nodata 응답)
        self.calls: list[tuple[str, str, str]] = []  # 모든 요청 (지점, 시작, 끝)

    @property
    def reversed_calls(self) -> list[tuple[str, str, str]]:
        """시작일이 종료일보다 늦은 요청 (있으면 안 된다)"""
        return [c for c in self.calls if c[1] > c[2]]

    def __call__(self, url, timeout):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert q["serviceKey"] == KEY
        stn, s, e = q["stnIds"], q["startDt"], q["endDt"]
        self.calls.append((stn, s, e))
        if stn == self.fail_stn:
            return 500, b"server error"
        d0, d1 = (
            dt.date.fromisoformat(f"{s[:4]}-{s[4:6]}-{s[6:]}"),
            dt.date.fromisoformat(f"{e[:4]}-{e[4:6]}-{e[6:]}"),
        )
        if d0 > d1:
            # 역전 구간은 오류로 거절한다. 오류 코드는 시험용 가짜 값이며 실제 기상청 응답이 아니다
            # (정상 코드 "00"·빈 목록으로 받아 주지 않는다).
            return 200, json.dumps(
                {"response": {"header": {"resultCode": "10", "resultMsg": "FAKE_REVERSED_RANGE"}}}
            ).encode()
        items = [
            _item(d0 + dt.timedelta(days=k), gsr=stn not in self.no_gsr)
            for k in range((d1 - d0).days + 1)
            if d0 + dt.timedelta(days=k) not in self.missing and d0.year not in self.nodata_years
        ]
        page, rows = int(q["pageNo"]), int(q["numOfRows"])
        part = items[(page - 1) * rows : page * rows]
        body = {"items": {"item": part}, "totalCount": len(items)}
        return 200, json.dumps(
            {"response": {"header": {"resultCode": "00", "resultMsg": "OK"}, "body": body}}
        ).encode()


class FakeClock:
    def __init__(self, t):
        self.t = pd.Timestamp(t)
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept += s


def _dicts() -> Dictionaries:
    st = lambda i, n, act=True: dict(  # noqa: E731
        stn_id=i, name_ko=n, lat=37.0, lon=127.0, elev_m=50, wind_height_m=10, is_active=act
    )
    crops = [
        dict(crop_id="apple", name_ko="사과", t_base_c=4, gdd_stages=[500, 1000]),
        dict(crop_id="kimchi_cabbage", name_ko="배추", t_base_c=4, gdd_stages=[300]),
    ]
    return Dictionaries.from_rows(
        dict(
            stations=[st("101", "춘천"), st("216", "태백"), st("999", "폐지지점", act=False)],
            crops=crops,
            crop_seasons=[],
            crop_kc_sets=[],
            soil_types=[],
            scenarios=[],
        )
    )


def dicts_with_common() -> Dictionaries:
    """_dicts() 에 작물 공통(기준온도 5℃)을 더한 것. 감시가 보는 줄(crop_id = common)을 시험할 때 쓴다."""
    d = _dicts()
    d.crops["common"] = dict(crop_id="common", name_ko="작물 공통", t_base_c=5, gdd_stages=[1000, 2000])
    return d
