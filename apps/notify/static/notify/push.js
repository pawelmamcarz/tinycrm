/* TinyCRM — włączanie powiadomień web push (przycisk na liście powiadomień).
 * Wymaga: service worker (moduł pwa), PushManager, Notification API i klucza VAPID z serwera.
 * iOS/iPadOS: push działa tylko w aplikacji dodanej do ekranu początkowego (PWA). */
(function () {
  "use strict";
  var box = document.getElementById("push-box");
  if (!box) return;
  var enableBtn = box.querySelector("[data-push-enable]");
  var disableBtn = box.querySelector("[data-push-disable]");
  var statusEl = box.querySelector("[data-push-status]");

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = "text-sm " + (kind === "error" ? "text-error" : kind === "ok" ? "text-success" : "opacity-80");
  }
  function show(el, visible) { if (el) el.classList.toggle("hidden", !visible); }

  function isIOS() {
    return /iphone|ipad|ipod/i.test(navigator.userAgent) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  }
  function isStandalone() {
    return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
      window.navigator.standalone === true;
  }
  function supported() {
    return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  }

  function urlBase64ToUint8Array(base64) {
    var padding = "=".repeat((4 - (base64.length % 4)) % 4);
    var raw = atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function postJSON(url, data) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": box.dataset.csrf },
      body: JSON.stringify(data)
    }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }

  function getRegistration() {
    var swUrl = box.dataset.swUrl;
    return navigator.serviceWorker.getRegistration().then(function (reg) {
      if (reg) return reg;
      if (!swUrl) throw new Error("no-sw");
      return navigator.serviceWorker.register(swUrl, { scope: "/" }).catch(function () {
        return navigator.serviceWorker.register(swUrl);
      });
    }).then(function () { return navigator.serviceWorker.ready; });
  }

  function enable() {
    enableBtn.disabled = true;
    setStatus("Włączanie…");
    Notification.requestPermission().then(function (permission) {
      if (permission !== "granted") throw new Error("denied");
      return fetch(box.dataset.vapidUrl, { credentials: "same-origin" }).then(function (r) { return r.json(); });
    }).then(function (data) {
      if (!data.publicKey) throw new Error("no-key");
      return getRegistration().then(function (reg) {
        function fresh() {
          return reg.pushManager.subscribe({
            userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(data.publicKey)
          });
        }
        function send(sub) {
          var json = sub.toJSON();
          return postJSON(box.dataset.subscribeUrl, { endpoint: json.endpoint, keys: json.keys || {} });
        }
        return reg.pushManager.getSubscription().then(function (existing) {
          return existing || fresh();
        }).then(function (sub) {
          return send(sub).catch(function (err) {
            // 409: subskrypcja tej przeglądarki należy do innego konta — wymień ją na nową (nowy endpoint).
            if (!err || err.message !== "HTTP 409") throw err;
            return sub.unsubscribe().then(fresh).then(send);
          });
        });
      });
    }).then(function () {
      setStatus("Powiadomienia włączone na tym urządzeniu.", "ok");
      show(enableBtn, false); show(disableBtn, true);
    }).catch(function (err) {
      var msg = {
        "denied": "Zablokowano powiadomienia. Zezwól na nie w ustawieniach przeglądarki i spróbuj ponownie.",
        "no-key": "Serwer nie ma skonfigurowanych kluczy push — powiadomienia widać tylko na tej liście.",
        "no-sw": "Brak service workera aplikacji — otwórz aplikację terenową i spróbuj ponownie."
      }[err && err.message] || "Nie udało się włączyć powiadomień. Spróbuj ponownie później.";
      setStatus(msg, "error");
    }).finally(function () { enableBtn.disabled = false; });
  }

  function disable() {
    disableBtn.disabled = true;
    navigator.serviceWorker.getRegistration().then(function (reg) {
      return reg ? reg.pushManager.getSubscription() : null;
    }).then(function (sub) {
      if (!sub) return null;
      var endpoint = sub.endpoint;
      return sub.unsubscribe().then(function () { return postJSON(box.dataset.unsubscribeUrl, { endpoint: endpoint }); });
    }).then(function () {
      setStatus("Powiadomienia wyłączone na tym urządzeniu.");
      show(enableBtn, true); show(disableBtn, false);
    }).catch(function () {
      setStatus("Nie udało się wyłączyć powiadomień.", "error");
    }).finally(function () { disableBtn.disabled = false; });
  }

  if (!supported()) {
    show(enableBtn, false);
    if (isIOS() && !isStandalone()) {
      setStatus("Na iPhonie powiadomienia działają po dodaniu aplikacji do ekranu początkowego: " +
        "Udostępnij → „Do ekranu początkowego”, potem otwórz TinyCRM z ikony i wróć tutaj.");
    } else {
      setStatus("Ta przeglądarka nie obsługuje powiadomień push — powiadomienia widać na tej liście.");
    }
    return;
  }
  if (Notification.permission === "denied") {
    setStatus("Powiadomienia są zablokowane w ustawieniach przeglądarki dla tej strony.", "error");
  }
  navigator.serviceWorker.getRegistration().then(function (reg) {
    return reg ? reg.pushManager.getSubscription() : null;
  }).then(function (sub) {
    if (sub && Notification.permission === "granted") {
      setStatus("Powiadomienia są włączone na tym urządzeniu.", "ok");
      show(enableBtn, false); show(disableBtn, true);
    }
  }).catch(function () { /* brak informacji — zostaw domyślny stan */ });

  enableBtn.addEventListener("click", enable);
  disableBtn.addEventListener("click", disable);
})();
