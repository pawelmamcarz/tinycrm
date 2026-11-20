// Testy zachowania kolejki offline (queue.js) w Node: własność rekordów i wyścig „Wyślij” z trwającą wysyłką.
// Uruchamiane z pytest (apps/pwa/tests.py) przez `node --test`.
import { test } from "node:test";
import assert from "node:assert/strict";

function fakeIndexedDB() {
  const stores = new Map();
  return {
    open() {
      const req = {};
      setTimeout(() => {
        const db = {
          objectStoreNames: { contains: (n) => stores.has(n) },
          createObjectStore: (n, opts) => { stores.set(n, { keyPath: opts.keyPath, data: new Map() }); },
          transaction(name) {
            const s = stores.get(name);
            const t = {};
            let pending = 0;
            const op = (fn) => {
              const r = {};
              pending += 1;
              setTimeout(() => {
                r.result = fn();
                if (r.onsuccess) r.onsuccess();
                pending -= 1;
                if (!pending) setTimeout(() => t.oncomplete && t.oncomplete());
              });
              return r;
            };
            t.objectStore = () => ({
              put: (rec) => op(() => { s.data.set(rec[s.keyPath], structuredClone(rec)); return rec[s.keyPath]; }),
              getAll: () => op(() => [...s.data.values()].map((r) => structuredClone(r))),
              count: () => op(() => s.data.size),
              delete: (k) => op(() => { s.data.delete(k); }),
            });
            return t;
          },
        };
        req.result = db;
        if (!stores.size && req.onupgradeneeded) req.onupgradeneeded();
        if (req.onsuccess) req.onsuccess();
      });
      return req;
    },
  };
}

globalThis.self = globalThis;
globalThis.indexedDB = fakeIndexedDB();
globalThis.window = { dispatchEvent() {}, addEventListener() {} };
globalThis.document = { cookie: "csrftoken=abc", querySelector: () => null, addEventListener() {} };

const sent = [];
let gate = null;
globalThis.fetch = async (url, init) => {
  const uuid = init.body.get("client_uuid");
  if (gate) await gate;
  sent.push(uuid);
  return {
    status: 201, redirected: false,
    headers: { get: () => "application/json" },
    json: async () => ({ url: `/notatki/${uuid}/`, id: 1 }),
  };
};

const q = await import(new URL("../static/pwa/queue.js", import.meta.url));

test("rekordy innego konta nie są pokazywane ani wysyłane", async () => {
  q.setOwner("1:10");
  const mine = await q.buildRecord({ text: "notatka A", customerId: "" });
  await q.addRecord(mine);
  q.setOwner("2:20");
  assert.deepEqual(await q.listRecords(), []);
  assert.equal(await q.countForeignRecords(), 1);
  const summary = await q.flushQueue("/notatki/upload/");
  assert.deepEqual(summary.sent, []);
  assert.equal(sent.length, 0);
  await q.deleteRecord(mine.client_uuid); // cudzego nie da się usunąć
  q.setOwner("1:10");
  assert.equal((await q.listRecords()).length, 1);
  const own = await q.flushQueue("/notatki/upload/");
  assert.deepEqual(own.sent.map((s) => s.client_uuid), [mine.client_uuid]);
});

test("Wyślij w trakcie wysyłki: nowy rekord trafia do kolejnej rundy, nie do „serwer nie przyjął”", async () => {
  q.setOwner("1:10");
  sent.length = 0;
  let release;
  gate = new Promise((resolve) => { release = resolve; });
  const older = await q.buildRecord({ text: "starsza", customerId: "" });
  await q.addRecord(older);
  const first = q.flushQueue("/notatki/upload/");
  await new Promise((r) => setTimeout(r, 20)); // pierwsza runda już pobrała listę rekordów
  const fresh = await q.buildRecord({ text: "nowa", customerId: "" });
  await q.addRecord(fresh);
  const second = q.flushQueue("/notatki/upload/");
  release();
  gate = null;
  await first;
  const summary = await second;
  assert.ok(summary.sent.some((s) => s.client_uuid === fresh.client_uuid));
  assert.deepEqual(sent.sort(), [older.client_uuid, fresh.client_uuid].sort());
  assert.equal(summary.remaining, 0);
});
