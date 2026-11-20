/* TinyCRM Teren — ekran startowy: rejestracja service workera, baner kolejki offline i automatyczna wysyłka. */
import { autoFlush, countForeignRecords, countRecords, deleteRecord, flushQueue, listRecords, QUEUE_EVENT,
  queueSupported, retryRecord, setOwner } from "./queue.js";
import { registerServiceWorker } from "./sw-register.js";

const cfgEl = document.getElementById("pwa-config");
const CFG = cfgEl ? JSON.parse(cfgEl.textContent) : {};
setOwner(CFG.owner);
const banner = document.getElementById("queue-banner");
const countEl = document.getElementById("queue-count");
const msgEl = document.getElementById("queue-message");
const listEl = document.getElementById("queue-list");
const sendNowBtn = document.getElementById("queue-send-now");

function plural(n) {
  if (n === 1) return "notatka czeka";
  const last = n % 10;
  const lastTwo = n % 100;
  return last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14) ? "notatki czekają" : "notatek czeka";
}

function when(iso) {
  try {
    return new Date(iso).toLocaleString("pl-PL", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  } catch (e) { return ""; }
}

async function render(extraMessage) {
  if (!banner || !queueSupported()) return;
  let rows = [];
  let foreign = 0;
  try { rows = await listRecords(); } catch (e) { return; }
  try { foreign = await countForeignRecords(); } catch (e) { foreign = 0; }
  banner.classList.toggle("hidden", rows.length === 0 && foreign === 0);
  countEl.textContent = `${rows.length} ${plural(rows.length)} na wysłanie`;
  const foreignNote = foreign
    ? ` Na tym telefonie są też notatki innego konta (${foreign}) — wyślą się po zalogowaniu ich autora.` : "";
  msgEl.textContent = (extraMessage || (navigator.onLine === false
    ? "Brak zasięgu — wyślę automatycznie, gdy wróci." : "")) + foreignNote;
  listEl.innerHTML = "";
  for (const row of rows) {
    const li = document.createElement("li");
    li.className = "flex flex-wrap items-center gap-2 text-sm py-1";
    const label = document.createElement("span");
    label.className = "flex-1 min-w-0 truncate";
    const kind = row.audio ? "🎙️" : "✍️";
    const preview = row.text ? ` — ${row.text.slice(0, 60)}` : "";
    label.textContent = `${kind} ${when(row.recorded_at)}${preview}`;
    li.appendChild(label);
    if (row.state === "failed") {
      const err = document.createElement("span");
      err.className = "badge badge-error badge-sm";
      err.textContent = row.last_error || "Odrzucona";
      li.appendChild(err);
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "btn btn-xs";
      retry.textContent = "Ponów";
      retry.addEventListener("click", async () => { await retryRecord(row.client_uuid); run(); });
      li.appendChild(retry);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn-xs btn-ghost text-error";
      del.textContent = "Usuń";
      del.addEventListener("click", async () => {
        if (window.confirm("Usunąć tę notatkę z telefonu? Nie da się jej przywrócić.")) {
          await deleteRecord(row.client_uuid);
          render();
        }
      });
      li.appendChild(del);
    }
    listEl.appendChild(li);
  }
}

function describe(summary) {
  if (!summary) return "";
  if (summary.auth) return "Sesja wygasła — zaloguj się ponownie, a notatki wyślą się same.";
  if (summary.sent.length && !summary.remaining) return "";
  if (summary.failed) return "Część notatek została odrzucona przez serwer — ponów albo usuń.";
  if (summary.offline) return "Brak zasięgu — wyślę automatycznie, gdy wróci.";
  return "";
}

async function run() {
  if (!queueSupported()) return null;
  const summary = await flushQueue(CFG.uploadUrl);
  await render(describe(summary));
  if (summary.sent.length) {
    // Wysłane notatki pojawią się w sekcji „Moje notatki” po odświeżeniu.
    const notesBox = document.getElementById("notes-refresh");
    if (notesBox) notesBox.classList.remove("hidden");
  }
  return summary;
}

if (sendNowBtn) sendNowBtn.addEventListener("click", run);
window.addEventListener(QUEUE_EVENT, () => render());
window.addEventListener("offline", () => render());

registerServiceWorker(CFG.swUrl);
if (queueSupported()) {
  Promise.all([countRecords(), countForeignRecords()])
    .then(([own, foreign]) => { if (own || foreign) render(); }).catch(() => {});
  autoFlush(CFG.uploadUrl, (summary) => render(describe(summary)));
  run();
}
