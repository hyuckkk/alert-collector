"""Fetch watched pages from GitHub Actions (Korean government sites often block
overseas cloud fetchers; the Actions runner is a second vantage point) and store
their visible text under snapshots/<id>.txt plus a small index with hashes, so the
judgement step can diff them via git without fetching itself.

watch/urls.json: [{"id": "yuseong-event-reception", "url": "https://...", "note": "..."}]
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCH = ROOT / "watch" / "urls.json"
SNAP = ROOT / "snapshots"
INDEX = SNAP / "_index.json"
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


class TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)
        if tag in {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "dt", "dd", "td", "th"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        t = html.unescape("".join(self.parts))
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r"\n\s*\n+", "\n", t)
        return t.strip()


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
        try:
            body = raw.decode(enc, "replace")
        except LookupError:
            body = raw.decode("utf-8", "replace")
        return r.status, body


_HOST_LOCKS: dict[str, "threading.Lock"] = {}
_HOST_LOCKS_GUARD = None


def _host_lock(url: str):
    """같은 호스트(구청·시청 등)에는 동시에 1개 요청만 보낸다(차단·지연 방지)."""
    import threading
    from urllib.parse import urlsplit
    global _HOST_LOCKS_GUARD
    if _HOST_LOCKS_GUARD is None:
        _HOST_LOCKS_GUARD = threading.Lock()
    host = urlsplit(url).netloc.lower()
    with _HOST_LOCKS_GUARD:
        return _HOST_LOCKS.setdefault(host, threading.Lock())


def fetch_one(wid: str, url: str, entry: dict, now: str) -> dict:
    """Fetch one page; returns the updated index entry (never raises)."""
    with _host_lock(url):
        return _fetch_one(wid, url, entry, now)


def _fetch_one(wid: str, url: str, entry: dict, now: str) -> dict:
    # 직전 실행들에서 계속 실패한 페이지(last_error_at이 last_ok보다 최신)는 재시도 없이
    # 1회만 시도해 시간 예산을 정상 페이지에 남긴다. fail_streak가 누적된다.
    streak = int(entry.get("fail_streak") or 0)
    attempts = 1 if streak >= 2 else 3
    try:
        last = None
        for attempt in range(attempts):
            try:
                status, body = fetch(url); break
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt + 1 < attempts:
                    time.sleep(5 * (attempt + 1))
        else:
            raise last
        p = TextExtractor()
        p.feed(body)
        text = p.text()
        if len(text) < 80:
            raise RuntimeError(f"too little text ({len(text)} chars), status {status}")
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        changed = entry.get("hash") != h
        (SNAP / f"{wid}.txt").write_text(text, encoding="utf-8")
        (SNAP / f"{wid}.links.json").write_text(json.dumps(p.links[:400], ensure_ascii=False, indent=0), encoding="utf-8")
        entry.update(url=url, last_ok=now, hash=h, changed_at=(now if changed else entry.get("changed_at")),
                     status=status, error=None, chars=len(text), fail_streak=0)
        time.sleep(1.5)  # 같은 호스트 연속 요청 간격
        print(f"[{wid}] ok {len(text)} chars {'CHANGED' if changed else 'same'}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        entry.update(url=url, last_error_at=now, error=str(e)[:300], fail_streak=streak + 1)
        print(f"[{wid}] ERROR {e}", file=sys.stderr)
    return entry


def main() -> int:
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    SNAP.mkdir(exist_ok=True)
    items = json.loads(WATCH.read_text(encoding="utf-8")) if WATCH.exists() else []
    index = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    now = datetime.now(KST).isoformat(timespec="seconds")
    # 정상 페이지(최근 성공)를 먼저, 계속 실패 중인 페이지를 뒤에 두고 병렬로 받는다.
    # 전체 시간 예산을 넘기면 남은 것은 건너뛰어 커밋 단계가 timeout으로 사라지지 않게 한다.
    # healthy pages first, and among them the ones read longest ago (pages skipped for budget last run come first)
    items = sorted(items, key=lambda it: (int(index.get(it["id"], {}).get("fail_streak") or 0) >= 2,
                                          index.get(it["id"], {}).get("last_ok") or ""))
    budget_s = float(os.environ.get("FETCH_BUDGET_S", "600"))
    workers = int(os.environ.get("FETCH_WORKERS", "5"))
    t0 = time.monotonic()
    skipped: list[str] = []
    ex = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    for it in items:
        wid, url = it["id"], it["url"]
        entry = index.setdefault(wid, {"url": url})
        futures[ex.submit(fetch_one, wid, url, entry, now)] = wid
    pending = set(futures)
    while pending:
        remaining = budget_s - (time.monotonic() - t0)
        if remaining <= 0:
            break
        done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
    for f in pending:
        if not f.running() and f.cancel():
            skipped.append(futures[f])
    # 이미 실행 중인 것은 최대 socket timeout만큼 기다린다(결과가 index에 반영되도록).
    still = [f for f in pending if not f.cancelled()]
    if still:
        wait(still, timeout=60)
        for f in still:
            if not f.done():
                skipped.append(futures[f])
    ex.shutdown(wait=False, cancel_futures=True)
    index["_last_run"] = now
    index["_skipped_for_budget"] = skipped
    if skipped:
        print(f"budget exhausted, skipped {len(skipped)}: {skipped}", file=sys.stderr)
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    # A server that drips bytes slowly keeps a worker thread alive past its socket timeout, and the
    # interpreter would wait for it at exit — hanging the workflow until its timeout and losing the
    # commit. The index is written, so leave immediately.
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
