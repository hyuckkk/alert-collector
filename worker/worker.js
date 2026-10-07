/**
 * telegram-relay — Cloudflare Worker (free plan).
 *
 * Since 2026-10-07 this Worker does everything that needs secrets or personal data:
 *   1. Telegram webhooks: instant button answers (✅ 확인 / 🚫 관심없음) and text commands.
 *   2. Every 5 minutes (cron): reads the PRIVATE state repo (env.GH_REPO), applies queued
 *      reactions, sends pending outbox/*.json alerts with the duplicate rules that used to live
 *      in scripts/send.py, and writes the records back — all through the GitHub REST API, so
 *      no GitHub Actions minutes are used.
 * Page collection and Korean search run separately in the public repo hyuckkk/alert-collector
 * (public pages only, no personal data, no secrets in code).
 *
 * Secrets / vars (Worker → Settings → Variables): TOKEN_SHOW TOKEN_SALE TOKEN_MILITARY
 * TOKEN_FIREWORKS TOKEN_DAM TOKEN_INVEST, CHAT_ID (owner), GH_TOKEN (PAT, contents RW on GH_REPO),
 * GH_REPO (private state repo, e.g. "hyuckkk/telegram-relay"), HOOK_SECRET.
 * Optional var SENDER_PAUSED="1" stops sending (buttons keep working).
 */

const CATS = ["show", "sale", "military", "fireworks", "dam", "invest"];
const BOT_LABEL = { show: "Show", sale: "Sale", military: "Military experience", fireworks: "Fireworks", dam: "Dam", invest: "Invest" };
const ORIGIN = "https://telegram-relay.jinhyuck77-b13.workers.dev";
const BUILD = "2026-10-07-sender-v3";
// Free plan: 50 subrequests per invocation. Every GitHub/Telegram call is counted; optional work stops
// early so that the claim commit (4) and the result commit (8 per attempt) always fit.
let SUB = 0;
const SUB_LIMIT = 48;

// ---- duplicate rules (ported 1:1 from scripts/send.py, incidents 9/30·10/1) ----
const IMPORTANT_CHANGES = new Set(["NEW", "BOOKING_OPEN", "APPLICATION_OPEN", "ADDITIONAL", "REOPENED", "RESCHEDULED",
  "CANCELLED", "AMOUNT_CHANGED", "EXTENDED", "DISCHARGE_CHANGED", "LOGISTICS"]);
const ONCE_PER_EVENT = new Set(["APPLICATION_OPEN", "BOOKING_OPEN", "LOGISTICS"]);
const URGENT_CHANGES = new Set(["CANCELLED", "RESCHEDULED", "BOOKING_OPEN", "REOPENED", "DISCHARGE_CHANGED", "EXTENDED"]);
const MIN_GAP_HOURS = 24;

function token(env, cat) { return env["TOKEN_" + cat.toUpperCase()]; }
function isOwner(env, id) { return String(id) === String(env.CHAT_ID); }
function nowIso() { return new Date(Date.now() + 9 * 3600 * 1000).toISOString().replace(/\.\d+Z$/, "+09:00"); }

async function tg(tok, method, payload) {
  SUB++;
  const r = await fetch(`https://api.telegram.org/bot${tok}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload || {}),
  });
  let d; try { d = await r.json(); } catch { d = { ok: false, description: `HTTP ${r.status}` }; }
  return d;
}

// ---------------------------------------------------------------- GitHub storage
function b64encode(str) {
  const bytes = new TextEncoder().encode(str); let bin = "";
  for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(bin);
}
function b64decode(b64) {
  const bin = atob(b64.replace(/\s/g, "")); const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}
function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (v && typeof v === "object") { const o = {}; for (const k of Object.keys(v).sort()) o[k] = sortKeys(v[k]); return o; }
  return v;
}
function dumpJson(v) { return JSON.stringify(sortKeys(v), null, 2) + "\n"; } // same layout as Python save_json

class Repo {
  constructor(env) { this.env = env; this.base = `https://api.github.com/repos/${env.GH_REPO}`; }
  async api(path, method = "GET", body) {
    SUB++;
    const r = await fetch(this.base + path, {
      method, body: body ? JSON.stringify(body) : undefined,
      headers: { authorization: `Bearer ${this.env.GH_TOKEN}`, accept: "application/vnd.github+json",
        "content-type": "application/json", "user-agent": "telegram-relay-worker" },
    });
    return r;
  }
  async head() {
    const r = await this.api("/git/ref/heads/main");
    if (!r.ok) throw new Error(`ref ${r.status}`);
    return (await r.json()).object.sha;
  }
  async list(dir, ref) {
    const r = await this.api(`/contents/${dir}?ref=${ref}`);
    if (r.status === 404) return [];
    if (!r.ok) throw new Error(`list ${dir} ${r.status}`);
    const rows = await r.json();
    return Array.isArray(rows) ? rows.filter((x) => x.type === "file" && x.name.endsWith(".json")) : [];
  }
  async read(path, ref, fallback) {
    const r = await this.api(`/contents/${path}?ref=${ref}`);
    if (r.status === 404) return fallback;
    if (!r.ok) throw new Error(`read ${path} ${r.status}`);
    const d = await r.json();
    if (d.content && d.encoding === "base64" && d.size < 900000) return JSON.parse(b64decode(d.content));
    // large file: fetch raw blob
    const b = await this.api(`/git/blobs/${d.sha}`);
    const bd = await b.json();
    return JSON.parse(b64decode(bd.content));
  }
  /** One commit with several file changes; fails (returns false) if main moved meanwhile. */
  async commit(parent, message, files) {
    const pc = await (await this.api(`/git/commits/${parent}`)).json();
    const tree = Object.entries(files).map(([path, content]) => content === null
      ? { path, mode: "100644", type: "blob", sha: null }
      : { path, mode: "100644", type: "blob", content });
    const t = await this.api("/git/trees", "POST", { base_tree: pc.tree.sha, tree });
    if (!t.ok) throw new Error(`tree ${t.status} ${(await t.text()).slice(0, 120)}`);
    const c = await this.api("/git/commits", "POST", { message, tree: (await t.json()).sha, parents: [parent],
      author: { name: "telegram-relay[bot]", email: "relay@users.noreply.github.com", date: new Date().toISOString() } });
    if (!c.ok) throw new Error(`commit ${c.status}`);
    const sha = (await c.json()).sha;
    const u = await this.api("/git/refs/heads/main", "PATCH", { sha, force: false });
    if (u.status === 422 || u.status === 409) return false;
    if (!u.ok) throw new Error(`ref update ${u.status}`);
    return sha;
  }
}

// ---------------------------------------------------------------- message helpers
async function contentHash(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text.trim()));
  return Array.from(new Uint8Array(d), (x) => x.toString(16).padStart(2, "0")).join("").slice(0, 16);
}
function escapeHtml(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function renderHtml(text) {
  return text.split("\n").map((line) => {
    const e = escapeHtml(line); const st = line.trimStart();
    return (st.startsWith("🚨") || st.startsWith("📌")) ? `<b>${e}</b>` : e;
  }).join("\n");
}
function buttonsMarkup(nid, items) {
  const rows = [];
  for (let i = 1; i <= items; i++) {
    const l = items > 1 ? ` ${i}` : "";
    rows.push([{ text: `✅ 확인${l}`, callback_data: `ack:${nid}:${i}` }, { text: `🚫 관심없음${l}`, callback_data: `mute:${nid}:${i}` }]);
  }
  return { inline_keyboard: rows };
}
function label(action, itemNo, multi) {
  const s = multi ? ` ${itemNo}` : "";
  return { ack: [`✅ 확인${s}`, "ack"], unack: [`✅ 확인됨${s}`, "unack"], mute: [`🚫 관심없음${s}`, "mute"],
    unmute: [`🚫 관심없음 처리됨${s}`, "unmute"] }[action];
}
function rebuildRow(nid, itemNo, newState, multi) {
  const mk = (a) => { const [text, d] = label(a, itemNo, multi); return { text, callback_data: `${d}:${nid}:${itemNo}` }; };
  if (newState === "ACKNOWLEDGED") return [mk("unack")];
  if (newState === "MUTED") return [mk("unmute")];
  return [mk("ack"), mk("mute")];
}

function decide(ev, item, textHash) {
  const change = item.change_type || "NEW";
  if (!ev) return [true, "new event"];
  if (ev.user_state === "MUTED") return [false, "event is MUTED"];
  if (ev.last_content_hash === textHash) return [false, "identical content already notified"];
  if (change === "NEW" && ev.last_notified) return [false, "already notified as NEW earlier"];
  const sent = (ev.history || []).filter((h) => h.sent).map((h) => h.change_type);
  if (ONCE_PER_EVENT.has(change) && sent.includes(change)) return [false, `'${change}' already sent for this event`];
  if (!URGENT_CHANGES.has(change) && ev.last_notified) {
    const t = Date.parse(ev.last_notified);
    const gap = isNaN(t) ? 999 : (Date.now() - t) / 3600000;
    if (gap < MIN_GAP_HOURS) return [false, `same event notified ${gap.toFixed(1)}h ago`];
  }
  if (ev.user_state === "ACKNOWLEDGED") {
    if (IMPORTANT_CHANGES.has(change) && change !== "NEW") return [true, `acknowledged but important: ${change}`];
    return [false, `acknowledged; '${change}' not important`];
  }
  return [true, `change: ${change}`];
}
function recipientWants(ev, item, chatId) {
  if (!ev) return true;
  const st = (ev.recipient_states || {})[String(chatId)] || "NONE";
  const change = item.change_type || "NEW";
  if (st === "MUTED") return false;
  if (st === "ACKNOWLEDGED") return IMPORTANT_CHANGES.has(change) && change !== "NEW";
  return true;
}

// ---------------------------------------------------------------- reactions (ported from poll.py)
const ACK_WORDS = new Set(["확인", "ok", "OK", "ㅇㅇ", "✅"]);
const MUTE_WORDS = new Set(["ㄴㄴ", "관심없음", "무시", "🚫"]);

function findNotif(notifs, { nid, messageId, category } = {}) {
  if (nid) return notifs.find((n) => n.notification_id === nid) || null;
  if (messageId != null) return notifs.find((n) => n.message_id === messageId && n.category === category) || null;
  for (let i = notifs.length - 1; i >= 0; i--) { const n = notifs[i]; if (n.category === category && n.ok && !n.retracted) return n; }
  return null;
}
function applyState(events, notif, itemNo, newState, via) {
  const ids = notif.event_ids || [];
  if (newState == null) return "이미 처리된 항목입니다.";
  if (itemNo < 1 || itemNo > ids.length) return `항목 번호 ${itemNo}는 이 메시지에 없습니다 (1~${ids.length}).`;
  const eid = ids[itemNo - 1]; const ev = events[eid];
  if (!ev) return `이벤트 기록을 찾지 못했습니다: ${eid}`;
  const prev = ev.user_state || "NONE";
  ev.user_state = newState; ev.user_state_changed_at = nowIso();
  (ev.history = ev.history || []).push({ at: nowIso(), notification_id: notif.notification_id, item_no: itemNo,
    user_state: newState, previous: prev, via });
  const title = ev.title || eid;
  if (newState === "ACKNOWLEDGED") return `✅ 확인 처리: ${title}\n같은 내용은 다시 알리지 않고, 접수 시작·취소·일정 변경 같은 중요한 변화만 알립니다.`;
  if (newState === "MUTED") return `🚫 관심없음 처리: ${title}\n이 행사의 후속 알림을 모두 차단합니다. 되돌리려면 /unmute`;
  return `↩️ 차단 해제: ${title}`;
}
function statesFor(events, notif) {
  const o = {}; (notif.event_ids || []).forEach((eid, i) => { o[String(i + 1)] = (events[eid] || {}).user_state || "NONE"; }); return o;
}
function summary(events, cat, state) {
  const rows = Object.keys(events).sort().filter((k) => events[k].category === cat && events[k].user_state === state)
    .map((k) => `• ${events[k].title || k} (${events[k].date || "날짜 미확인"})`);
  return `${state === "MUTED" ? "관심없음" : "확인"} 처리된 항목 ${rows.length}건\n` + (rows.length ? rows.join("\n") : "(없음)");
}

/** Applies one reaction record. Returns {reply?: {cat,text,reply_to}, files?: {path: content}}. */
function applyReaction(env, rec, ctx) {
  const { events, notifs, recipients, joins } = ctx;
  const cat = rec.category;
  if (!CATS.includes(cat)) return {};
  if (rec.kind === "join") {
    if (!joins.some((j) => j.from_id === rec.from_id && j.category === cat)) {
      joins.push({ category: cat, from_id: rec.from_id, username: rec.username, name: rec.name, at: rec.at });
      ctx.joinsChanged = true;
    }
    return {};
  }
  if (!isOwner(env, rec.from_id)) {
    const extra = (recipients[cat] || []).some((r) => String(r.chat_id) === String(rec.from_id));
    if (extra && rec.kind === "callback") {
      const n = findNotif(notifs, { nid: rec.notification_id });
      if (n) {
        const ids = n.event_ids || []; const i = parseInt(rec.item_no || 1, 10);
        if (i >= 1 && i <= ids.length && events[ids[i - 1]]) {
          const ev = events[ids[i - 1]];
          (ev.recipient_states = ev.recipient_states || {})[String(rec.from_id)] = rec.user_state || "NONE";
          (ev.history = ev.history || []).push({ at: nowIso(), recipient: rec.from_id, user_state: rec.user_state, via: "button" });
        }
      }
    }
    return {};
  }
  if (rec.kind === "callback") {
    const n = findNotif(notifs, { nid: rec.notification_id });
    if (n) { applyState(events, n, parseInt(rec.item_no || 1, 10), rec.user_state, "button"); n.markup_synced = statesFor(events, n); }
    return {};
  }
  if (rec.kind !== "message") return {};
  const text = (rec.text || "").trim();
  const reply = (t) => ({ reply: { cat, text: t, reply_to: rec.message_id } });
  if (text.startsWith("/status")) {
    const c = Object.values(events).filter((e) => e.category === cat);
    const sent = notifs.filter((n) => n.category === cat && n.ok).length;
    return reply(`${BOT_LABEL[cat]} 봇 상태\n이벤트 ${c.length}건 / 발송 ${sent}건\n확인 ${c.filter((e) => e.user_state === "ACKNOWLEDGED").length}건, 관심없음 ${c.filter((e) => e.user_state === "MUTED").length}건`);
  }
  if (text.startsWith("/muted")) return reply(summary(events, cat, "MUTED"));
  if (text.startsWith("/acknowledged")) return reply(summary(events, cat, "ACKNOWLEDGED"));
  const m = text.match(/^(\/unmute|\S+)\s*(\d+)?$/);
  if (!m) {
    if (text.length >= 40) { // pasted ChatGPT alert -> comparison inbox for JOB11
      const stamp = nowIso().replace(/[:+]/g, "").replace("+", "_");
      return { files: { [`state/gpt_inbox/${stamp}_${cat}_${rec.message_id}.json`]: dumpJson({ at: nowIso(), category: cat, text, source: "telegram" }) },
        reply: { cat, text: "ChatGPT 알림 비교함에 넣었습니다. 대조 작업이 이 시스템의 발송 기록과 비교해 결과를 지원금 봇으로 보고합니다.", reply_to: rec.message_id } };
    }
    return {};
  }
  const word = m[1]; const num = parseInt(m[2] || "1", 10);
  let newState;
  if (word === "/unmute") newState = "NONE"; else if (ACK_WORDS.has(word)) newState = "ACKNOWLEDGED"; else if (MUTE_WORDS.has(word)) newState = "MUTED"; else return {};
  const n = rec.reply_to_message_id ? findNotif(notifs, { messageId: rec.reply_to_message_id, category: cat }) : findNotif(notifs, { category: cat });
  if (!n) return reply("처리할 알림을 찾지 못했습니다. 알림 메시지에 답장으로 보내주세요.");
  const msg = applyState(events, n, num, newState, "text");
  ctx.redraw.push(n);
  return reply(msg);
}

// ---------------------------------------------------------------- the 5-minute sender run
async function run(env) {
  SUB = 0;
  const repo = new Repo(env);
  const status = { build: BUILD, at: nowIso() };
  let head = await repo.head();
  const [outbox, reactions] = await Promise.all([repo.list("outbox", head), repo.list("state/reactions", head)]);
  const paused = env.SENDER_PAUSED === "1";
  if (!reactions.length && (paused || !outbox.length)) return { ...status, idle: true, paused };

  // ---- phase 1: read state, apply reactions, decide, claim (one commit) ----
  const [events, notifs, recipients, counter, sending, joins] = await Promise.all([
    repo.read("state/events.json", head, {}), repo.read("state/notifications.json", head, []),
    repo.read("state/recipients.json", head, {}), repo.read("state/counter.json", head, { seq: 0 }),
    repo.read("state/sending.json", head, {}), repo.read("state/join_requests.json", head, [])]);
  const ctx = { events, notifs, recipients, joins, joinsChanged: false, redraw: [] };
  const files = {}; const replies = [];
  // budget: 9 reads so far; reserve 4 (claim) + 16 (two result-commit attempts) + 2 per outbox send
  for (const f of reactions.slice(0, 5)) {
    if (SUB > 18) break;
    try {
      const rec = await repo.read(f.path, head, null);
      if (rec) { const out = applyReaction(env, rec, ctx); if (out.files) Object.assign(files, out.files); if (out.reply) replies.push(out.reply); }
    } catch (e) { status.reaction_error = String(e).slice(0, 120); }
    files[f.path] = null;
  }

  const sentIds = new Set(notifs.map((n) => n.outbox_id));
  const plan = [];
  let skips = null;
  for (const f of (paused ? [] : outbox.slice(0, 4))) {
    if (SUB + 1 + 2 * (plan.length + 1) + replies.length + ctx.redraw.length + 20 > SUB_LIMIT) break;
    const oid = f.name.replace(/\.json$/, "");
    let data;
    try { data = await repo.read(f.path, head, null); } catch { continue; } // read failed: keep file for next run
    files[f.path] = null;
    if (!data || typeof data !== "object") continue;
    if (sentIds.has(oid)) continue;                        // restart protection
    if (sending[oid] && !sending[oid].done) {               // claimed earlier, no record: never resend
      if (!skips) skips = await repo.read("state/claim_skips.json", head, []);
      skips.push({ outbox_id: oid, claimed_at: sending[oid].at || sending[oid], skipped_at: nowIso(), reported: false, data });
      files["state/claim_skips.json"] = dumpJson(skips.slice(-200));
      continue;
    }
    const category = data.category; const text = (data.text || "").trim(); const items = data.items || [];
    if (!CATS.includes(category) || !text || !items.length) continue;
    const textHash = await contentHash(text);
    const kept = [], dropped = [];
    items.forEach((it, i) => {
      if (!it.event_id) { dropped.push([i + 1, "missing event_id"]); return; }
      const [ok, why] = decide(events[it.event_id], it, textHash);
      (ok ? kept : dropped).push([i + 1, why]);
    });
    if (!kept.length) { (status.suppressed = status.suppressed || []).push({ oid, why: dropped.map((d) => d[1]) }); continue; }
    counter.seq = (parseInt(counter.seq || 0, 10)) + 1;
    const nid = `n${String(counter.seq).padStart(5, "0")}`;
    sending[oid] = { at: nowIso(), nid, done: false };
    plan.push({ oid, nid, data, category, text, items, textHash, kept: kept.map((k) => k[0]), dropped: dropped.map((d) => d[0]) });
  }
  files["state/events.json"] = dumpJson(events);
  files["state/notifications.json"] = dumpJson(notifs);
  files["state/counter.json"] = dumpJson(counter);
  files["state/sending.json"] = dumpJson(sending);
  if (ctx.joinsChanged) files["state/join_requests.json"] = dumpJson(joins);
  const claimed = await repo.commit(head, `relay: claim ${plan.length} / reactions ${reactions.length} [skip ci]`, files);
  if (!claimed) return { ...status, retry: "main moved before claim; next run" };
  head = claimed;

  // ---- phase 2: Telegram (only after the claim is durable) ----
  for (const r of replies) {
    if (SUB + 2 * plan.length + 8 >= SUB_LIMIT) break;
    const tok = token(env, r.cat);
    if (tok) await tg(tok, "sendMessage", { chat_id: env.CHAT_ID, text: r.text, reply_to_message_id: r.reply_to });
  }
  for (const n of ctx.redraw) {
    if (SUB + 2 * plan.length + 8 >= SUB_LIMIT) break;
    if (!n.message_id || n.message_id === -1) continue;
    const st = statesFor(events, n); const rows = [];
    for (let i = 1; i <= (n.item_count || 1); i++) rows.push(rebuildRow(n.notification_id, i, st[String(i)] || "NONE", (n.item_count || 1) > 1));
    await tg(token(env, n.category), "editMessageReplyMarkup", { chat_id: n.chat_id, message_id: n.message_id, reply_markup: { inline_keyboard: rows } });
  }
  const results = [];
  for (const p of plan) {
    const tok = token(env, p.category);
    const payload = { text: renderHtml(p.text), parse_mode: "HTML", reply_markup: buttonsMarkup(p.nid, p.items.length), disable_web_page_preview: true };
    const rec = { notification_id: p.nid, outbox_id: p.oid, category: p.category, bot: BOT_LABEL[p.category], chat_id: Number(env.CHAT_ID),
      event_ids: p.items.map((i) => i.event_id), item_count: p.items.length, dropped_items: p.dropped, content_hash: p.textHash,
      origin_id: p.data.origin_id || null, created_at: nowIso(), sent_at: null, message_id: null, ok: false, error: null, retracted: false, via: "worker" };
    const r = tok ? await tg(tok, "sendMessage", { chat_id: env.CHAT_ID, ...payload }) : { ok: false, description: "no token" };
    if (r.ok) {
      rec.ok = true; rec.sent_at = nowIso(); rec.message_id = r.result.message_id;
      const extras = {};
      for (const x of (p.data.owner_only ? [] : (recipients[p.category] || []))) {
        if (!x.chat_id || SUB + 8 >= SUB_LIMIT) continue;
        if (!p.items.some((it) => recipientWants(events[it.event_id], it, x.chat_id))) { extras[String(x.chat_id)] = { ok: false, skipped: "recipient state" }; continue; }
        const rr = await tg(tok, "sendMessage", { chat_id: x.chat_id, ...payload });
        extras[String(x.chat_id)] = rr.ok ? { ok: true, message_id: rr.result.message_id, sent_at: nowIso() } : { ok: false, error: String(rr.description).slice(0, 200) };
      }
      if (Object.keys(extras).length) rec.extra_deliveries = extras;
    } else {
      rec.error = String(r.description).slice(0, 300);
    }
    results.push({ p, rec });
  }
  if (!results.length) return { ...status, reactions: reactions.length, replies: replies.length };

  // ---- phase 3: record results (re-read fresh state; retry if main moves) ----
  for (let attempt = 0; attempt < 2; attempt++) {
    head = await repo.head();
    const [ev2, nf2, sd2] = await Promise.all([repo.read("state/events.json", head, {}), repo.read("state/notifications.json", head, []),
      repo.read("state/sending.json", head, {})]);
    const failedOutbox = {};
    for (const { p, rec } of results) {
      nf2.push(rec);
      if (!rec.ok) { delete sd2[p.oid]; failedOutbox[`outbox/${p.oid}.json`] = dumpJson(p.data); continue; } // definite failure: allow retry
      sd2[p.oid] = { ...(sd2[p.oid] || {}), done: true, sent_at: rec.sent_at };
      const ts = nowIso(); const keptSet = new Set(p.kept);
      p.items.forEach((it, idx0) => {
        const idx = idx0 + 1; const eid = it.event_id; if (!eid) return;
        const ev = ev2[eid] = ev2[eid] || { event_id: eid, category: p.category, first_seen: ts, user_state: "NONE", status: "NEW", history: [] };
        for (const k of ["title", "date", "location", "source_url", "raw_data"]) if (it[k] != null) ev[k] = it[k];
        ev.last_seen = ts; ev.status = it.change_type || ev.status || "NEW";
        if (keptSet.has(idx)) { ev.last_notified = ts; ev.last_content_hash = p.textHash; ev.last_notification_id = p.nid; }
        (ev.history = ev.history || []).push({ at: ts, notification_id: p.nid, item_no: idx, change_type: it.change_type || "NEW", sent: keptSet.has(idx) });
      });
    }
    // keep sending.json small: drop entries older than 3 days
    const cut = Date.now() - 3 * 86400000;
    for (const [k, v] of Object.entries(sd2)) { const t = Date.parse((v && v.at) || v); if (!isNaN(t) && t < cut) delete sd2[k]; }
    const ok = await repo.commit(head, `relay send ${results.filter((r) => r.rec.ok).length} ok / ${results.filter((r) => !r.rec.ok).length} failed [skip ci]`, {
      "state/events.json": dumpJson(ev2), "state/notifications.json": dumpJson(nf2), "state/sending.json": dumpJson(sd2), ...failedOutbox });
    if (ok) return { ...status, sent: results.filter((r) => r.rec.ok).map((r) => r.p.oid), failed: results.filter((r) => !r.rec.ok).map((r) => r.p.oid) };
  }
  return { ...status, error: "sent but could not record results; claim left in sending.json (no resend)" };
}

// ---------------------------------------------------------------- webhook handlers (instant)
async function saveReaction(env, rec) {
  const path = `state/reactions/${rec.at.replace(/[:+]/g, "-")}_${rec.update_id}.json`;
  const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/contents/${path}`, {
    method: "PUT",
    headers: { authorization: `Bearer ${env.GH_TOKEN}`, accept: "application/vnd.github+json", "user-agent": "telegram-relay-worker", "content-type": "application/json" },
    body: JSON.stringify({ message: `reaction: ${rec.kind} [skip ci]`, content: b64encode(JSON.stringify(rec, null, 2)) }),
  });
  return r.ok ? true : { ok: false, status: r.status };
}
async function isExtraFor(env, cat, id) {
  try {
    const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/contents/state/recipients.json`, {
      headers: { authorization: `Bearer ${env.GH_TOKEN}`, accept: "application/vnd.github.raw+json", "user-agent": "telegram-relay-worker" } });
    if (!r.ok) return false;
    const rec = await r.json();
    return (rec[cat] || []).some((x) => String(x.chat_id) === String(id));
  } catch { return false; }
}
async function handleCallback(env, cat, cq) {
  const tok = token(env, cat); const from = cq.from && cq.from.id;
  if (!isOwner(env, from) && !(await isExtraFor(env, cat, from))) { await tg(tok, "answerCallbackQuery", { callback_query_id: cq.id, text: "권한이 없습니다." }); return; }
  const parts = (cq.data || "").split(":");
  if (parts.length !== 3) { await tg(tok, "answerCallbackQuery", { callback_query_id: cq.id }); return; }
  let [action, nid, itemStr] = parts; const itemNo = parseInt(itemStr, 10);
  if (action === "noop") {
    const btn = (((cq.message || {}).reply_markup || {}).inline_keyboard || []).flat().find((b) => b.callback_data === cq.data);
    action = ((btn && btn.text) || "").includes("관심없음") ? "unmute" : "unack";
  }
  const newState = { ack: "ACKNOWLEDGED", mute: "MUTED", unack: "NONE", unmute: "NONE" }[action];
  if (!newState) { await tg(tok, "answerCallbackQuery", { callback_query_id: cq.id }); return; }
  const tip = { ack: "✅ 확인 처리. 같은 내용은 다시 알리지 않고 중요한 변화만 알립니다.", mute: "🚫 관심없음 처리. 이 행사의 후속 알림을 모두 차단합니다.", unack: "↩️ 확인 취소", unmute: "↩️ 관심없음 취소" }[action];
  await tg(tok, "answerCallbackQuery", { callback_query_id: cq.id, text: tip });
  const msg = cq.message || {};
  const kb = ((msg.reply_markup || {}).inline_keyboard || []).map((r) => r.slice());
  const multi = kb.length > 1;
  let rowIdx = kb.findIndex((row) => row.some((b) => (b.callback_data || "").endsWith(`:${nid}:${itemNo}`)));
  if (rowIdx < 0) rowIdx = Math.min(itemNo - 1, kb.length - 1);
  if (rowIdx >= 0) kb[rowIdx] = rebuildRow(nid, itemNo, newState, multi);
  if (msg.message_id) await tg(tok, "editMessageReplyMarkup", { chat_id: msg.chat.id, message_id: msg.message_id, reply_markup: { inline_keyboard: kb } });
  const ok = await saveReaction(env, { kind: "callback", category: cat, action, user_state: newState, notification_id: nid, item_no: itemNo,
    message_id: msg.message_id || null, update_id: cq._update_id, from_id: from, at: nowIso() });
  if (ok !== true) await tg(tok, "sendMessage", { chat_id: msg.chat.id, text: `⚠️ 버튼은 눌렸지만 저장에 실패했습니다 (GitHub ${ok.status}). 잠시 후 한 번 더 눌러주세요.` });
}
async function handleMessage(env, cat, m) {
  const tok = token(env, cat); const from = m.from && m.from.id; const chat = m.chat && m.chat.id;
  const text = (m.text || "").trim(); if (!text) return;
  if (!isOwner(env, from)) {
    if (await isExtraFor(env, cat, from)) {
      if (text.startsWith("/start") || text.startsWith("/help")) await tg(tok, "sendMessage", { chat_id: chat, text: "이 봇의 알림을 받고 있습니다. 알림의 [✅ 확인] [🚫 관심없음] 버튼을 누르면 본인 기준으로 반영됩니다(다시 누르면 취소)." });
      return;
    }
    await saveReaction(env, { kind: "join", category: cat, from_id: from, username: (m.from && m.from.username) || null,
      name: [(m.from && m.from.first_name) || "", (m.from && m.from.last_name) || ""].join(" ").trim(), update_id: m._update_id, at: nowIso() });
    await tg(tok, "sendMessage", { chat_id: chat, text: "이 봇은 소유자가 승인한 사람에게만 알림을 보냅니다. 요청이 접수되었으니 소유자 승인을 기다려 주세요." });
    return;
  }
  if (String(chat) !== String(env.CHAT_ID)) return;
  if (text.startsWith("/start") || text.startsWith("/help")) {
    await tg(tok, "sendMessage", { chat_id: chat, text: "버튼 [✅ 확인] [🚫 관심없음]을 누르면 즉시 반영됩니다. 다시 누르면 취소됩니다.\n텍스트 명령: 확인 / ㄴㄴ / 확인 2 / ㄴㄴ 2 (알림에 답장하면 그 메시지 기준), /unmute [n], /status, /muted, /acknowledged\n텍스트 명령은 5분 안에 처리되고 답장이 옵니다." });
    return;
  }
  const ok = await saveReaction(env, { kind: "message", category: cat, text, reply_to_message_id: m.reply_to_message ? m.reply_to_message.message_id : null,
    message_id: m.message_id, update_id: m._update_id, from_id: from, at: nowIso() });
  if (ok !== true) await tg(tok, "sendMessage", { chat_id: chat, reply_to_message_id: m.message_id, text: `⚠️ 저장에 실패했습니다 (GitHub ${ok.status}). 잠시 후 다시 보내주세요.` });
}

async function ensureWebhooks(env) {
  if (!env.HOOK_SECRET) return;
  for (const cat of CATS) {
    const tok = token(env, cat); if (!tok) continue;
    try {
      const want = `${ORIGIN}/hook/${cat}`;
      const info = await tg(tok, "getWebhookInfo");
      if (info.ok && info.result && info.result.url === want) continue;
      await tg(tok, "setWebhook", { url: want, secret_token: env.HOOK_SECRET, allowed_updates: ["message", "callback_query"], drop_pending_updates: false });
    } catch (e) { console.log("ensureWebhooks", cat); }
  }
}

// GitHub's own schedule for the public collector repo proved unreliable (nothing ran 16:00-21:00 on 10/7),
// so this Worker starts collection itself by committing a tick file; the workflows run on push of that path.
const COLLECTOR = "hyuckkk/alert-collector";
async function tickCollector(env, which) {
  const url = `https://api.github.com/repos/${COLLECTOR}/contents/ticks/${which}`;
  const h = { authorization: `Bearer ${env.GH_TOKEN}`, accept: "application/vnd.github+json", "user-agent": "telegram-relay-worker", "content-type": "application/json" };
  const cur = await fetch(url, { headers: h });
  const sha = cur.ok ? (await cur.json()).sha : undefined;
  const r = await fetch(url, { method: "PUT", headers: h, body: JSON.stringify({ message: `tick ${which}`, content: btoa(nowIso() + "\n"), sha }) });
  return { which, status: r.status };
}
let lastTick = null;
let lastStatus = null;
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      if (event.cron === "2 * * * *") { SUB = 0; await ensureWebhooks(env); return; } // hourly, own invocation
      if (event.cron === "17,47 * * * *") { lastTick = await tickCollector(env, "fetch").catch((e) => ({ error: String(e) })); return; }
      if (event.cron === "7 * * * *") { lastTick = await tickCollector(env, "discover").catch((e) => ({ error: String(e) })); return; }
      try { lastStatus = await run(env); } catch (e) { lastStatus = { build: BUILD, at: nowIso(), error: String(e && e.message).slice(0, 200) }; }
    })());
  },
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/health") return new Response(JSON.stringify({ ok: true, build: BUILD, last: lastStatus, tick: lastTick }), { headers: { "content-type": "application/json" } });
    if (url.pathname === "/run" && url.searchParams.get("key") === env.HOOK_SECRET) {
      const r = await run(env).catch((e) => ({ error: String(e && e.message) })); lastStatus = r;
      return new Response(JSON.stringify(r, null, 2), { headers: { "content-type": "application/json" } });
    }
    const m = url.pathname.match(/^\/hook\/(show|sale|military|fireworks|dam|invest)$/);
    if (m && request.method === "POST") {
      if (request.headers.get("x-telegram-bot-api-secret-token") !== env.HOOK_SECRET) return new Response("forbidden", { status: 403 });
      let upd; try { upd = await request.json(); } catch { return new Response("bad json", { status: 400 }); }
      try {
        if (upd.callback_query) { upd.callback_query._update_id = upd.update_id; await handleCallback(env, m[1], upd.callback_query); }
        else if (upd.message) { upd.message._update_id = upd.update_id; await handleMessage(env, m[1], upd.message); }
      } catch (e) { console.log("handler error"); }
      return new Response("ok");
    }
    return new Response("not found", { status: 404 });
  },
};
