// 增量 SHA-256（FIPS 180-4）。浏览器的 crypto.subtle.digest 只能一次喂完整个文件，几百 MB 的程序要边读边算。
// 用法：const h = createSha256(); h.update(bytes)…; h.hex()。hex() 之后不能再 update。

const K = new Int32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

const asBytes = (data) => (data instanceof Uint8Array ? data
  : ArrayBuffer.isView(data) ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength)
    : new Uint8Array(data));

export function createSha256() {
  const H = new Int32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
  const W = new Int32Array(64);
  const buf = new Uint8Array(64);
  let bufLen = 0;
  let total = 0;
  let digest = null;

  function block(b, o) {
    for (let i = 0; i < 16; i += 1) {
      const j = o + i * 4;
      W[i] = (b[j] << 24) | (b[j + 1] << 16) | (b[j + 2] << 8) | b[j + 3];
    }
    for (let i = 16; i < 64; i += 1) {
      const x = W[i - 15];
      const y = W[i - 2];
      const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
      const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
      W[i] = (W[i - 16] + s0 + W[i - 7] + s1) | 0;
    }
    let a = H[0]; let b2 = H[1]; let c = H[2]; let d = H[3];
    let e = H[4]; let f = H[5]; let g = H[6]; let h = H[7];
    for (let i = 0; i < 64; i += 1) {
      const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      const t1 = (h + S1 + ((e & f) ^ (~e & g)) + K[i] + W[i]) | 0;
      const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      const t2 = (S0 + ((a & b2) ^ (a & c) ^ (b2 & c))) | 0;
      h = g; g = f; f = e; e = (d + t1) | 0;
      d = c; c = b2; b2 = a; a = (t1 + t2) | 0;
    }
    H[0] = (H[0] + a) | 0; H[1] = (H[1] + b2) | 0; H[2] = (H[2] + c) | 0; H[3] = (H[3] + d) | 0;
    H[4] = (H[4] + e) | 0; H[5] = (H[5] + f) | 0; H[6] = (H[6] + g) | 0; H[7] = (H[7] + h) | 0;
  }

  // 不满 64 字节的尾巴留在 buf 里，等下一次 update 拼上
  function feed(bytes) {
    const n = bytes.length;
    let i = 0;
    if (bufLen) {
      i = Math.min(64 - bufLen, n);
      buf.set(bytes.subarray(0, i), bufLen);
      bufLen += i;
      if (bufLen < 64) return;
      block(buf, 0);
      bufLen = 0;
    }
    for (; i + 64 <= n; i += 64) block(bytes, i);
    if (i < n) {
      buf.set(bytes.subarray(i), 0);
      bufLen = n - i;
    }
  }

  return {
    update(data) {
      if (digest) throw new Error('sha256：已经取过结果，不能再追加');
      const bytes = asBytes(data);
      total += bytes.length;
      feed(bytes);
      return this;
    },
    hex() {
      if (digest) return digest;
      // 补 0x80、补零到 56 字节，再写 64 位大端的比特长度
      const pad = new Uint8Array((bufLen < 56 ? 64 : 128) - bufLen);
      pad[0] = 0x80;
      const v = new DataView(pad.buffer);
      v.setUint32(pad.length - 8, Math.floor(total / 0x20000000));
      v.setUint32(pad.length - 4, (total * 8) >>> 0);
      feed(pad);
      digest = Array.from(H, (x) => (x >>> 0).toString(16).padStart(8, '0')).join('');
      return digest;
    },
  };
}

export const sha256Hex = (data) => createSha256().update(data).hex();
