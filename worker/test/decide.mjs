// 11-28 한국 알림 중복 점검 재현: 이미 알린 행사에 사소한 변경 표시가 붙어 다시 나가던 것
import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os"; import { join } from "node:path";
const dir = mkdtempSync(join(tmpdir(), "dec-")); const f = join(dir, "w.mjs");
writeFileSync(f, readFileSync(new URL("../worker.js", import.meta.url), "utf8"));
const { decide } = await import(f);
const fail = (m, x) => { console.error("FAIL", m, x); process.exit(1); };
const day = (n) => new Date(Date.now() + n * 86400000 + 9 * 3600000).toISOString().slice(0, 10);
const ev = (o = {}) => ({ user_state: "NONE", last_notified: new Date(Date.now() - 3 * 86400000).toISOString(), last_content_hash: "a", history: [{ at: new Date(Date.now() - 3 * 86400000).toISOString(), sent: true, change_type: "NEW" }], date: day(20), ...o });
const ok = (e, it) => decide(e, it, "b")[0];
if (ok(ev(), { change_type: "LOGISTICS" })) fail("먼 행사 LOGISTICS 는 다시 안 감(키즈 서커스·멧돼지 사례)");
if (!ok(ev({ date: day(1) }), { change_type: "LOGISTICS" })) fail("행사 직전 셔틀·운영 정보는 감");
if (ok(ev(), { change_type: "UPDATED" })) fail("UPDATED 는 다시 안 감(철원 두루미)");
if (!ok(ev(), { change_type: "BOOKING_OPEN" })) fail("예매 시작은 감");
if (!ok(ev(), { change_type: "CANCELLED" })) fail("취소는 감");
const add = ev({ history: [{ at: new Date(Date.now() - 2 * 86400000).toISOString(), sent: true, change_type: "ADDITIONAL" }] });
if (ok(add, { change_type: "ADDITIONAL" })) fail("ADDITIONAL 7일에 한 번(김장 수산물 3번)");
if (!ok(undefined, { change_type: "NEW" })) fail("새 행사는 감");
console.log("ok decide");
