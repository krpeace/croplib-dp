"""보관·삭제 수집기 시험용 가짜 Supabase(표 + Storage). 입력은 모두 가짜 값이다.

`FakeSupabase.request`는 SupabaseWriter·Storage 가 쓰는 `(method, url, headers, body) → (status, body)` 모양이고,
`rest_get`은 읽기용 `(url, headers) → (status, body)`, `head`는 줄 수 세기용 `(…) → (status, headers, body)`이다.
모든 쓰기 호출(POST·PATCH·DELETE)은 `writes`에 남겨, dry-run 이 아무것도 쓰지 않았는지 확인할 수 있다.
"""

# ruff: noqa: E501  (한글 문장이 많아 ruff 가 전각 문자를 2칸으로 세어 110을 넘는 줄이 많다)

import json
import urllib.parse

import pandas as pd


def _parse(url: str):
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
    return u.path, {k: v[-1] for k, v in q.items()}


def _in_list(spec: str) -> list[str]:
    inner = spec[len("in.(") : -1]
    return [x.strip('"') for x in inner.split(",")] if inner else []


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {
            "forecast_runs": [],
            "water_outlook": [],
            "water_outlook_summary": [],
        }
        self.storage: dict[tuple[str, str], bytes] = {}
        self.writes: list[tuple[str, str]] = []  # (method, path) — POST·PATCH·DELETE 전부
        self.tamper_on_download: set[str] = set()  # 이 경로를 내려받으면 깨진 바이트를 준다
        self.upload_fail: set[str] = set()
        self.no_embed = False  # True 면 조인(forecast_runs!inner) 조회가 400 으로 실패한다

    # ── 표 ────────────────────────────────────────────────────────────
    def _run(self, run_id):
        return next((r for r in self.tables["forecast_runs"] if r["run_id"] == run_id), None)

    def _select_rows(self, table: str, params: dict) -> list[dict]:
        rows = [dict(r) for r in self.tables[table]]
        sel = params.get("select", "*")
        embed = "forecast_runs!inner" in sel
        out = []
        for r in rows:
            run = self._run(r.get("run_id")) if table != "forecast_runs" else None
            if embed:
                if run is None:
                    continue
                r["forecast_runs"] = {k: run[k] for k in ("run_at", "run_name", "stn_id", "collector")}
            out.append(r)
        for key, val in params.items():
            if key == "run_at" and val.startswith("lt."):
                lim = pd.Timestamp(val[3:])
                out = [r for r in out if pd.Timestamp(r["run_at"]) < lim]
            elif key == "forecast_runs.run_at" and val.startswith("lt."):
                lim = pd.Timestamp(val[3:])
                out = [r for r in out if pd.Timestamp(r["forecast_runs"]["run_at"]) < lim]
            elif key in ("run_id", "raw_path") and val.startswith("in."):
                keep = set(_in_list(val))
                out = [r for r in out if str(r.get(key)) in keep]
        order = params.get("order")
        if order:
            cols = order.split(",")
            out.sort(key=lambda r: tuple(str(r.get(c)) for c in cols))
        if "offset" in params or "limit" in params:
            o, n = int(params.get("offset", 0)), int(params.get("limit", 1000))
            out = out[o : o + n]
        cols = [c for c in sel.split(",") if c != "*" and "(" not in c and "!" not in c]
        if cols and "*" not in sel:
            out = [{c: r.get(c) for c in cols} for r in out]
        return out

    def rest_get(self, url, headers):
        path, params = _parse(url)
        table = path.rsplit("/", 1)[-1]
        if self.no_embed and "forecast_runs!inner" in params.get("select", ""):
            return 400, b'{"code":"PGRST200","message":"Could not find a relationship between tables"}'
        return 200, json.dumps(self._select_rows(table, params)).encode()

    def head(self, method, url, headers, body):
        path, params = _parse(url)
        table = path.rsplit("/", 1)[-1]
        n = len(self._select_rows(table, {k: v for k, v in params.items() if k not in ("limit", "offset")}))
        return 200, {"Content-Range": f"0-0/{n}" if n else "*/0"}, b"[]"

    # ── 쓰기·Storage ───────────────────────────────────────────────────
    def request(self, method, url, headers, body):
        path, params = _parse(url)
        if method != "GET" and not path.startswith("/storage/v1/object/list/"):  # 목록 조회는 읽기다
            self.writes.append((method, path))
        if path.startswith("/rest/v1/"):
            table = path.rsplit("/", 1)[-1]
            return self._rest_write(method, table, params, body)
        return self._storage(method, path, body)

    def _rest_write(self, method, table, params, body):
        if method == "POST":  # upsert
            rows = json.loads(body)
            key = params["on_conflict"].split(",")
            for r in rows:
                for i, old in enumerate(self.tables[table]):
                    if all(str(old.get(k)) == str(r.get(k)) for k in key):
                        self.tables[table][i] = dict(old, **r)
                        break
                else:
                    self.tables[table].append(dict(r))
            return 201, b""
        # 조건에 맞는 원본 줄을 고른다
        match = []
        for r in self.tables[table]:
            ok = True
            for k, v in params.items():
                if k in ("run_id", "raw_path") and v.startswith("in."):
                    ok &= str(r.get(k)) in set(_in_list(v))
            if ok:
                match.append(r)
        sel = [c for c in params.get("select", "").split(",") if c]
        if method == "DELETE":
            for r in match:
                self.tables[table].remove(r)
        elif method == "PATCH":
            for r in match:
                r.update(json.loads(body))
        return 200, json.dumps([{c: r.get(c) for c in sel} if sel else r for r in match]).encode()

    def _storage(self, method, path, body):
        parts = path[len("/storage/v1/object/") :]
        if parts.startswith("list/"):
            bucket = parts[len("list/") :]
            req = json.loads(body)
            prefix = req.get("prefix", "")
            names: dict[str, dict] = {}
            for (b, p), data in sorted(self.storage.items()):
                if b != bucket or not p.startswith(prefix):
                    continue
                rest = p[len(prefix) :].lstrip("/") if prefix else p
                head, _, tail = rest.partition("/")
                if tail:
                    names.setdefault(head, {"name": head, "id": None, "metadata": None})
                else:
                    names[head] = {"name": head, "id": f"id-{p}", "metadata": {"size": len(data)}}
            items = list(names.values())
            off, lim = req.get("offset", 0), req.get("limit", 100)
            return 200, json.dumps(items[off : off + lim]).encode()
        if method == "DELETE":
            bucket = parts
            paths = json.loads(body)["prefixes"]
            gone = [p for p in paths if (bucket, p) in self.storage]
            for p in gone:
                del self.storage[(bucket, p)]
            return 200, json.dumps([{"name": p} for p in gone]).encode()
        bucket, _, p = parts.partition("/")
        if method == "POST":
            if p in self.upload_fail:
                return 500, b"fail"
            self.storage[(bucket, p)] = body
            return 200, b"{}"
        if method == "GET":
            if (bucket, p) not in self.storage:
                return 400, b'{"statusCode":"404","error":"not_found","message":"Object not found"}'
            data = self.storage[(bucket, p)]
            return 200, (b"tampered" if p in self.tamper_on_download else data)
        return 405, b""

    # ── 시험용 도우미 ──────────────────────────────────────────────────
    def add_run(self, run_id, run_at, stn_id="101", run_name="아침", collector="actions", raw_path=None):
        self.tables["forecast_runs"].append(
            dict(
                run_id=run_id,
                run_at=run_at,
                run_name=run_name,
                stn_id=stn_id,
                collector=collector,
                raw_path=raw_path,
            )
        )

    def add_outlook(self, run_id, scenario="S1", days=(0, 1, 2), eto=3.0):
        for lead in days:
            self.tables["water_outlook"].append(
                dict(
                    run_id=run_id, scenario_id=scenario, target_date=f"2026-10-{20 + lead:02d}", lead_day=lead,
                    eto_mm=eto + lead / 10, etc_mm=2.5, etc_err_mm=0.4, rain_fcst_mm=None, pop_pct=10,
                    rain_expected_mm=0.2, dr_center_mm=10.0, dr_early_mm=9.0, dr_late_mm=11.0, status="normal",
                    calc_version="v1.5",
                )
            )  # fmt: skip

    def add_summary(self, run_id, scenario="S1"):
        self.tables["water_outlook_summary"].append(
            dict(
                run_id=run_id, scenario_id=scenario, horizon_days=3, start_date="2026-10-20", dr_start_mm=5.0,
                status_start="normal", need_center_in=2.0, need_early_in=1.0, need_late_in=3.0,
                advice_net_mm=12.0, advice_gross_mm=15.0, etc_sum_mm=8.0, etc_sum_err_mm=1.0, rain_sum_mm=0.5,
                rain_expected_sum_mm=0.3, calc_version="v1.5",
            )
        )  # fmt: skip
