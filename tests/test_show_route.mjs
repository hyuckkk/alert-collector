// Exercise unmodified production functions in a VM. Any network attempt fails.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../worker/worker.js', import.meta.url), 'utf8');
const context = vm.createContext({ Date, console, fetch() { throw Error('Network forbidden'); } });
vm.runInContext(source.replace('export default {', 'const worker = {') + '\nthis.api = {gptToOutbox, decide, token, recipientWants};', context);
const {gptToOutbox, decide, token, recipientWants} = context.api;
let checks = 0;
function check(value) { assert.ok(value); checks++; }
const now = Date.now();
for (const topic of ['artist', 'university', 'family']) {
    const queue = {id:'offline-only', event_id:'show:offline:'+topic, topic, category:'show', recipient:'owner', text:'Offline fixture', expires_at:now+10000, change:'NEW'};
    const out = gptToOutbox(queue);
    check(out.category === 'show' && out.owner_only);
    check(token({TOKEN_SHOW:'FAKE_SHOW_ONLY'}, out.category) === 'FAKE_SHOW_ONLY');
    check(decide(null, out.items[0], 'fresh')[0]);
    check(!gptToOutbox({...queue, expires_at:now-1000}));
}
const item = {event_id:'show:offline', change_type:'NEW'};
check(!decide({user_state:'MUTED'}, item, 'new')[0]);
check(!decide({user_state:'ACKNOWLEDGED'}, item, 'new')[0]);
check(!decide({last_content_hash:'same'}, item, 'same')[0]);
check(!decide({last_notified:'2025-01-01T00:00:00Z'}, item, 'different-source')[0]);
for (const change_type of ['CANCELLED','RESCHEDULED','BOOKING_OPEN','ADDITIONAL']) {
    const changed = {...item, change_type};
    check(decide({user_state:'ACKNOWLEDGED'}, changed, 'material-change')[0]);
    check(!decide({user_state:'MUTED'}, changed, 'material-change')[0]);
}
// Historical official examples: expiration remains tied to their actual end date.
for (const [title, end, url] of [
    ['2025 ILLIT GLITTER DAY ENCORE','2025-11-09T23:59:59+09:00','https://www.ksponco.or.kr/olympicpark/eventInfo/eventInfoView.es?evtSeq=5022&mid=a20301010200'],
    ['YENA MBC Virtual Live Festival','2025-10-19T23:59:59+09:00','https://news.coupang.com/archives/56473/']
]) {
    const queue = {category:'show', event_id:title, text:title, expires_at:Date.parse(end), sources:[{url}]};
    check(gptToOutbox(queue) === null);
}
console.log(`${checks} production Show routing/deduplication/expiration checks passed; no network or queue writes.`);
