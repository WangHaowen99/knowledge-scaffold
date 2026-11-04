// Login handoff from the modeling page that opened this window. Credentials only travel through
// window messages between trusted origins, never through the URL.
export const READY = 'onto-eval-ready';
export const HANDOFF = 'onto-eval-handoff';
const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);

export function isTrustedOrigin(origin, trusted, here) {
  if (trusted.length) return trusted.includes(origin);
  try {
    // Without explicit configuration, only pages served from this host name are trusted;
    // loopback names count as one host so a localhost modeling page reaches a 127.0.0.1 evaluation page.
    const from = new URL(origin).hostname, to = new URL(here).hostname;
    return from === to || (LOOPBACK.has(from) && LOOPBACK.has(to));
  } catch {
    return false;
  }
}

export function receiveHandoff({ opener, trusted, here, target, timeoutMs = 5000 }) {
  return new Promise((resolve) => {
    if (!opener) { resolve(null); return; }
    const finish = (value) => { clearTimeout(timer); target.removeEventListener('message', onMessage); resolve(value); };
    const timer = setTimeout(() => finish(null), timeoutMs);
    function onMessage(event) {
      const data = event.data || {};
      if (event.source !== opener || !isTrustedOrigin(event.origin, trusted, here) || data.type !== HANDOFF) return;
      if (typeof data.access_token !== 'string' || !data.access_token) return;
      finish({ access_token: data.access_token, refresh_token: typeof data.refresh_token === 'string' ? data.refresh_token : '' });
    }
    target.addEventListener('message', onMessage);
    // The ready signal carries no credentials, so its target origin need not be restricted.
    opener.postMessage({ type: READY }, '*');
  });
}
