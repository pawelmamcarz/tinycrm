// Testy service workera (sw.js) w Node: cache stron offline i obsługa kliknięcia w powiadomienie.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../templates/pwa/sw.js", import.meta.url), "utf8");
const ORIGIN = "https://app.test";
const CFG = {
  version: "t", offlineUrl: "/teren/offline/", loginUrl: "/logowanie/", logoutUrl: "/wyloguj/",
  staticUrl: "/static/", precache: [], warmPages: ["/teren/", "/teren/nagraj/"], recordPath: "/teren/nagraj/",
  pagePrefixes: ["/teren/"], cdnHosts: [], defaultIcon: "/i.png", defaultUrl: "/teren/",
};
const PAGES = "tinycrm-pages-t";

function page(body, extra = {}) {
  return { ok: true, redirected: false, type: "basic", url: ORIGIN + "/", body, clone() { return this; }, ...extra };
}

function setup({ network }) {
  const caches = new Map();
  const cacheApi = {
    open: async (name) => {
      if (!caches.has(name)) caches.set(name, new Map());
      const m = caches.get(name);
      return { put: async (k, v) => { m.set(String(k), v); }, match: async (k) => m.get(String(k)) };
    },
    delete: async (name) => caches.delete(name),
    match: async (k) => { for (const m of caches.values()) if (m.has(String(k))) return m.get(String(k)); return undefined; },
    keys: async () => [...caches.keys()],
  };
  const listeners = {};
  const fetched = [];
  const clientsApi = { opened: [], list: [], matchAll: async () => clientsApi.list,
    openWindow: async (u) => { clientsApi.opened.push(u); }, claim: async () => {} };
  const self = {
    TINYCRM_SW: CFG, location: { origin: ORIGIN },
    addEventListener: (t, fn) => { listeners[t] = fn; },
    clients: clientsApi, registration: {}, skipWaiting: () => {},
  };
  const ctx = vm.createContext({
    self, caches: cacheApi, URL, Promise, Response: class { constructor(b, i) { this.body = b; Object.assign(this, i); } },
    Request: class { constructor(url, init) { this.url = url; Object.assign(this, init); } toString() { return this.url; } },
    fetch: async (req) => {
      const url = typeof req === "string" ? req : req.url;
      fetched.push(url);
      return network(new URL(url, ORIGIN));
    },
    console,
  });
  vm.runInContext(SOURCE, ctx);
  async function navigate(path) {
    const waits = [];
    let responded;
    listeners.fetch({
      request: { method: "GET", mode: "navigate", url: ORIGIN + path },
      respondWith: (p) => { responded = p; },
      waitUntil: (p) => { waits.push(p); },
    });
    const resp = await responded;
    await Promise.all(waits);
    await new Promise((r) => setTimeout(r, 0));
    return resp;
  }
  return { caches, cacheApi, navigate, fetched, listeners, clientsApi };
}

test("przekierowanie (np. logo → /teren/) nie wyrzuca ekranu nagrywania z cache", async () => {
  const sw = setup({ network: () => ({ ok: false, type: "opaqueredirect", redirected: false, url: ORIGIN + "/" }) });
  (await sw.cacheApi.open(PAGES)).put("/teren/nagraj/", page("nagraj"));
  await sw.navigate("/");
  assert.equal((await sw.cacheApi.match("/teren/nagraj/")).body, "nagraj");
});

test("logowanie czyści cache, a pierwsze zalogowane wejście dogrzewa /teren/nagraj/", async () => {
  const sw = setup({ network: (url) => page(url.pathname === "/logowanie/" ? "login" : "strona " + url.pathname) });
  (await sw.cacheApi.open(PAGES)).put("/teren/nagraj/", page("stara"));
  await sw.navigate("/logowanie/");
  assert.equal(sw.caches.has(PAGES), false);
  await sw.navigate("/teren/");
  assert.equal((await sw.cacheApi.match("/teren/")).body, "strona /teren/");
  assert.equal((await sw.cacheApi.match("/teren/nagraj/")).body, "strona /teren/nagraj/");
});

test("strona z ?klient= nie nadpisuje ogólnego ekranu nagrywania w cache", async () => {
  let online = true;
  const sw = setup({ network: (url) => {
    if (!online) throw new Error("offline");
    return page("nagraj" + url.search);
  } });
  (await sw.cacheApi.open(PAGES)).put("/teren/nagraj/", page("nagraj"));
  (await sw.cacheApi.open(PAGES)).put("/teren/", page("teren"));
  await sw.navigate("/teren/nagraj/?klient=5");
  assert.equal((await sw.cacheApi.match("/teren/nagraj/")).body, "nagraj");
  online = false;
  const offline = await sw.navigate("/teren/nagraj/?klient=7");
  assert.equal(offline.body, "nagraj");
});

test("kliknięcie powiadomienia nie przechodzi z ekranu nagrywania", async () => {
  const sw = setup({ network: () => page("x") });
  const navigated = [];
  const recording = { url: ORIGIN + "/teren/nagraj/", focus: async () => {}, navigate: async (u) => { navigated.push(["rec", u]); } };
  sw.clientsApi.list = [recording];
  let waits = [];
  const click = (url) => sw.listeners.notificationclick({
    notification: { close() {}, data: { url } }, waitUntil: (p) => waits.push(p) });
  click("/notatki/1/");
  await Promise.all(waits);
  assert.deepEqual(navigated, []);
  assert.deepEqual(sw.clientsApi.opened, [ORIGIN + "/notatki/1/"]);

  const other = { url: ORIGIN + "/teren/", focus: async () => {}, navigate: async (u) => { navigated.push(["home", u]); return null; } };
  sw.clientsApi.list = [recording, other];
  waits = [];
  click("/notatki/2/");
  await Promise.all(waits);
  assert.deepEqual(navigated, [["home", ORIGIN + "/notatki/2/"]]);
});
