/* Only human interaction renews a session; feeds, polling and focus do not. */
(() => {
  'use strict';
  const script = document.currentScript;
  const timeout = Number(script.dataset.timeout) * 1000;
  const storageKey = 'mb.activity.' + script.dataset.session;
  let last = Date.now() - timeout + Number(script.dataset.remaining) * 1000;
  let lastSent = 0, lastShared = 0, pending = false, locked = false;
  const csrf = script.parentElement.querySelector('[name=csrfmiddlewaretoken]').value;
  function readShared() {
    try {
      const value = Number(localStorage.getItem(storageKey));
      if (value > last && value <= Date.now()) last = value;
    } catch (_) {}
  }
  async function lock() {
    if (locked) return;
    locked = true;
    // Remove account information immediately, including on a suspended tab.
    document.body.replaceChildren();
    const message = document.createElement('p');
    message.textContent = 'Sesión bloqueada por inactividad. Vuelve a identificarte.';
    document.body.appendChild(message);
    try {
      await fetch(script.dataset.lock, {method: 'POST', credentials: 'same-origin',
        headers: {'X-CSRFToken': csrf, 'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams({session: script.dataset.session}), signal: AbortSignal.timeout(2500)});
    } catch (_) {}
    location.replace(script.dataset.login);
  }
  function check() {
    readShared();
    if (Date.now() - last >= timeout) { lock(); return false; }
    return !locked;
  }
  async function renew() {
    if (pending || !check() || last <= lastSent) return;
    pending = true;
    const sent = last;
    try {
      const response = await fetch(script.dataset.activity, {method: 'POST', credentials: 'same-origin',
        headers: {'X-CSRFToken': csrf, 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
        body: new URLSearchParams({idle_seconds: String(Math.max(0, (Date.now() - sent) / 1000))}),
        signal: AbortSignal.timeout(4000)});
      if (response.status === 401 || response.status === 403) { lock(); return; }
      if (response.ok) lastSent = sent;
    } catch (_) { /* Server lease still expires; no offline authorization. */ }
    finally { pending = false; }
  }
  function interacted(event) {
    if (!event.isTrusted || document.hidden || !check()) return;
    last = Date.now();
    if (last - lastShared >= 1000) {
      lastShared = last;
      try { localStorage.setItem(storageKey, String(last)); } catch (_) {}
    }
  }
  ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart'].forEach(name =>
    document.addEventListener(name, interacted, {passive: true, capture: true}));
  window.addEventListener('pagehide', () => {
    if (locked || last <= lastSent || Date.now() - last >= timeout) return;
    if (navigator.sendBeacon) navigator.sendBeacon(script.dataset.activity,
      new URLSearchParams({csrfmiddlewaretoken: csrf, idle_seconds: String(Math.max(0, (Date.now() - last) / 1000))}));
  });
  window.addEventListener('storage', check);
  window.addEventListener('pageshow', check);
  document.addEventListener('visibilitychange', check);
  setInterval(check, 500);
  setInterval(renew, 3000);
  lastSent = last; // Page loads themselves are not human interaction.
  check();
})();
