"""농장 계산의 DB 읽기 · 쓰기 (이슈 #131, 결정 0040).

서버 키(SUPABASE_SERVICE_ROLE_KEY)로 읽고 쓴다. 이 키는 RLS 를 건너뛰므로 이 저장소의 수집기에서만 쓴다.
- 읽기는 표마다 한 번(쪽 나눔)에 읽고 농장별로 나누는 것은 부르는 쪽이 한다(요청 수가 농장 수에 비례하지 않게).
  지점별 기상 · 회차 · 전망 줄은 지점마다 한 번.
- 쓰기: 농장별 결과 표 세 개(upsert), 계산 상태(done_at · result · error · calc_version 만),
  줄 정리(삭제). **삭제 조건에는 늘 `farm_id=eq.<그 농장>`이 들어간다**(조건이 비면 예외 — 표 전체를 지우는 일이 없게).
- 오류 문구에는 농장 줄 내용이 들어갈 수 있어 부르는 쪽이 짧은 말로 바꿔 쓴다(여기서는 그대로 올린다).
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from croplib_dp.io.dictionaries import RestGet, read_pages, read_table, rest_get
from croplib_dp.io.supabase_write import Request, SupabaseWriter, http_request

CHUNK = 50  # in.(…) 조건에 한 번에 넣는 값 수
DEL_CHUNK = 100  # 날짜 삭제를 한 번에 보내는 날짜 수
UUID = re.compile(r"^[0-9a-fA-F-]{32,36}$")
FARM_TABLES = ("farm_water_daily", "farm_water_outlook", "farm_water_outlook_summary")


def _chunks(items: Sequence[Any], n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


class FarmDb:
    def __init__(
        self,
        base_url: str,
        key: str,
        http: RestGet = rest_get,
        post: Request = http_request,
        writer: SupabaseWriter | None = None,
    ):
        self.base, self.key, self.http, self.post = base_url.rstrip("/"), key, http, post
        self.w = writer or SupabaseWriter(base_url, key, http=post)

    # ── 읽기: 표마다 한 번 ────────────────────────────────────────────────
    def _all(self, table: str, select: str, order: str, extra: str = "") -> list[dict]:
        return read_pages(self.base, self.key, table, self.http, f"select={select}&order={order}{extra}")

    def farms(self) -> list[dict]:
        return self._all(
            "farms",
            "farm_id,stn_id,soil_code,irrigated_area_m2,crop_id,season_type_id,kc_set_id",
            "farm_id",
        )

    def seasons(self) -> list[dict]:
        return self._all(
            "farm_seasons",
            "farm_season_id,farm_id,crop_id,season_type_id,kc_set_id,start_date,end_date,start_unset",
            "farm_id,start_date",
        )

    def logs(self) -> list[dict]:
        return self._all("irrigation_logs", "log_id,farm_id,date,volume_l", "farm_id,date,log_id")

    def status(self) -> list[dict]:
        return self._all("farm_calc_status", "farm_id,requested_at,recalc_from,done_at,result", "farm_id")

    def existing_daily(self, farm_ids: Sequence[str]) -> list[dict]:
        """이번에 계산하는 농장들의 기존 farm_water_daily (farm_id · date) — 줄 정리용. 농장 묶음마다 한 번."""
        out: list[dict] = []
        for part in _chunks(list(farm_ids), CHUNK):
            out += self._all(
                "farm_water_daily", "farm_id,date", "farm_id,date", f"&farm_id=in.({','.join(part)})"
            )
        return out

    # ── 읽기: 지점마다 한 번 ───────────────────────────────────────────────
    def weather(self, stn_id: str, start: str) -> list[dict]:
        """지점의 weather_daily (start 부터, 날짜순). 수집기가 만든 값이라 ASOS 를 다시 부르지 않는다."""
        return self._all(
            "weather_daily",
            "date,eto_mm,rain_mm,filled,u2_ms,rh_min_pct",
            "date",
            f"&stn_id=eq.{stn_id}&date=gte.{start}",
        )

    def latest_fetched(self, stn_id: str) -> str | None:
        """그 지점 weather_daily 의 가장 늦은 fetched_at (회차 기록이 없는 지점의 '오래된 농장' 기준)"""
        rows = read_table(
            self.base,
            self.key,
            "weather_daily",
            self.http,
            f"select=fetched_at&stn_id=eq.{stn_id}&order=fetched_at.desc&limit=1",
        )
        return rows[0]["fetched_at"] if rows else None

    # ── 읽기: 회차 · 공개 전망 ────────────────────────────────────────────
    def runs(self, since_iso: str) -> list[dict]:
        """최근 회차 기록 전부(모든 지점, 며칠치). 지점별 최신 회차와 예보표 되살리기 · 오래된 농장 기준에 쓴다."""
        return self._all(
            "forecast_runs",
            "run_id,stn_id,run_at,run_name,result,src_run_at,finished_at",
            "run_at.desc,run_id",
            f"&run_at=gte.{since_iso}",
        )

    def outlook_src(self, run_ids: Sequence[Any]) -> list[dict]:
        """그 회차들의 공개 전망 줄(모든 시나리오). 예보 ETo · 비 · 강수확률은 지점 값이라 한 시나리오분만 쓴다."""
        out: list[dict] = []
        for part in _chunks([str(i) for i in run_ids], CHUNK):
            out += self._all(
                "water_outlook",
                "run_id,scenario_id,target_date,lead_day,eto_mm,rain_fcst_mm,pop_pct,rain_expected_mm",
                "run_id,scenario_id,target_date",
                f"&run_id=in.({','.join(part)})",
            )
        return out

    # ── 쓰기 ─────────────────────────────────────────────────────────────
    def sync_all(self) -> int:
        """`public.sync_all_farm_seasons()` — 모든 농장의 작기 줄을 한국 오늘 기준으로 만들고 고친다. 처리한 농장 수."""
        url = f"{self.base}/rest/v1/rpc/sync_all_farm_seasons"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        status, body = self.post("POST", url, headers, b"{}")
        if status != 200:
            raise RuntimeError(f"sync_all_farm_seasons() 호출 실패: HTTP {status}")
        return int(json.loads(body))

    def upsert(self, table: str, rows: Iterable[Mapping[str, Any]]) -> None:
        keys = {
            "farm_water_daily": ("farm_id", "date"),
            "farm_water_outlook": ("farm_id", "target_date"),
            "farm_water_outlook_summary": ("farm_id",),
            "farm_calc_status": ("farm_id",),
        }
        rows = list(rows)
        if rows:
            self.w.upsert(table, rows, on_conflict=keys[table])

    def put_status(
        self, farm_id: str, done_at: str, result: str, error: str | None, calc_version: str
    ) -> None:
        """farm_calc_status 의 done_at · result · error · calc_version 만 쓴다(requested_at · recalc_from 은 DB 트리거 몫)."""
        self.upsert(
            "farm_calc_status",
            [dict(farm_id=farm_id, done_at=done_at, result=result, error=error, calc_version=calc_version)],
        )

    def _guard(self, farm_id: str, table: str) -> str:
        if table not in FARM_TABLES:
            raise ValueError(f"농장 결과 표가 아닙니다: {table}")
        if not isinstance(farm_id, str) or not UUID.match(farm_id):
            raise ValueError("농장 번호가 올바르지 않아 지우지 않습니다")
        return f"farm_id=eq.{farm_id}"

    def delete_daily(self, farm_id: str, dates: Sequence[str]) -> int:
        """그 농장의 farm_water_daily 중 dates 에 드는 줄을 지운다. 지운 줄 수."""
        base = self._guard(farm_id, "farm_water_daily")
        n = 0
        for part in _chunks(list(dates), DEL_CHUNK):
            n += len(self.w.delete("farm_water_daily", f"{base}&date=in.({','.join(part)})", select="date"))
        return n

    def delete_outlook(self, farm_id: str, keep_run_id: Any = None) -> int:
        """그 농장의 전망 줄을 지운다. keep_run_id 를 주면 그 회차 줄은 남긴다(최신 회차만 두려고)."""
        q = self._guard(farm_id, "farm_water_outlook")
        if keep_run_id is not None:
            q += f"&run_id=neq.{int(keep_run_id)}"
        return len(self.w.delete("farm_water_outlook", q, select="target_date"))

    def delete_summary(self, farm_id: str) -> int:
        q = self._guard(farm_id, "farm_water_outlook_summary")
        return len(self.w.delete("farm_water_outlook_summary", q, select="farm_id"))
