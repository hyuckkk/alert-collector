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
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
import queue
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
    r"\.kr/(bbs|board|notice|event|program|cop/bbs)|instagram\.com/p/|"
    r"cafe\.daum\.net/[^/?#]+/[^/?#]+/\d+|brunch\.co\.kr/@[^/]+/\d+|blog\.daum\.net/[^/?#]+/\d+)")
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


def build_tasks(queries, now):
    # Interleave topics so a slow/large first topic cannot starve later topics.
    groups = []
    for job, qs in queries.items():
        if job.startswith('_') or not qs:
            continue
        indices = list(range(len(qs)))
        # Rotate priority between runs, keeping the original Naver slot membership.
        offset = (int(now.timestamp()) // 10800) % len(indices)
        indices = indices[offset:] + indices[:offset]
        groups.append([(job, qs[i], eng, url) for i in indices
                       for eng, url in engines(qs[i])
                       if not eng.startswith('naver') or i % 3 == now.hour % 3])
    return [group[i] for i in range(max(map(len, groups), default=0))
            for group in groups if i < len(group)]


def search_lane(tasks, output, workers, naver):
    """Child process: a blocked socket can be terminated without losing parent data."""
    blocked = False

    def execute(task):
        nonlocal blocked
        job, query, engine, url = task
        if naver and blocked:
            return job, [], 'skipped', 'Naver circuit open after 403/429'
        try:
            return job, search(engine, url, query), 'ok', None
        except Exception as error:
            message = f"{engine} {query}: {error}"[:200]
            if naver and any(word in str(error) for word in ('403', 'Forbidden', '429')):
                blocked = True
            return job, [], 'failed', message
        finally:
            time.sleep(1.5 if naver else 0.8)

    # One dedicated Naver worker; its wait never occupies Daum/Google workers.
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(execute, task) for task in tasks]
        for future in as_completed(futures):
            output.put(('result', future.result()))
    output.put(('done', None))


def save_results(queries, previous, results, errors, counts, planned, started, finished):
    cutoff = (started - timedelta(days=7)).isoformat()
    for job in planned:
        prev = previous[job]
        # Preserve the old timestamp when no fresh search succeeded.
        recent = {h['url']: h for h in prev.get('hits', prev.get('recent', []))
                  if h.get('first_seen', '') >= cutoff}
        known = {h['url'] for h in prev.get('hits', prev.get('recent', []))}
        new = 0
        for hit in results[job]:
            if hit['url'] in known or hit['url'] in recent:
                continue
            recent[hit['url']] = dict(hit, first_seen=started.isoformat(timespec='seconds'))
            new += 1
        stats = counts[job]
        pending = planned[job] - sum(stats.values())
        complete = pending == 0 and stats['failed'] == 0 and stats['skipped'] == 0
        payload = {
            'updated': datetime.now(KST).isoformat(timespec='seconds') if stats['ok'] else prev.get('updated'),
            'attempted_at': started.isoformat(timespec='seconds'),
            'status': 'complete' if complete else ('partial' if stats['ok'] else 'failed'),
            'run_finished': finished,
            'coverage': dict(stats, scheduled=planned[job], pending=pending),
            'note': 'Discovery candidates only. Partial/failed coverage is not no change. first_seen is not the event date.',
            'new_this_run': new,
            'hits': sorted(recent.values(), key=lambda h: h['first_seen'], reverse=True)[:3000],
            'errors': ([f'Incomplete search coverage: {pending} pending, '
                        f"{stats['failed']} failed, {stats['skipped']} skipped; not no change."]
                       if not complete else []) + errors[job][:29],
        }
        path = OUT / f'{job}.json'
        temp = path.with_suffix('.json.tmp')
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
        temp.replace(path)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    queries = json.loads(QUERIES.read_text(encoding='utf-8'))
    now = datetime.now(KST)
    tasks = build_tasks(queries, now)
    planned = {job: 0 for job in queries if not job.startswith('_')}
    for job, *_ in tasks:
        planned[job] += 1
    previous = {}
    for job in planned:
        path = OUT / f'{job}.json'
        # Fail before any writes if the existing snapshot is malformed.
        previous[job] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    results = {job: [] for job in planned}
    errors = {job: [] for job in planned}
    counts = {job: dict(ok=0, failed=0, skipped=0) for job in planned}
    budget = min(1500, max(0.1, float(os.environ.get('DISCOVER_BUDGET_SECONDS', '1440'))))
    workers = min(16, max(1, int(os.environ.get('DISCOVER_WORKERS', '8'))))
    context = multiprocessing.get_context('spawn')
    output = context.Queue()
    lanes = []
    for naver in (True, False):
        subset = [t for t in tasks if t[2].startswith('naver') == naver]
        if subset:
            process = context.Process(target=search_lane, args=(subset, output, 1 if naver else workers, naver))
            process.start()
            lanes.append(process)
    deadline = time.monotonic() + budget
    checkpoint = time.monotonic() + 30
    done = 0
    try:
        while done < len(lanes) and time.monotonic() < deadline:
            try:
                kind, value = output.get(timeout=min(0.5, max(0.01, deadline-time.monotonic())))
                if kind == 'done':
                    done += 1
                else:
                    job, hits, status, error = value
                    counts[job][status] += 1
                    results[job].extend(hits)
                    if error and len(errors[job]) < 30:
                        errors[job].append(error)
            except queue.Empty:
                if all(not p.is_alive() for p in lanes):
                    break
            if time.monotonic() >= checkpoint:
                save_results(queries, previous, results, errors, counts, planned, now, False)
                print('Discovery checkpoint saved', flush=True)
                checkpoint = time.monotonic() + 30
    finally:
        for process in lanes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        output.close()
    save_results(queries, previous, results, errors, counts, planned, now, True)
    for job in planned:
        print(f'[{job}] {counts[job]} / scheduled={planned[job]}', flush=True)
    # A partial result must reach the existing commit step, not be lost to timeout.
    return 0


if __name__ == '__main__':
    sys.exit(main())


