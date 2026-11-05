// 增量 SHA-256：标准向量（FIPS 180-4 / NIST），以及任意切块、跨 64 字节边界和补位边界时与 node:crypto 一致
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { createSha256, sha256Hex } from '../src/lib/sha256.js';

const enc = (s) => new TextEncoder().encode(s);

test('标准向量', () => {
  assert.equal(sha256Hex(enc('')), 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  assert.equal(sha256Hex(enc('abc')), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  assert.equal(sha256Hex(enc('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq')), '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1');
  assert.equal(
    sha256Hex(enc('abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu')),
    'cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1',
  );
});

test('一百万个 a：分块喂入，块长跨 64 字节边界', () => {
  const a = new Uint8Array(1000000).fill(0x61);
  const want = 'cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0';
  assert.equal(sha256Hex(a), want);
  for (const step of [1, 7, 63, 64, 65, 1000, 8191]) {
    const h = createSha256();
    for (let i = 0; i < a.length; i += step) h.update(a.subarray(i, i + step));
    assert.equal(h.hex(), want, `块长 ${step}`);
  }
});

test('补位边界与随机切块：和 node:crypto 一致；接受 ArrayBuffer 与其他视图', () => {
  let seed = 7;
  const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed; };
  for (const n of [0, 1, 55, 56, 57, 63, 64, 65, 119, 120, 127, 128, 129, 4096, 70001]) {
    const data = Uint8Array.from({ length: n }, () => rnd() & 0xff);
    const want = createHash('sha256').update(data).digest('hex');
    const h = createSha256();
    for (let i = 0; i < n;) {
      const len = 1 + (rnd() % 150);
      h.update(data.subarray(i, i + len));
      i += len;
    }
    assert.equal(h.hex(), want, `长度 ${n}`);
  }
  const buf = enc('abc').buffer;
  assert.equal(sha256Hex(buf), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  assert.equal(sha256Hex(new DataView(enc('xabc').buffer, 1)), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
});

test('取过结果后再取一样，不能再追加', () => {
  const h = createSha256().update(enc('abc'));
  assert.equal(h.hex(), h.hex());
  assert.throws(() => h.update(enc('d')));
});
