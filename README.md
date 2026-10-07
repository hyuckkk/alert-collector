# alert-collector

공개 페이지 수집과 한국어 검색 결과만 모아 두는 저장소입니다. 개인 정보·발송 기록·비밀값은 여기에 두지 않습니다.

- `watch/urls.json`: 감시 페이지 목록 → `snapshots/*.txt`, `snapshots/_index.json`, `snapshots/listings.json`
- `watch/queries.json`: 작업별 검색어 → `snapshots/discover/<job>.json`
- `worker/worker.js`: Cloudflare Worker 코드(비밀값은 Cloudflare에만 저장). push 시 `deploy-worker` 실행으로 배포.

수집: 매시 17·47분(fetch), 7분(discover).
