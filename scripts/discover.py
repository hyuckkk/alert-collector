"""Korean-language discovery search, run on GitHub Actions.

Why: the Claude judgement sessions search with a US-centred web index that barely
covers Korean local sources (구청 게시판 소식, 네이버 블로그/카페, 지역 매체). ChatGPT
caught items days earlier from exactly those sources. The Actions runner can read
Naver/Daum search result pages and Google News (ko-KR) directly, so we run every
JOB's keyword list here, keep only results we have not seen before, and each JOB
reads `snapshots/discover/<job>.json` first.

watch/queries.json: {"job5": ["헬기 탑승 체험 모집", ...], "job10": [...], ...}
Output per job: {"updated": iso, "new_this_run": n, "hits": [hit... last 7 days, newest first]}
hit = {"title", "url", "engine", "query", "first_seen", "pub"}
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ROOT / "watch" / "queries.json"
OUT = ROOT / "snapshots" / "discover"
SEEN = OUT / "_seen.json"
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

# Result links worth keeping, per engine (navigation/ads/help links are dropped).
KEEP = re.compile(
    r"(blog\.naver\.com/[^/?#]+/\d+|cafe\.naver\.com/[^?#]+/\d+|n\.news\.naver\.com/|v\.daum\.net/v/|"
    r"news\.google\.com/rss/articles/|/news/articleView\.html|/view/\d|/article/|\.go\.kr/|\.or\.kr/|"
    r"modoo\.io/content/|ggzine\.com/\d+|tistory\.com/\d+|post\.naver\.com/viewer|"
    # ticket / booking pages (Naver's integrated search shows a performance box linking here)
    r"ticket\.yes24\.com/(Perf|New/Perf)|nol\.yanolja\.com/ticket/(products|places)|tickets\.interpark\.com/goods|"
    r"ticketlink\.co\.kr/(product|help/notice)|booking\.naver\.com/booking|ticket\.melon\.com/performance|"
    r"\.kr/(bbs|board|notice|event|program|cop/bbs)|instagram\.com/p/)")
DROP = re.compile(r"(search\.naver\.com|search\.daum\.net|help\.|policy|keep\.naver|nid\.naver|"
                  r"channel/\d+/home|javascript:)")


def engines(q: str) -> list[tuple[str, str]]:
    e = urllib.parse.quote(q)
    return [
        ("naver_blog", f"https://search.naver.com/search.naver?ssc=tab.blog.all&query={e}&nso=so:dd,p:1w"),
        ("naver_news", f"https://search.naver.com/search.naver?where=news&query={e}&sort=1&pd=4"),
        ("naver_cafe", f"https://search.naver.com/search.naver?ssc=tab.cafe.all&query={e}&nso=so:dd,p:1w"),
        ("daum_news", f"https://search.daum.net/search?w=news&q={e}&sort=recency&period=w"),
        ("gnews", f"https://news.google.com/rss/search?q={e}+when:7d&hl=ko&gl=KR&ceid=KR:ko"),
        # 2026-10-08: web documents and the integrated result page (공연·행사 정보 박스, 예매 링크),
        # plus Daum blog/cafe — ChatGPT kept finding items that only appeared in these.
        ("naver_all", f"https://search.naver.com/search.naver?where=nexearch&query={e}"),
        ("naver_web", f"https://search.naver.com/search.naver?ssc=tab.web.all&query={e}"),
        ("daum_web", f"https://search.daum.net/search?w=web&q={e}"),
        ("daum_blog", f"https://search.daum.net/search?w=blog&q={e}&sort=recency"),
        ("daum_cafe", f"https://search.daum.net/search?w=cafe&q={e}&sort=recency"),
    ]


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[tuple[str, str]] = []
        self._href = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", html.unescape("".join(self._buf))).strip()
            self.out.append((self._href, text))
            self._href = None


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return raw.decode(r.headers.get_content_charset() or "utf-8", "replace")


def search(engine: str, url: str, q: str) -> list[dict]:
    body = get(url)
    hits: list[dict] = []
    if engine == "gnews":
        root = ET.fromstring(body)
        for it in root.iter("item"):
            hits.append({"title": (it.findtext("title") or "").strip(), "url": it.findtext("link") or "",
                         "pub": it.findtext("pubDate"), "engine": engine, "query": q})
        return hits
    p = Anchors()
    p.feed(body)
    best: dict[str, str] = {}
    for href, text in p.out:
        if not href or not href.startswith("http") or DROP.search(href) or not KEEP.search(href):
            continue
        if len(text) > len(best.get(href, "")):
            best[href] = text
    for href, text in best.items():
        if len(text) >= 8:  # the title anchor, not the thumbnail/"공유" anchors
            hits.append({"title": text[:200], "url": href, "engine": engine, "query": q})
    return hits


def seen_key(job: str, url: str) -> str:
    """Short hashed key. Plain "job|url" keys made _seen.json grow past 100 MB (Google News
    RSS links are very long), which GitHub refuses to store."""
    import hashlib
    return hashlib.sha1(f"{job}|{url}".encode("utf-8")).hexdigest()[:16]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    queries = json.loads(QUERIES.read_text(encoding="utf-8"))
    # No global "seen" file any more: it grew past 100 MB. A result is new when its URL is not in
    # this job's previous hit list (last 7 days, up to 1000 entries).
    now = datetime.now(KST)
    now_s = now.isoformat(timespec="seconds")
    # Naver answers 403 once one IP sends too many searches (10/8: blocked after ~2 runs at 5 Naver
    # tabs x all queries). Naver tabs therefore cover one third of the queries per hourly run
    # (each query every 3 hours) and go through a single slow lane; Daum/Google cover all every run.
    slot = now.hour % 3
    tasks = []
    for job, qs in queries.items():
        if job.startswith("_"):
            continue
        for qi, q in enumerate(qs):
            for eng, url in engines(q):
                if eng.startswith("naver") and (qi % 3) != slot:
                    continue
                tasks.append((job, q, eng, url))

    import threading
    naver_lane = threading.Lock()
    naver_blocked = threading.Event()

    def run(t):
        job, q, eng, url = t
        if eng.startswith("naver"):
            if naver_blocked.is_set():
                return job, eng, [], None
            with naver_lane:
                try:
                    return job, eng, search(eng, url, q), None
                except Exception as e:  # noqa: BLE001
                    if "403" in str(e) or "Forbidden" in str(e) or "429" in str(e):
                        naver_blocked.set()   # stop hammering; next run tries again
                    return job, eng, [], f"{eng} '{q}': {e}"[:200]
                finally:
                    time.sleep(1.5)
        try:
            return job, eng, search(eng, url, q), None
        except Exception as e:  # noqa: BLE001
            return job, eng, [], f"{eng} '{q}': {e}"[:200]
        finally:
            time.sleep(0.8)

    results: dict[str, list[dict]] = {}
    errors: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=int(os.environ.get("DISCOVER_WORKERS", "8"))) as ex:
        for job, eng, hits, err in ex.map(run, tasks):
            results.setdefault(job, []).extend(hits)
            if err:
                errors.setdefault(job, []).append(err)

    cutoff = (now - timedelta(days=7)).isoformat()
    for job in queries:
        if job.startswith("_"):
            continue
        path = OUT / f"{job}.json"
        prev = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        recent = {h["url"]: h for h in prev.get("hits", prev.get("recent", [])) if h.get("first_seen", "") >= cutoff}
        new = []
        known = {h["url"] for h in prev.get("hits", [])}
        for h in results.get(job, []):
            if h["url"] in known or h["url"] in recent:
                continue
            h["first_seen"] = now_s
            new.append(h)
            recent[h["url"]] = h
        path.write_text(json.dumps({
            "updated": now_s,
            "note": "hits = 최근 7일 검색 결과(first_seen 내림차순). 각 JOB은 first_seen이 자기 직전 실행(state/runs/<job>.json의 last_run) 이후인 것만 판정한다. 이 파일은 수정하지 않는다.",
            "new_this_run": len(new),
            "hits": sorted(recent.values(), key=lambda h: h["first_seen"], reverse=True)[:3000],
            "errors": errors.get(job, [])[:30],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[{job}] {len(results.get(job, []))} results, {len(new)} new, {len(errors.get(job, []))} errors",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
