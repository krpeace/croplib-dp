"""KREI 농업관측 API 접속 시험 (표준 라이브러리만 사용).

사용: python scripts/krei_probe.py [--yyyy 2026 --mm 11] [--no-pdf]
- 세 API(품목분류·관측월보·관측속보)를 부르고 상태·응답시간·item 수·앞 몇 건을 출력
- 월보 첫 PDF를 실제로 내려받아 %PDF 시작·크기·SHA256 확인
- GitHub Actions에서는 실행 요약(GITHUB_STEP_SUMMARY)에도 결과를 씀
- 하나라도 실패하면 종료 코드 1
"""

import argparse
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

BASE = os.getenv("KREI_BASE", "https://aglook.krei.re.kr/main/api")
UA = {"User-Agent": "croplib-dp-krei-probe/0.1"}
FIELDS = [
    "yyyy",
    "mm",
    "ovr_item_code",
    "prt_ovr_item_code",
    "item_name",
    "prt_item_name",
    "title",
    "pdf_link",
    "reg_date",
    "mod_date",
]
out = []


def log(s=""):
    print(s)
    out.append(s)


def get(url, timeout=60):
    t0 = time.time()
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, dict(r.headers), body, time.time() - t0, None
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b"", time.time() - t0, str(e)
    except Exception as e:  # 연결 거부·시간 초과·DNS 등
        return None, {}, b"", time.time() - t0, f"{type(e).__name__}: {e}"


def probe_list(name, path, params):
    q = "&".join(f"{k}={v}" for k, v in params.items() if v)
    url = f"{BASE}/{path}" + (f"?{q}" if q else "")
    st, hd, body, dt, err = get(url)
    log(
        f"\n## {name}\n- URL: {url}\n- 상태: {st}  시간: {dt:.1f}s  "
        f"크기: {len(body)}B  형식: {hd.get('Content-Type')}"
    )
    if err or st != 200:
        log(f"- ❌ 실패: {err}\n- 본문 앞부분: {body[:300]!r}")
        return False, []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        log(f"- ❌ XML 아님: {e}\n- 본문 앞부분: {body[:300]!r}")
        return False, []
    items = root.findall(".//item")
    log(f"- ✅ item {len(items)}건")
    rows = []
    for it in items:
        rows.append({c.tag.strip(): (c.text or "").strip() for c in it})
    for r in rows[:5]:
        log("  - " + " | ".join(f"{k}={r.get(k, '')[:60]}" for k in FIELDS if k in r or k == "item_name"))
    if rows:
        log(f"- 필드: {sorted(rows[0].keys())}")
    return True, rows


def probe_pdf(rows):
    links = [r.get("pdf_link") for r in rows if r.get("pdf_link")]
    log("\n## PDF 내려받기")
    if not links:
        log("- ⚠️ pdf_link 없음")
        return False
    url = links[0] if links[0].startswith("http") else BASE.split("/main/api")[0] + links[0]
    st, hd, body, dt, err = get(url, timeout=120)
    ok = st == 200 and body[:4] == b"%PDF"
    sha = hashlib.sha256(body).hexdigest()[:16] if body else "-"
    log(
        f"- URL: {url}\n- 상태: {st}  시간: {dt:.1f}s  "
        f"크기: {len(body) / 1e6:.2f}MB  형식: {hd.get('Content-Type')}"
    )
    log(f"- {'✅ PDF 확인' if ok else '❌ PDF 아님/실패'}  sha256={sha}  {err or ''}")
    log(f"- PDF 링크 개수: {len(links)}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yyyy", default="")
    ap.add_argument("--mm", default="")
    ap.add_argument("--no-pdf", action="store_true")
    a = ap.parse_args()
    where = "GitHub Actions" if os.getenv("GITHUB_ACTIONS") else "로컬"
    log(f"# KREI API 접속 시험 ({where}, {time.strftime('%Y-%m-%d %H:%M:%S %Z')})")
    p = {"yyyy": a.yyyy, "mm": a.mm}
    ok1, _ = probe_list("품목분류", "reportItem", {})
    ok2, mrows = probe_list("관측월보", "montlyReport", p)
    ok3, _ = probe_list("관측속보", "newsReport", p)
    ok4 = True if a.no_pdf else probe_pdf(mrows)
    allok = ok1 and ok2 and ok3 and ok4
    log(f"\n**결과: {'✅ 모두 성공' if allok else '❌ 실패 있음'}**")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
