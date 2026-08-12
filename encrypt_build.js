/* Encrypt the static data payload so a public URL is not a public board.
 *
 * GitHub Pages has no server, so it cannot check a password - anything that
 * merely HIDES the board in JavaScript is bypassed by opening devtools. This
 * instead encrypts the data itself: what gets published is ciphertext, and
 * without the passphrase there is nothing to read. That is real protection, not
 * a cosmetic gate.
 *
 * AES-256-GCM, key derived with PBKDF2-SHA256 (250k iterations, random salt).
 * GCM is authenticated, so a tampered file fails to decrypt rather than
 * silently returning nonsense.
 *
 *   node encrypt_build.js "<passphrase>"        # run after export_static.py
 *
 * Caveats worth knowing, because encryption invites over-confidence:
 *   - anyone you give the passphrase to can pass it on
 *   - the ciphertext is public, so a weak passphrase can be attacked offline at
 *     leisure. Use something long. The 250k PBKDF2 iterations make each guess
 *     expensive, not impossible.
 */
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const ITERATIONS = 250000;
const DATA_DIR = path.join(__dirname, 'docs', 'data');

function encrypt(key, plaintext) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  const ct = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  // WebCrypto expects the GCM tag appended to the ciphertext; node keeps them
  // separate, so join them here or the browser cannot decrypt.
  return Buffer.concat([iv, ct, cipher.getAuthTag()]);
}

function main() {
  const pass = process.argv[2];
  if (!pass) {
    console.error('usage: node encrypt_build.js "<passphrase>"');
    process.exit(2);
  }
  if (pass.length < 10) {
    console.error('Refusing: the ciphertext is public, so a short passphrase can be');
    console.error('cracked offline. Use at least 10 characters (a phrase is ideal).');
    process.exit(2);
  }
  if (!fs.existsSync(DATA_DIR)) {
    console.error(`No ${DATA_DIR} - run export_static.py first.`);
    process.exit(1);
  }

  const salt = crypto.randomBytes(16);
  const key = crypto.pbkdf2Sync(pass, salt, ITERATIONS, 32, 'sha256');

  const files = fs.readdirSync(DATA_DIR).filter(f => f.endsWith('.json'));
  let total = 0;
  for (const f of files) {
    const src = path.join(DATA_DIR, f);
    const blob = encrypt(key, fs.readFileSync(src, 'utf8'));
    fs.writeFileSync(src + '.enc', blob);
    fs.unlinkSync(src);                       // publish ciphertext only
    total += blob.length;
    console.log(`  ${f} -> ${f}.enc  ${(blob.length / 1024).toFixed(0)} KB`);
  }

  // A known plaintext lets the page reject a wrong passphrase immediately
  // instead of failing later with a confusing parse error.
  fs.writeFileSync(path.join(DATA_DIR, 'manifest.json'), JSON.stringify({
    encrypted: true,
    salt: salt.toString('base64'),
    iterations: ITERATIONS,
    check: encrypt(key, 'gridiron-edge').toString('base64'),
  }));

  console.log(`\nEncrypted ${files.length} files (${(total / 1024 / 1024).toFixed(2)} MB).`);
  console.log('docs/ now contains no readable data.');
}

main();
