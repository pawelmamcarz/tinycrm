/* TinyCRM Teren — rejestracja service workera z zakresem „/” (nagłówek Service-Worker-Allowed z serwera). */
export function registerServiceWorker(swUrl) {
  if (!("serviceWorker" in navigator) || !swUrl) return Promise.resolve(null);
  return navigator.serviceWorker.register(swUrl, { scope: "/" })
    .catch(() => navigator.serviceWorker.register(swUrl))
    .catch(() => null);
}
