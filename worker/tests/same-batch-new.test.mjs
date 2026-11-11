import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// Exercise the real sender planner and result logic against in-memory doubles.
const worker = process.argv.includes("--stdin-worker")
  ? readFileSync(0, "utf8")
  : readFileSync(new URL("../worker.js", import.meta.url), "utf8");
const source = worker
  .replace("class Repo {", "class RealRepo {")
  .replace("async function tg(", "async function realTg(")
  .replace("async function contentHash(", "async function realContentHash(")
  .replace("export default {", "const unusedWorker = {");
assert.notEqual(source, worker);

const eid = "sale:test:canonical-event";
function harness({ secondEvent = eid, secondChange = "NEW", existing = {} } = {}) {
  const db = {
    "state/events.json": existing,
    "state/notifications.json": [],
    "outbox/original.json": {
      category: "sale", text: "first producer's wording",
      items: [{ event_id: eid, change_type: "NEW" }],
    },
    "relay-v2/queue/reviewed.json": {
      id: "reviewed", event_id: secondEvent, change: secondChange, category: "sale",
      text: "second producer's different wording", recipient: "owner",
    },
  };
  const calls = [];
  class FakeRepo {
    async head() { return "fake-head"; }
    async list(dir) {
      return Object.keys(db).filter(p => p.startsWith(dir + "/")).map(path => ({
        path, name: path.slice(dir.length + 1),
      }));
    }
    async read(path, _head, fallback) { return structuredClone(db[path] ?? fallback); }
    async commit(_head, _message, files) {
      for (const [path, value] of Object.entries(files)) {
        if (value == null) delete db[path];
        else db[path] = JSON.parse(value);
      }
      return "fake-head";
    }
  }
  const fakeSend = async (_token, method, payload) => {
    assert.equal(method, "sendMessage");
    calls.push(payload.text);
    return { ok: true, result: { message_id: calls.length } };
  };
  const run = new Function("Repo", "tg", "contentHash", source + "\nreturn run;")(
    FakeRepo, fakeSend, async text => text,
  );
  return { run: () => run({ CHAT_ID: "1", TOKEN_SALE: "fake-value" }), db, calls };
}

test("same canonical NEW from outbox and GPT in one batch sends once", async () => {
  const h = harness();
  await h.run();
  assert.equal(h.calls.length, 1);
  assert.equal(h.db["state/notifications.json"].length, 1);
  assert.deepEqual(h.db["state/suppressed.json"][0].why, ["duplicate NEW already planned in this batch"]);
  assert.equal(h.db["state/suppressed.json"][0].outbox_id, "gpt-reviewed");
  await h.run();
  assert.equal(h.calls.length, 1);
});

test("different event IDs are not combined", async () => {
  const h = harness({ secondEvent: eid + "-other" });
  await h.run();
  assert.equal(h.calls.length, 2);
});

test("booking opening is not suppressed by a same-batch NEW", async () => {
  const h = harness({ secondChange: "BOOKING_OPEN" });
  await h.run();
  assert.equal(h.calls.length, 2);
});

test("existing MUTED state still wins", async () => {
  const h = harness({ existing: { [eid]: { user_state: "MUTED" } } });
  await h.run();
  assert.equal(h.calls.length, 0);
});
