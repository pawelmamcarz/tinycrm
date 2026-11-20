/* TinyCRM Teren — ekran „Nagraj notatkę”.
 * Nagranie: MediaRecorder (webm/opus na Androidzie, mp4 na iOS), maks. kilka minut, licznik czasu.
 * Równolegle (jeśli przeglądarka umie) dyktowanie na żywo pl-PL do pola tekstowego — pole działa też samo.
 * „Wyślij”: najpierw zapis do kolejki IndexedDB, potem próba wysyłki; brak zasięgu = zostaje w kolejce. */
import { addRecord, autoFlush, buildRecord, flushQueue, queueSupported, sendRecord, setOwner } from "./queue.js";
import { registerServiceWorker } from "./sw-register.js";

const cfgEl = document.getElementById("pwa-config");
const CFG = cfgEl ? JSON.parse(cfgEl.textContent) : {};
setOwner(CFG.owner);
const MAX_SECONDS = CFG.maxSeconds || 300;
const MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4;codecs=mp4a.40.2", "audio/mp4",
  "audio/ogg;codecs=opus", "audio/aac"];

const $ = (id) => document.getElementById(id);
const els = {
  recBtn: $("rec-btn"), recIcon: $("rec-icon"), recLabel: $("rec-label"), timer: $("rec-timer"),
  recStatus: $("rec-status"), player: $("rec-player"), discard: $("rec-discard"), recBox: $("rec-box"),
  text: $("note-text"), dictHint: $("dictation-hint"),
  search: $("customer-search"), results: $("customer-results"), selected: $("customer-selected"),
  selectedName: $("customer-selected-name"), clearCustomer: $("customer-clear"), customerId: $("customer-id"),
  sendBtn: $("send-btn"), sendStatus: $("send-status"),
};

const state = {
  recorder: null, stream: null, chunks: [], blob: null, mime: "", startedAt: 0, recordedAt: null,
  timerId: null, recognition: null, recognizing: false, dictBase: "", resultOffset: 0, wakeLock: null,
  stopResolvers: [], sending: false,
  // Pochodzenie tekstu w polu: dyktowanie na żywo (może być urwane) albo ręczna edycja.
  dictated: false, textEdited: false, dictationBroken: false, dictationIncomplete: false,
};

/** Źródło tekstu dla serwera: przy urwanym/niepewnym dyktowaniu serwer przepisze całe nagranie (STT). */
function textSource() {
  if (state.dictationIncomplete) return "DYKTOWANIE";
  if (state.textEdited) return "RECZNY";
  return state.dictated ? "DYKTOWANIE" : "";
}

/** Czyści tekst przy usunięciu / ponownym nagraniu; ręcznie wpisany tekst zostaje tylko na wyraźne życzenie. */
function clearTextForNewTake() {
  if (state.textEdited && els.text.value.trim()
      && window.confirm("Zachować wpisany ręcznie tekst? (Anuluj = wyczyść pole razem z dyktowaniem)")) {
    state.dictated = false;
    return;
  }
  els.text.value = "";
  state.dictBase = "";
  state.lastFinals = "";
  state.dictated = false;
  state.textEdited = false;
  state.dictationIncomplete = false;
}

/* ---------- pomocnicze UI ---------- */
function setStatus(el, text, kind) {
  if (!el) return;
  el.textContent = text || "";
  el.className = "text-sm min-h-5 " + ({ error: "text-error", ok: "text-success", warn: "text-warning" }[kind] || "opacity-80");
}

function fmt(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 60)).padStart(1, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function isRecording() {
  return !!(state.recorder && state.recorder.state === "recording") || (state.recognizing && !state.recorder);
}

function renderRecButton() {
  const recording = isRecording();
  els.recBtn.classList.toggle("btn-error", recording);
  els.recBtn.classList.toggle("btn-primary", !recording);
  els.recBtn.setAttribute("aria-pressed", recording ? "true" : "false");
  els.recIcon.textContent = recording ? "⏹️" : "🎙️";
  els.recLabel.textContent = recording ? "Zatrzymaj" : (state.blob ? "Nagraj od nowa" : "Nagrywaj");
  els.recBox.classList.toggle("ring-4", recording);
  els.recBox.classList.toggle("ring-error", recording);
}

/* ---------- wybór formatu ---------- */
function pickMime() {
  if (typeof MediaRecorder === "undefined") return null;
  if (typeof MediaRecorder.isTypeSupported !== "function") return "";
  for (const type of MIME_CANDIDATES) {
    try { if (MediaRecorder.isTypeSupported(type)) return type; } catch (e) { /* dalej */ }
  }
  return "";
}

/* ---------- dyktowanie na żywo ---------- */
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;

function startDictation() {
  if (!SpeechRec || state.recognition) return;
  let rec;
  try { rec = new SpeechRec(); } catch (e) { return; }
  rec.lang = "pl-PL";
  rec.continuous = true;
  rec.interimResults = true;
  state.dictBase = els.text.value ? els.text.value.replace(/\s*$/, " ") : "";
  state.resultOffset = 0;
  rec.onresult = (event) => {
    if (state.pendingOffset) { state.resultOffset = event.resultIndex; state.pendingOffset = false; }
    let finals = "";
    let interim = "";
    for (let i = state.resultOffset; i < event.results.length; i++) {
      const res = event.results[i];
      if (res.isFinal) finals += res[0].transcript;
      else interim += res[0].transcript;
    }
    state.lastFinals = finals;
    if (finals || interim) state.dictated = true;
    els.text.value = (state.dictBase + finals + interim).replace(/\s+/g, " ").trimStart();
  };
  rec.onerror = (event) => {
    if (event.error === "no-speech" || event.error === "aborted") return;
    state.dictationBroken = true;
    state.dictationIncomplete = true;
    setStatus(els.dictHint, "Dyktowanie na żywo przerwane — tekst poniżej może być niepełny. Nagrywanie trwa: "
      + "po wysłaniu serwer przepisze całe nagranie (gdy nie ma nagrania, wyślemy sam tekst).", "warn");
  };
  rec.onend = () => {
    // Chrome kończy sesję po ciszy — wznawiamy, dopóki trwa nagrywanie.
    state.dictBase = (state.dictBase + (state.lastFinals || "")).replace(/\s*$/, " ");
    state.lastFinals = "";
    state.resultOffset = 0;
    state.pendingOffset = false;
    els.text.value = state.dictBase.trimStart();
    if (state.recognizing && !state.dictationBroken) {
      try { rec.start(); return; } catch (e) { /* koniec */ }
    }
    state.recognition = null;
    if (!state.recorder) { state.recognizing = false; stopTimer(); renderRecButton(); }
  };
  state.recognition = rec;
  state.recognizing = true;
  state.dictationBroken = false;
  try {
    rec.start();
    setStatus(els.dictHint, "Dyktowanie na żywo włączone — tekst pojawia się poniżej, możesz go poprawić.");
  } catch (e) {
    state.recognition = null;
    state.recognizing = false;
  }
}

function stopDictation() {
  state.recognizing = false;
  if (state.recognition) {
    try { state.recognition.stop(); } catch (e) { /* ignoruj */ }
  }
}

// Ręczna poprawka w trakcie dyktowania: to, co jest w polu, staje się nową bazą,
// a dotychczasowe wyniki sesji rozpoznawania są pomijane (nowe dopiszą się od następnego zdarzenia).
els.text.addEventListener("input", () => {
  state.textEdited = true;
  if (!state.recognition) return;
  state.dictBase = els.text.value.replace(/\s*$/, " ");
  state.lastFinals = "";
  state.pendingOffset = true;
});

/* ---------- licznik i blokada wygaszania ekranu ---------- */
function startTimer() {
  state.startedAt = Date.now();
  els.timer.textContent = `0:00 / ${fmt(MAX_SECONDS)}`;
  stopTimer();
  state.timerId = setInterval(() => {
    const elapsed = (Date.now() - state.startedAt) / 1000;
    els.timer.textContent = `${fmt(elapsed)} / ${fmt(MAX_SECONDS)}`;
    if (elapsed >= MAX_SECONDS) {
      setStatus(els.recStatus, `Osiągnięto limit ${fmt(MAX_SECONDS)} — nagranie zatrzymane.`, "warn");
      stopRecording();
    }
  }, 250);
}

function stopTimer() {
  if (state.timerId) clearInterval(state.timerId);
  state.timerId = null;
}

async function keepScreenOn(on) {
  try {
    if (on && "wakeLock" in navigator) state.wakeLock = await navigator.wakeLock.request("screen");
    else if (!on && state.wakeLock) { await state.wakeLock.release(); state.wakeLock = null; }
  } catch (e) { /* niekrytyczne */ }
}

/* ---------- nagrywanie ---------- */
async function startRecording() {
  setStatus(els.recStatus, "");
  // „Nagraj od nowa”: stary tekst z dyktowania nie może dołączyć się do nowego nagrania.
  if (state.blob) clearTextForNewTake();
  const mime = pickMime();
  const canRecord = mime !== null && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
  if (!canRecord) {
    if (SpeechRec) {
      setStatus(els.recStatus, "Ta przeglądarka nie nagrywa dźwięku — używam samego dyktowania.", "warn");
      startDictation();
      startTimer();
      renderRecButton();
    } else {
      setStatus(els.recStatus, "Nagrywanie niedostępne w tej przeglądarce — wpisz tekst lub użyj mikrofonu na klawiaturze.", "error");
    }
    return;
  }
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (err) {
    const denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
    setStatus(els.recStatus, denied
      ? "Brak zgody na mikrofon. Zezwól w ustawieniach przeglądarki albo wpisz tekst poniżej."
      : "Nie udało się włączyć mikrofonu — wpisz tekst poniżej.", "error");
    return;
  }
  try {
    state.recorder = mime ? new MediaRecorder(state.stream, { mimeType: mime }) : new MediaRecorder(state.stream);
  } catch (e) {
    state.recorder = new MediaRecorder(state.stream);
  }
  state.mime = state.recorder.mimeType || mime || "audio/webm";
  state.chunks = [];
  state.blob = null;
  state.recordedAt = new Date().toISOString();
  els.player.classList.add("hidden");
  els.discard.classList.add("hidden");
  state.recorder.ondataavailable = (e) => { if (e.data && e.data.size) state.chunks.push(e.data); };
  state.recorder.onstop = () => {
    const type = (state.chunks[0] && state.chunks[0].type) || state.mime;
    state.blob = state.chunks.length ? new Blob(state.chunks, { type }) : null;
    state.chunks = [];
    if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
    state.stream = null;
    state.recorder = null;
    stopTimer();
    keepScreenOn(false);
    if (state.blob) {
      if (els.player.src) URL.revokeObjectURL(els.player.src);
      els.player.src = URL.createObjectURL(state.blob);
      els.player.classList.remove("hidden");
      els.discard.classList.remove("hidden");
      setStatus(els.recStatus, "Nagranie gotowe. Odsłuchaj, popraw tekst i wyślij.", "ok");
    } else {
      setStatus(els.recStatus, "Nagranie jest puste — spróbuj ponownie albo wpisz tekst.", "warn");
    }
    renderRecButton();
    const resolvers = state.stopResolvers.splice(0);
    resolvers.forEach((resolve) => resolve());
  };
  state.recorder.start(1000);
  startTimer();
  keepScreenOn(true);
  startDictation();
  setStatus(els.recStatus, "Nagrywam… mów swobodnie: klient, co ustaliliście, co trzeba zrobić.");
  renderRecButton();
}

function stopRecording() {
  stopDictation();
  if (state.recorder && state.recorder.state !== "inactive") {
    const done = new Promise((resolve) => state.stopResolvers.push(resolve));
    try { state.recorder.stop(); } catch (e) { return Promise.resolve(); }
    return done;
  }
  stopTimer();
  renderRecButton();
  return Promise.resolve();
}

els.recBtn.addEventListener("click", () => {
  if (isRecording()) stopRecording();
  else startRecording();
});

els.discard.addEventListener("click", () => {
  clearTextForNewTake();
  state.blob = null;
  if (els.player.src) URL.revokeObjectURL(els.player.src);
  els.player.removeAttribute("src");
  els.player.classList.add("hidden");
  els.discard.classList.add("hidden");
  setStatus(els.recStatus, "Nagranie usunięte.");
  renderRecButton();
});

/* ---------- wybór klienta (opcjonalny) ---------- */
let searchTimer = null;
let searchSeq = 0;

function selectCustomer(id, label) {
  els.customerId.value = id || "";
  els.selectedName.textContent = id ? label : "Nie wiem / AI rozpozna";
  els.clearCustomer.classList.toggle("hidden", !id);
  els.results.innerHTML = "";
  els.search.value = "";
}

function renderResults(results) {
  els.results.innerHTML = "";
  if (!results.length) {
    const li = document.createElement("li");
    li.className = "p-2 text-sm opacity-70";
    li.textContent = "Brak wyników — zostaw „AI rozpozna”.";
    els.results.appendChild(li);
    return;
  }
  for (const item of results) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-ghost btn-block justify-start h-auto py-2 text-left normal-case";
    const label = item.city ? `${item.name} · ${item.city}` : item.name;
    btn.textContent = label;
    btn.addEventListener("click", () => selectCustomer(item.id, label));
    li.appendChild(btn);
    els.results.appendChild(li);
  }
}

els.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = els.search.value.trim();
  if (q.length < 2) { els.results.innerHTML = ""; return; }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    try {
      const url = `${CFG.customerSearchUrl}?format=json&q=${encodeURIComponent(q)}`;
      const resp = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!resp.ok) throw new Error(String(resp.status));
      const data = await resp.json();
      if (seq === searchSeq) renderResults(Array.isArray(data.results) ? data.results : []);
    } catch (e) {
      if (seq !== searchSeq) return;
      els.results.innerHTML = "";
      const li = document.createElement("li");
      li.className = "p-2 text-sm text-warning";
      li.textContent = "Brak zasięgu — AI rozpozna klienta z nagrania.";
      els.results.appendChild(li);
    }
  }, 250);
});

els.clearCustomer.addEventListener("click", () => selectCustomer("", ""));

/* ---------- wysyłka ---------- */
function resetForm() {
  state.blob = null;
  if (els.player.src) URL.revokeObjectURL(els.player.src);
  els.player.removeAttribute("src");
  els.player.classList.add("hidden");
  els.discard.classList.add("hidden");
  els.text.value = "";
  state.dictBase = "";
  state.lastFinals = "";
  state.dictated = false;
  state.textEdited = false;
  state.dictationIncomplete = false;
  els.timer.textContent = `0:00 / ${fmt(MAX_SECONDS)}`;
  selectCustomer("", "");
  renderRecButton();
}

async function send() {
  if (state.sending) return;
  state.sending = true;
  els.sendBtn.disabled = true;
  try {
    if (isRecording()) await stopRecording();
    const text = els.text.value.trim();
    if (!(state.blob && state.blob.size) && !text) {
      setStatus(els.sendStatus, "Nagraj notatkę albo wpisz tekst.", "error");
      return;
    }
    setStatus(els.sendStatus, "Zapisuję…");
    const record = await buildRecord({
      blob: state.blob, text, textSource: text ? textSource() : "", customerId: els.customerId.value,
      recordedAt: state.recordedAt,
    });
    let queued = false;
    if (queueSupported()) {
      try { await addRecord(record); queued = true; } catch (e) { queued = false; }
    }
    if (!queued) {
      // Brak IndexedDB (np. tryb prywatny starszego Safari) — wysyłamy od razu, bez kolejki.
      try {
        const result = await sendRecord(record, CFG.uploadUrl);
        window.location.assign(result.url || CFG.homeUrl);
      } catch (err) {
        setStatus(els.sendStatus, err && err.message !== "network" ? err.message
          : "Brak zasięgu, a ta przeglądarka nie zapisuje offline. Nie zamykaj strony i spróbuj ponownie.", "error");
      }
      return;
    }
    setStatus(els.sendStatus, "Wysyłam…");
    const summary = await flushQueue(CFG.uploadUrl);
    const mine = summary.sent.find((s) => s.client_uuid === record.client_uuid);
    if (mine) {
      setStatus(els.sendStatus, "Wysłano ✔", "ok");
      window.location.assign(mine.url || CFG.homeUrl);
      return;
    }
    resetForm();
    if (summary.auth) {
      setStatus(els.sendStatus, "Notatka zapisana na telefonie. Zaloguj się ponownie — wyślę ją automatycznie.", "warn");
    } else if (summary.offline || navigator.onLine === false) {
      setStatus(els.sendStatus, "Zapisano offline – wyślę, gdy wróci zasięg", "warn");
    } else {
      setStatus(els.sendStatus, "Notatka zapisana na telefonie, ale serwer jej nie przyjął — sprawdź kolejkę na ekranie „Teren”.", "error");
    }
  } finally {
    state.sending = false;
    els.sendBtn.disabled = false;
  }
}

els.sendBtn.addEventListener("click", send);

window.addEventListener("beforeunload", (event) => {
  if (isRecording()) { event.preventDefault(); event.returnValue = ""; }
});

/* ---------- start ---------- */
(function init() {
  const initial = document.getElementById("initial-customer");
  if (initial) {
    const data = JSON.parse(initial.textContent);
    if (data && data.id) selectCustomer(data.id, data.label);
  }
  els.timer.textContent = `0:00 / ${fmt(MAX_SECONDS)}`;
  if (!SpeechRec) {
    setStatus(els.dictHint, "Możesz wpisać tekst albo podyktować go mikrofonem na klawiaturze telefonu.");
  }
  if (pickMime() === null && !SpeechRec) {
    els.recBtn.disabled = true;
    setStatus(els.recStatus, "Nagrywanie niedostępne w tej przeglądarce — wpisz tekst poniżej.", "warn");
  }
  renderRecButton();
  registerServiceWorker(CFG.swUrl);
  autoFlush(CFG.uploadUrl);
})();
