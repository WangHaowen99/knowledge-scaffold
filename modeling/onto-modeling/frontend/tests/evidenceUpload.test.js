// 分块上传目标程序（EV-31～EV-33）的状态机：顺序传块、整文件哈希、自动重试当前块、手动续传、补缺块、从头重来、取消
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { checkArtifactFile, chunkCount, createArtifactUpload, isUploading } from '../src/pages/evidence/artifactUpload.js';

const bytesOf = (n) => Uint8Array.from({ length: n }, (_, i) => (i * 7 + 3) & 0xff);
const fakeFile = (n) => ({ name: 'sample-orders.zip', size: n, bytes: bytesOf(n) });
const read = async (f, s, e) => f.bytes.slice(s, e);
const apiErr = (status, code, details) => Object.assign(new Error(code), { status, code, details });

// 假接口：记录调用；failPut(index, attempt) 返回要抛的错误
function fakeApi({ chunk = 4, failPut = () => null, complete } = {}) {
  const calls = [];
  let uploads = 0;
  const attempts = new Map();
  return {
    calls,
    startEvidenceUpload: async (body) => { calls.push(['start', body]); uploads += 1; return { upload_id: `up-${uploads}`, chunk_bytes: chunk, max_bytes: 1 << 30 }; },
    putEvidenceUploadChunk: async (id, i, bytes, o) => {
      const n = (attempts.get(`${id}/${i}`) || 0) + 1;
      attempts.set(`${id}/${i}`, n);
      calls.push(['put', id, i, bytes.length, !!o?.signal]);
      const e = failPut(i, n, id);
      if (e) throw e;
      return { received_bytes: bytes.length, received_chunks: i + 1 };
    },
    completeEvidenceUpload: async (id, body) => {
      calls.push(['complete', id, body.sha256]);
      const e = complete?.(id, calls.filter((c) => c[0] === 'complete').length);
      if (e) throw e;
      return { artifact: { sha256: body.sha256, size: 10, filename: 'sample-orders.zip' } };
    },
  };
}
const sha = (u8) => createHash('sha256').update(u8).digest('hex');
const noSleep = async () => {};

test('顺序传块：最后一块不满，整文件哈希随 EV-33 带上，进度到 100', async () => {
  const file = fakeFile(10);
  const api = fakeApi();
  const seen = [];
  const up = createArtifactUpload({ api, file, read, sleep: noSleep, onChange: (s) => seen.push([s.phase, s.pct]) });
  await up.start();
  assert.deepEqual(api.calls.map((c) => c.slice(0, 4)), [
    ['start', { filename: 'sample-orders.zip', size: 10 }], ['put', 'up-1', 0, 4], ['put', 'up-1', 1, 4], ['put', 'up-1', 2, 2], ['complete', 'up-1', sha(file.bytes)],
  ]);
  assert.ok(api.calls.filter((c) => c[0] === 'put').every((c) => c[4]), '每块都带取消信号');
  assert.equal(up.state.phase, 'done');
  assert.equal(up.state.artifact.sha256, sha(file.bytes));
  assert.deepEqual(seen.map((x) => x[0]).filter((p, i, a) => p !== a[i - 1]), ['starting', 'uploading', 'finishing', 'done']);
  assert.deepEqual(seen.filter((x) => x[0] === 'uploading').map((x) => x[1]), [0, 40, 80, 100]);
  assert.equal(chunkCount(10, 4), 3);
});

test('网络类失败自动重试当前块；同一块不重复算哈希', async () => {
  const file = fakeFile(10);
  const waits = [];
  const api = fakeApi({ failPut: (i, n) => (i === 1 && n === 1 ? apiErr(503, 'upstream_unavailable') : i === 2 && n === 1 ? apiErr(0, 'network_error') : null) });
  const up = createArtifactUpload({ api, file, read, sleep: async (ms) => waits.push(ms) });
  await up.start();
  assert.deepEqual(api.calls.filter((c) => c[0] === 'put').map((c) => c[2]), [0, 1, 1, 2, 2]);
  assert.equal(waits.length, 2);
  assert.equal(up.state.phase, 'done');
  assert.equal(api.calls.at(-1)[2], sha(file.bytes));
});

test('自动重试用完停在 failed；retry() 从当前块接着传，哈希仍对', async () => {
  const file = fakeFile(10);
  let down = true;
  const api = fakeApi({ failPut: (i) => (down && i === 1 ? apiErr(502, 'upstream_unavailable') : null) });
  const up = createArtifactUpload({ api, file, read, sleep: noSleep, retries: 2 });
  await up.start();
  assert.equal(up.state.phase, 'failed');
  assert.equal(up.state.error.code, 'upstream_unavailable');
  assert.equal(up.state.pct, 40);
  assert.equal(api.calls.filter((c) => c[0] === 'put' && c[2] === 1).length, 3);
  down = false;
  await up.retry();
  assert.equal(up.state.phase, 'done');
  assert.equal(api.calls.filter((c) => c[0] === 'start').length, 1);
  assert.deepEqual(api.calls.filter((c) => c[0] === 'put').map((c) => c[2]), [0, 1, 1, 1, 1, 2]);
  assert.equal(api.calls.at(-1)[2], sha(file.bytes));
});

test('4xx 不自动重试；完成时缺块只补缺的；过期或哈希不符时重试从头开新上传', async () => {
  const file = fakeFile(10);
  const bad = fakeApi({ failPut: (i, n) => (i === 0 && n === 1 ? apiErr(400, 'invalid_argument') : null) });
  const u1 = createArtifactUpload({ api: bad, file, read, sleep: noSleep });
  await u1.start();
  assert.equal(u1.state.phase, 'failed');
  assert.equal(bad.calls.filter((c) => c[0] === 'put').length, 1);

  const gap = fakeApi({ complete: (id, n) => (n === 1 ? apiErr(409, 'upload_incomplete', { missing: [1] }) : null) });
  const u2 = createArtifactUpload({ api: gap, file, read, sleep: noSleep });
  await u2.start();
  assert.equal(u2.state.phase, 'failed');
  await u2.retry();
  assert.equal(u2.state.phase, 'done');
  assert.deepEqual(gap.calls.map((c) => [c[0], c[2]]).slice(4), [['complete', sha(file.bytes)], ['put', 1], ['complete', sha(file.bytes)]]);

  const mism = fakeApi({ complete: (id) => (id === 'up-1' ? apiErr(422, 'upload_hash_mismatch') : null) });
  const u3 = createArtifactUpload({ api: mism, file, read, sleep: noSleep });
  await u3.start();
  await u3.retry();
  assert.equal(u3.state.phase, 'done');
  assert.deepEqual(mism.calls.filter((c) => c[0] === 'start').length, 2);
  assert.deepEqual(mism.calls.at(-1), ['complete', 'up-2', sha(file.bytes)]);
});

test('取消：中止在途请求，停在 cancelled，不再继续', async () => {
  const file = fakeFile(10);
  let up;
  const api = fakeApi();
  const put = api.putEvidenceUploadChunk;
  api.putEvidenceUploadChunk = (id, i, bytes, o) => {
    if (i === 1) {
      up.cancel();
      return new Promise((_, reject) => { if (o.signal.aborted) reject(new DOMException('aborted', 'AbortError')); });
    }
    return put(id, i, bytes, o);
  };
  up = createArtifactUpload({ api, file, read, sleep: noSleep });
  assert.equal(isUploading(up.state), false);
  await up.start();
  assert.equal(up.state.phase, 'cancelled');
  assert.ok(!api.calls.some((c) => c[0] === 'complete'));
  await up.retry();
  assert.equal(up.state.phase, 'cancelled');
});

test('选文件先挡：空文件、超过上限', () => {
  assert.equal(checkArtifactFile({ size: 0 }, 100), 'empty');
  assert.equal(checkArtifactFile({ size: 101 }, 100), 'too_large');
  assert.equal(checkArtifactFile({ size: 100 }, 100), null);
  assert.equal(checkArtifactFile({ size: 5 }, null), null);
});
