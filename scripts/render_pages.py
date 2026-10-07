"""Second pass after fetch_pages.py: pages whose content is drawn by JavaScript
(plain fetch returns an empty shell: "too little text", or only the site menu) are
opened in headless Chromium and their rendered text is stored the same way.

Which pages: watch entries with "render": true, plus any entry whose last plain
fetch ended with "too little text". Runs with a time budget; failures are recorded
in the index but never break the workflow.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCH = ROOT / "watch" / "urls.json"
SNAP = ROOT / "snapshots"
INDEX = SNAP / "_index.json"
KST = timezone(timedelta(hours=9))


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed; skipping render pass", file=sys.stderr)
        if INDEX.exists():
            idx = json.loads(INDEX.read_text(encoding="utf-8"))
            idx["_render_error"] = {"at": datetime.now(KST).isoformat(timespec="seconds"), "errors": ["playwright not installed"]}
            INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return 0
    items = json.loads(WATCH.read_text(encoding="utf-8"))
    index = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    todo = [it for it in items
            if it.get("render") or "too little text" in (index.get(it["id"], {}).get("error") or "")]
    if not todo:
        return 0
    now = datetime.now(KST).isoformat(timespec="seconds")
    budget = float(os.environ.get("RENDER_BUDGET_S", "360"))
    t0 = time.monotonic()
    import subprocess
    ok = fail = 0
    launch_errors = []
    for it in todo:
        if time.monotonic() - t0 > budget:
            break
        wid, url = it["id"], it["url"]
        entry = index.setdefault(wid, {"url": url})
        # one short-lived process per page: a page that pops a JS dialog or crashes the browser
        # driver takes only itself down, not the whole pass
        try:
            r = subprocess.run([sys.executable, __file__, "--one", url], capture_output=True, text=True, timeout=50)
            res = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {"error": (r.stderr or "no output")[-300:]}
        except Exception as e:  # noqa: BLE001
            res = {"error": f"{type(e).__name__}: {e}"[:300]}
        if res.get("launch_error"):
            launch_errors.append(res["launch_error"])
        if res.get("text"):
            text = res["text"]
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            changed = entry.get("hash") != h
            (SNAP / f"{wid}.txt").write_text(text, encoding="utf-8")
            (SNAP / f"{wid}.links.json").write_text(json.dumps(res.get("links") or [], ensure_ascii=False, indent=0), encoding="utf-8")
            entry.update(url=url, last_ok=now, hash=h, changed_at=(now if changed else entry.get("changed_at")),
                         error=None, chars=len(text), fail_streak=0, rendered=True, render_error=None)
            ok += 1
            print(f"[{wid}] rendered {len(text)} chars {'CHANGED' if changed else 'same'}", file=sys.stderr)
        else:
            entry.update(render_error=str(res.get("error"))[:300], render_error_at=now)
            fail += 1
            print(f"[{wid}] RENDER ERROR {res.get('error')}", file=sys.stderr)
        INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    index["_render_error"] = ({"at": now, "errors": launch_errors[:3]} if launch_errors and not ok else None)
    index["_render_last"] = {"at": now, "ok": ok, "failed": fail}
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)


def render_one(url: str) -> dict:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, errs = None, []
        for opts in ({"channel": "chrome"}, {"executable_path": "/usr/bin/google-chrome"}, {}):
            try:
                browser = p.chromium.launch(**opts)
                break
            except Exception as e:  # noqa: BLE001
                errs.append(f"{opts}: {str(e)[:150]}")
        if browser is None:
            return {"error": "launch failed", "launch_error": errs}
        ctx = browser.new_context(locale="ko-KR", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"))
        page = ctx.new_page()
        page.on("dialog", lambda d: d.dismiss())
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1500)
        text = page.evaluate("document.body ? document.body.innerText : ''") or ""
        links = page.evaluate("[...document.querySelectorAll('a[href]')].map(a=>a.href).slice(0,400)")
        text = "\n".join(l.strip() for l in text.splitlines() if l.strip())
        if len(text) < 80:
            return {"error": f"rendered but too little text ({len(text)} chars)"}
        return {"text": text, "links": links}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--one":
        try:
            out = render_one(sys.argv[2])
        except Exception as e:  # noqa: BLE001
            out = {"error": f"{type(e).__name__}: {e}"[:300]}
        print(json.dumps(out, ensure_ascii=False), flush=True)
        os._exit(0)
    sys.exit(main())
