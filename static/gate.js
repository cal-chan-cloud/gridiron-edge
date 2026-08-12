/* Passphrase gate for an encrypted build.
 *
 * If encrypt_build.js has run, docs/data holds ciphertext and a manifest. This
 * asks for the passphrase, derives the key, and then transparently decrypts
 * every data request by wrapping fetch - so app.js needs no knowledge of any of
 * it and behaves identically on a plain build.
 *
 * On an unencrypted build this file does nothing at all.
 */
(function () {
  'use strict';

  const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const KEY_CACHE = 'gridiron.key';

  async function deriveKey(pass, salt, iterations) {
    const base = await crypto.subtle.importKey(
      'raw', new TextEncoder().encode(pass), 'PBKDF2', false, ['deriveKey']);
    return crypto.subtle.deriveKey(
      { name: 'PBKDF2', salt, iterations, hash: 'SHA-256' },
      base, { name: 'AES-GCM', length: 256 }, true, ['decrypt']);
  }

  async function decrypt(key, buf) {
    const bytes = new Uint8Array(buf);
    const iv = bytes.slice(0, 12);
    const body = bytes.slice(12);       // ciphertext || GCM tag
    const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, key, body);
    return new TextDecoder().decode(plain);
  }

  function prompt(manifest, onKey) {
    const wrap = document.createElement('div');
    wrap.className = 'gate';
    wrap.innerHTML = `
      <form class="gate-box" autocomplete="on">
        <div class="gate-logo">🏈</div>
        <h1>Gridiron Edge</h1>
        <p>This board is encrypted. Enter the passphrase to unlock it.</p>
        <input type="password" id="gatePass" placeholder="Passphrase"
               autocomplete="current-password" autofocus>
        <button type="submit" id="gateGo">Unlock</button>
        <div class="gate-err" id="gateErr" hidden></div>
        <small>Decryption happens in this browser. The passphrase is never sent anywhere.</small>
      </form>`;
    document.body.appendChild(wrap);

    const err = wrap.querySelector('#gateErr');
    const input = wrap.querySelector('#gatePass');
    wrap.querySelector('form').onsubmit = async ev => {
      ev.preventDefault();
      const btn = wrap.querySelector('#gateGo');
      btn.disabled = true; btn.textContent = 'Unlocking…'; err.hidden = true;
      try {
        const key = await deriveKey(input.value, b64(manifest.salt), manifest.iterations);
        // Verify against the known plaintext so a wrong passphrase fails here,
        // clearly, rather than as a confusing parse error three requests later.
        const probe = await decrypt(key, b64(manifest.check).buffer);
        if (probe !== 'gridiron-edge') throw new Error('bad key');
        const raw = await crypto.subtle.exportKey('raw', key);
        sessionStorage.setItem(KEY_CACHE,
          btoa(String.fromCharCode(...new Uint8Array(raw))));
        wrap.remove();
        onKey(key);
      } catch (e) {
        err.textContent = 'That passphrase does not unlock this board.';
        err.hidden = false;
        btn.disabled = false; btn.textContent = 'Unlock';
        input.select();
      }
    };
  }

  /* Wrap fetch so data/*.json transparently resolves to its encrypted twin. */
  function install(key) {
    const original = window.fetch.bind(window);
    window.fetch = async (url, opts) => {
      if (typeof url === 'string' && /^data\/.*\.json$/.test(url)) {
        const res = await original(url + '.enc', opts);
        if (!res.ok) return res;
        const text = await decrypt(key, await res.arrayBuffer());
        return new Response(text, {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
      return original(url, opts);
    };
  }

  window.__gateReady = (async () => {
    let manifest;
    try {
      const r = await fetch('data/manifest.json', { cache: 'no-store' });
      if (!r.ok) return;                       // plain build: nothing to do
      manifest = await r.json();
    } catch (e) { return; }
    if (!manifest || !manifest.encrypted) return;

    // A key from earlier this session means no re-prompting on every reload.
    const cached = sessionStorage.getItem(KEY_CACHE);
    if (cached) {
      try {
        const key = await crypto.subtle.importKey(
          'raw', b64(cached), 'AES-GCM', true, ['decrypt']);
        if (await decrypt(key, b64(manifest.check).buffer) === 'gridiron-edge') {
          install(key);
          return;
        }
      } catch (e) { sessionStorage.removeItem(KEY_CACHE); }
    }

    await new Promise(resolve => {
      const start = () => prompt(manifest, key => { install(key); resolve(); });
      if (document.body) start();
      else document.addEventListener('DOMContentLoaded', start);
    });
  })();
})();
