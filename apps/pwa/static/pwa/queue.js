/* TinyCRM Teren — kolejka offline notatek (IndexedDB) i wysyłka do voice:upload.
 * Rekord: {client_uuid, owner, audio: ArrayBuffer|null, mime, text, text_source, customer_id, recorded_at,
 *          created_at, state: "pending"|"failed", attempts, last_error}.
 * `owner` = "<organizacja>:<użytkownik>" z konfiguracji strony. Rekordy innego konta (np. telefon przekazany
 * innej osobie) nie są pokazywane ani wysyłane — czekają, aż zaloguje się ich właściciel.
 * Najpierw zapis do kolejki, potem próba wysyłki — nagranie nie ginie przy braku zasięgu.
 * Serwer jest idempotentny po client_uuid, więc ponowna wysyłka tego samego rekordu jest bezpieczna. */

const DB_NAME = "tinycrm-teren";
const DB_VERSION = 1;
const STORE = "voice-queue";
export const QUEUE_EVENT = "tinycrm:queue-changed";

const EXT_BY_MIME = { "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3",
  "audio/aac": "aac", "audio/wav": "wav", "audio/x-m4a": "m4a" };

let dbPromise = null;
let flushing = null;
let flushAgain = null;
let currentOwner = "";

/** Ustaw właściciela bieżącej sesji (z `pwa-config`). Bez właściciela kolejka niczego nie pokazuje ani nie wysyła. */
export function setOwner(owner) {
  currentOwner = owner ? String(owner) : "";
}

function isMine(row) {
  return !!currentOwner && !!row && row.owner === currentOwner;
}

export function newUuid() {
  if (self.crypto && typeof self.crypto.randomUUID === "function") return self.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  (self.crypto || window.msCrypto).getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function queueSupported() {
  return typeof indexedDB !== "undefined";
}

function openDb() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    if (!queueSupported()) { reject(new Error("no-indexeddb")); return; }
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: "client_uuid" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("idb-open"));
    req.onblocked = () => reject(new Error("idb-blocked"));
  });
  dbPromise.catch(() => { dbPromise = null; });
  return dbPromise;
}

function tx(mode, fn) {
  return openDb().then((db) => new Promise((resolve, reject) => {
    const t = db.transaction(STORE, mode);
    const store = t.objectStore(STORE);
    let result;
    const req = fn(store);
    if (req) req.onsuccess = () => { result = req.result; };
    t.oncomplete = () => resolve(result);
    t.onerror = () => reject(t.error || new Error("idb-tx"));
    t.onabort = () => reject(t.error || new Error("idb-abort"));
  }));
}

function changed() {
  countRecords().then((count) => {
    window.dispatchEvent(new CustomEvent(QUEUE_EVENT, { detail: { count } }));
  }).catch(() => {});
}

export function addRecord(record) {
  return tx("readwrite", (store) => store.put(record)).then(() => { changed(); return record; });
}

function listAllRecords() {
  return tx("readonly", (store) => store.getAll()).then((rows) => (rows || [])
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at))));
}

/** Rekordy bieżącego właściciela (patrz `setOwner`). */
export function listRecords() {
  return listAllRecords().then((rows) => rows.filter(isMine));
}

export function countRecords() {
  return listRecords().then((rows) => rows.length);
}

/** Liczba rekordów innych kont na tym urządzeniu (bez ich treści). */
export function countForeignRecords() {
  return listAllRecords().then((rows) => rows.filter((row) => !isMine(row)).length);
}

export function deleteRecord(clientUuid) {
  // Tylko własne rekordy — cudzych nagrań na tym telefonie nie da się usunąć z bieżącej sesji.
  return listRecords().then((rows) => {
    if (!rows.some((r) => r.client_uuid === clientUuid)) return null;
    return tx("readwrite", (store) => store.delete(clientUuid)).then(() => changed());
  });
}

export function retryRecord(clientUuid) {
  return listRecords().then((rows) => {
    const row = rows.find((r) => r.client_uuid === clientUuid);
    if (!row) return null;
    row.state = "pending";
    row.last_error = "";
    return addRecord(row);
  });
}

/** Buduje rekord kolejki z nagrania (Blob) i tekstu. Audio zapisujemy jako ArrayBuffer (stabilne w Safari). */
export async function buildRecord({ blob, text, textSource, customerId, recordedAt }) {
  let audio = null;
  let mime = "";
  if (blob && blob.size) {
    audio = await blob.arrayBuffer();
    mime = blob.type || "audio/webm";
  }
  return {
    client_uuid: newUuid(),
    owner: currentOwner,
    audio,
    mime,
    text: (text || "").trim(),
    text_source: textSource || "",
    customer_id: customerId ? String(customerId) : "",
    recorded_at: recordedAt || new Date().toISOString(),
    created_at: new Date().toISOString(),
    state: "pending",
    attempts: 0,
    last_error: "",
  };
}

export function getCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  if (match) return decodeURIComponent(match[1]);
  const meta = document.querySelector("meta[name='csrf-token']");
  return meta ? meta.content : "";
}

function fileNameFor(mime) {
  const base = String(mime || "").split(";")[0].trim().toLowerCase();
  return `notatka.${EXT_BY_MIME[base] || "webm"}`;
}

class UploadError extends Error {
  constructor(message, { retryable = true, auth = false } = {}) {
    super(message);
    this.retryable = retryable;
    this.auth = auth;
  }
}

/** Wysyła jeden rekord. Zwraca {url} albo rzuca UploadError. */
export async function sendRecord(record, uploadUrl) {
  const form = new FormData();
  if (record.audio && record.audio.byteLength) {
    const blob = new Blob([record.audio], { type: record.mime || "audio/webm" });
    form.append("audio", blob, fileNameFor(record.mime));
  }
  form.append("typed_text", record.text || "");
  form.append("text_source", record.text_source || "");
  form.append("customer_id", record.customer_id || "");
  form.append("client_uuid", record.client_uuid);
  form.append("recorded_at", record.recorded_at || "");
  let resp;
  try {
    resp = await fetch(uploadUrl, {
      method: "POST",
      body: form,
      credentials: "same-origin",
      headers: { "X-CSRFToken": getCsrfToken(), "Accept": "application/json" },
    });
  } catch (err) {
    throw new UploadError("network", { retryable: true });
  }
  const isJson = (resp.headers.get("Content-Type") || "").includes("application/json");
  if (resp.redirected || resp.status === 401 || resp.status === 403) {
    // Sesja wygasła albo brak tokenu CSRF — rekord zostaje w kolejce do ponownego zalogowania.
    throw new UploadError("Zaloguj się ponownie, aby wysłać notatki z kolejki.", { retryable: true, auth: true });
  }
  let data = {};
  if (isJson) { try { data = await resp.json(); } catch (e) { data = {}; } }
  if ((resp.status === 200 || resp.status === 201) && isJson) {
    return { url: data.url || "", id: data.id };
  }
  if (resp.status >= 500 || resp.status === 408 || resp.status === 429 || !isJson) {
    throw new UploadError(`Serwer chwilowo niedostępny (HTTP ${resp.status}).`, { retryable: true });
  }
  throw new UploadError(data.error || `Serwer odrzucił notatkę (HTTP ${resp.status}).`, { retryable: false });
}

/**
 * Wysyła wszystkie oczekujące rekordy po kolei. Rekordy odrzucone przez serwer (4xx) dostają stan "failed"
 * i czekają na decyzję użytkownika (ponów / usuń) — nigdy nie kasujemy nagrania bez potwierdzenia serwera.
 * Zwraca {sent: [{client_uuid, url}], failed, remaining, offline, auth}.
 */
export function flushQueue(uploadUrl, { onlyUuid = null } = {}) {
  if (flushing) {
    // Trwa wysyłka z wcześniejszą listą rekordów — nowy rekord (np. właśnie dodany „Wyślij”) mógł się w niej
    // nie znaleźć. Dołącz jedną kolejną rundę po bieżącej, zamiast zwracać nieaktualny wynik.
    if (!flushAgain) {
      flushAgain = flushing.catch(() => null).then(() => {
        flushAgain = null;
        return flushQueue(uploadUrl, { onlyUuid });
      });
    }
    return flushAgain;
  }
  flushing = (async () => {
    const summary = { sent: [], failed: 0, remaining: 0, offline: false, auth: false };
    let rows = [];
    try { rows = await listRecords(); } catch (e) { return summary; }
    for (const row of rows) {
      if (onlyUuid && row.client_uuid !== onlyUuid) continue;
      if (row.state === "failed") continue;
      if (summary.offline || summary.auth) break;
      try {
        const result = await sendRecord(row, uploadUrl);
        await deleteRecord(row.client_uuid);
        summary.sent.push({ client_uuid: row.client_uuid, url: result.url });
      } catch (err) {
        row.attempts = (row.attempts || 0) + 1;
        if (err && err.retryable === false) {
          row.state = "failed";
          row.last_error = err.message;
        } else if (err && err.auth) {
          summary.auth = true;
          row.last_error = err.message;
        } else {
          summary.offline = true;
          row.last_error = err && err.message !== "network" ? err.message : "";
        }
        try { await addRecord(row); } catch (e) { /* zostaw jak było */ }
      }
    }
    try {
      const left = await listRecords();
      summary.remaining = left.length;
      summary.failed = left.filter((r) => r.state === "failed").length;
    } catch (e) { /* ignoruj */ }
    return summary;
  })().finally(() => { flushing = null; });
  return flushing;
}

/** Automatyczna wysyłka: po powrocie zasięgu i po powrocie do aplikacji. */
export function autoFlush(uploadUrl, onResult) {
  const run = () => flushQueue(uploadUrl).then((r) => { if (onResult) onResult(r); return r; });
  window.addEventListener("online", run);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && navigator.onLine !== false) run();
  });
  return run;
}
