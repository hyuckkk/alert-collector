# Aside 받은편지함 (한국)

맥북의 Aside(실제 브라우저 에이전트)가 서버(GitHub 미국 서버)에서 막히는 사이트 — 네이버 검색, 티켓 사이트,
403·400·시간 초과로 비활성 처리한 공식 페이지 — 를 하루 1회 대신 읽어 온다.
무엇을 읽을지는 `aside/targets.json`, 결과는 아래 두 형식으로 클라우드 예약작업이 main 에 커밋한다.
telegram-relay 의 판정 JOB 은 `ops/dispatch.md` 규칙에 따라 이 결과를 다른 스냅샷·검색 결과와 같이 읽는다.

## 읽을 대상: `aside/targets.json`
```json
{"version": 1,
 "pages":    [{"id": "daejeon-familycenter-programs", "url": "https://…", "want": "무엇을 찾아 올지"}],
 "searches": [{"id": "naver-job1-01", "site": "search.naver.com", "query": "대전 어린이 뮤지컬", "want": "…", "job": "job1"}]}
```
`pages[].id` 는 `watch/urls.json` 의 같은 id(비활성 항목)와 맞춘다. `searches[].job` 은 그 검색어가 나온 JOB.

## 결과 1 — 페이지: `snapshots/aside/<id>.txt`
페이지마다 파일 하나를 **덮어쓴다**. 머리 4줄 다음 빈 줄, 그다음 화면에 보이는 본문 텍스트(최대 20,000자).
```
id: daejeon-familycenter-programs
url: https://daejeon.familynet.or.kr/center/lay1/program/S295T322C451/recruitReceipt/list.do
read_at: 2026-11-22T21:05:00+09:00
status: ok            # ok | blocked(로그인·캡차 등, 사유 한 줄) | error(사유 한 줄)

(페이지 본문 텍스트 — 목록이면 항목마다 한 줄: 제목 · 날짜 · 대상 · 상태 · 상세 링크)
```

## 결과 2 — 검색: `snapshots/discover/aside.json`
하루 한 번 통째로 **덮어쓴다**. `hits` 형식은 `snapshots/discover/<job>.json` 과 같고, 필드 몇 개를 더한다.
```json
{
  "updated": "2026-11-22T21:20:00+09:00",
  "source": "aside",
  "note": "Aside 실제 브라우저 네이버 검색 결과(하루 1회). 각 JOB 은 job 이 자기 것이고 first_seen 이 직전 실행 이후인 hit 만 판정한다.",
  "searches_done": 18, "searches_failed": 0, "errors": [],
  "hits": [
    {"title": "대전 크리스마스 가족뮤지컬 ○○ 티켓오픈", "url": "https://blog.naver.com/…", "official_url": "https://ticket.yes24.com/Perf/…",
     "snippet": "검색 결과에 보인 요약 한두 줄", "evidence": "글·공식 페이지 원문 한 줄(500자 이하)",
     "query": "대전 크리스마스 가족뮤지컬", "job": "job1", "engine": "aside_naver", "first_seen": "2026-11-22T21:12:00+09:00"}
  ]
}
```
- `hits` 는 최근 7일분만 남긴다(이전 파일의 `first_seen` 은 유지, 새 hit 만 새 시각).
- `official_url` 은 글에 공식 공고·예약 링크가 있을 때만. 없으면 생략.
- 개인 정보(가족 나이·주소 등)는 넣지 않는다. 공개 저장소다.
