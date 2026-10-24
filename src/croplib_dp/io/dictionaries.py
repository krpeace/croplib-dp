"""Supabase에서 사전 자료 읽기: 지점, 작물, 작형, 재배 방식별 Kc, 토성, 계산 조건 (데이터 계약 v1.1).

읽기만 한다. 서버 키(SUPABASE_SERVICE_ROLE_KEY)는 이 저장소의 수집기에서만 쓴다.
읽은 줄을 계산 모듈의 입력(StationMeta, CropParams, Soil)으로 바꾸는 함수도 여기 둔다.
"""

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from croplib_dp.core.daily_weather import StationMeta
from croplib_dp.core.water_balance import CropParams, Soil

TABLES = ("stations", "crops", "crop_seasons", "crop_kc_sets", "soil_types", "scenarios")
RestGet = Callable[[str, dict], tuple[int, bytes]]


def rest_get(url: str, headers: dict) -> tuple[int, bytes]:
    import requests

    r = requests.get(url, headers=headers, timeout=30)
    return r.status_code, r.content


def read_table(base_url: str, key: str, table: str, http: RestGet = rest_get) -> list[dict]:
    """PostgREST로 표 전체를 읽는다. 실패하면 오류를 낸다(조용히 넘어가지 않는다)."""
    url = f"{base_url.rstrip('/')}/rest/v1/{table}?select=*"
    status, body = http(url, {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"})
    if status != 200:
        text = (
            body.decode("utf-8", "replace")[:200] if isinstance(body, bytes | bytearray) else str(body)[:200]
        )
        raise RuntimeError(f"Supabase {table} 읽기 실패: HTTP {status} {text}")
    return json.loads(body)


@dataclass
class Dictionaries:
    stations: dict[str, dict] = field(default_factory=dict)
    crops: dict[str, dict] = field(default_factory=dict)
    seasons: dict[str, dict] = field(default_factory=dict)
    kc_sets: dict[str, dict] = field(default_factory=dict)
    soils: dict[str, dict] = field(default_factory=dict)
    scenarios: list[dict] = field(default_factory=list)

    @classmethod
    def from_rows(cls, rows: dict[str, list[dict]]) -> "Dictionaries":
        return cls(
            stations={str(r["stn_id"]): r for r in rows["stations"]},
            crops={r["crop_id"]: r for r in rows["crops"]},
            seasons={r["season_type_id"]: r for r in rows["crop_seasons"]},
            kc_sets={r["kc_set_id"]: r for r in rows["crop_kc_sets"]},
            soils={r["soil_code"]: r for r in rows["soil_types"]},
            scenarios=sorted(rows["scenarios"], key=lambda r: r["scenario_id"]),
        )

    def scenarios_for(self, stn_id: str, year: int | None = None) -> list[dict]:
        return [
            s
            for s in self.scenarios
            if str(s["stn_id"]) == str(stn_id) and (year is None or s["season_year"] == year)
        ]


def load_dictionaries(base_url: str, key: str, http: RestGet = rest_get) -> Dictionaries:
    return Dictionaries.from_rows({t: read_table(base_url, key, t, http) for t in TABLES})


def _date(v: Any) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])


def station_meta(st: dict) -> StationMeta:
    return StationMeta(
        lat=float(st["lat"]), elev_m=float(st["elev_m"]), wind_height_m=float(st["wind_height_m"])
    )


def crop_params(d: Dictionaries, scenario: dict) -> CropParams:
    """계산 조건 → 작물 조건 (작형의 생육단계 일수 + 재배 방식의 Kc 표값 + 그해 작기 시작일)"""
    season = d.seasons[scenario["season_type_id"]]
    kc = d.kc_sets[scenario["kc_set_id"]]
    return CropParams(
        season_start=_date(scenario["season_start"]),
        l_ini=int(season["l_ini_d"]),
        l_dev=int(season["l_dev_d"]),
        l_mid=int(season["l_mid_d"]),
        l_late=int(season["l_late_d"]),
        kc_ini=float(kc["kc_ini"]),
        kc_mid=float(kc["kc_mid"]),
        kc_end=float(kc["kc_end"]),
    )


def soil_of(scenario: dict) -> Soil:
    return Soil(
        taw_mm=float(scenario["taw_mm"]), raw_mm=float(scenario["raw_mm"]), dr0_mm=float(scenario["dr0_mm"])
    )


def crop_height(d: Dictionaries, scenario: dict) -> float | None:
    h = d.crops[scenario["crop_id"]].get("h_m")
    return None if h is None else float(h)
