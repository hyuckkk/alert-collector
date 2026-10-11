"""Turn reception-window snapshots into structured listings (snapshots/listings.json)
so the judgement step reads a small JSON instead of a noisy page dump.

Understands the Yuseong-gu and Seogu (서구 통합예약) list layouts:
  순번 / 부서 / 행사명 / 행사기간 / 접수기간 / 신청/정원 / 접수상태
Other sources are left as raw text for the judgement step.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "snapshots"
OUT = SNAP / "listings.json"
BASE = {"yuseong-anytime-event": "https://www.yuseong.go.kr/prog/anytmevt/kor/sub03_18_01/list.do",
        "yuseong-anytime-edu": "https://www.yuseong.go.kr/prog/anytmedc/kor/sub03_19_01/list.do",
        "yuseong-round-event": "https://www.yuseong.go.kr/prog/rndevent/kor/sub03_22_01/list.do",
        "yuseong-playground": "https://www.yuseong.go.kr/prog/plygrnd/kor/sub03_25_01/list.do"}
DATE_RANGE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})? ~ \d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")
CAP = re.compile(r"^\d+/\d+")


def parse_yuseong(text: str, source: str, url: str) -> list[dict]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = []
    i = 0
    while i < len(lines) - 6:
        if lines[i].isdigit() and DATE_RANGE.match(lines[i + 3]) and DATE_RANGE.match(lines[i + 4]) and CAP.match(lines[i + 5]):
            j = i + 6
            waiting = None
            if j < len(lines) and lines[j].startswith("(대기"):
                waiting = lines[j].strip("()")
                j += 1
            status = lines[j] if j < len(lines) else ""
            out.append({"source": source, "list_url": url, "seq": int(lines[i]), "dept": lines[i + 1],
                        "title": lines[i + 2], "event_period": lines[i + 3], "apply_period": lines[i + 4],
                        "capacity": lines[i + 5], "waiting": waiting, "status": status})
            i = j + 1
        else:
            i += 1
    return out


SEOGU = {"seogu-exprn-list": "https://www.seogu.go.kr/prog/exprnLnbns/yeyak/sub03_01/list.do",
         "seogu-event-list": "https://www.seogu.go.kr/prog/exprnLnbns/yeyak/sub02_01/list.do",
         "seogu-edu-list": "https://www.seogu.go.kr/prog/exprnLnbns/yeyak/sub01_02/list.do"}
D = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")
TILDE = re.compile(r"^~ ?\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")


def parse_seogu(text: str, source: str, url: str) -> list[dict]:
    """Seogu (대전 서구) 통합예약 list: 부서 / 사업명 / 선정방식 / 행사 시작 / ~ 끝 / 접수 시작 / ~ 끝 /
    신청/모집 / (대기 a/b) / 접수상태 — one value per line (the headless render joins cells with tabs,
    so tabs are treated as line breaks too)."""
    lines = [l.strip() for l in re.split(r"[\r\n\t]+", text) if l.strip()]
    out = []
    i = 0
    while i < len(lines) - 8:
        if (D.match(lines[i + 3]) and TILDE.match(lines[i + 4]) and D.match(lines[i + 5])
                and TILDE.match(lines[i + 6]) and CAP.match(lines[i + 7])):
            j = i + 8
            waiting = None
            if j < len(lines) and lines[j].startswith("(대기"):
                waiting = lines[j].strip("()")
                j += 1
            status = lines[j] if j < len(lines) else ""
            out.append({"source": source, "list_url": url, "dept": lines[i], "title": lines[i + 1],
                        "selection": lines[i + 2],
                        "event_period": f"{lines[i + 3]} {lines[i + 4]}",
                        "apply_period": f"{lines[i + 5]} {lines[i + 6]}",
                        "capacity": lines[i + 7], "waiting": waiting, "status": status})
            i = j + 1
        else:
            i += 1
    return out


def main() -> int:
    listings = []
    for sid, url in BASE.items():
        f = SNAP / f"{sid}.txt"
        if f.exists():
            listings.extend(parse_yuseong(f.read_text(encoding="utf-8"), sid, url))
    for sid, url in SEOGU.items():
        f = SNAP / f"{sid}.txt"
        if f.exists():
            listings.extend(parse_seogu(f.read_text(encoding="utf-8"), sid, url))
    OUT.write_text(json.dumps({"listings": listings}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(listings)} listings", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
