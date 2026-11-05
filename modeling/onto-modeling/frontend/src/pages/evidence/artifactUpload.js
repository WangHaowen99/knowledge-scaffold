// 分块上传目标程序（EV-31 开始 → EV-32 逐块 PUT → EV-33 带整文件 sha256 完成）。
// 一次只读一块进内存，块发出去后趁网络在传时算进哈希；网络类失败先自动重试当前块，还不行就停在 failed，
// retry() 从当前块接着传（哈希不重算）；上传过期或哈希对不上时 retry() 从头来；cancel() 中止在途请求。
// 纯逻辑：api、读块、等待都由调用方给，单测换成假的。
import { createSha256 } from '../../lib/sha256.js';

export const UPLOAD_RETRIES = 2;

// phase：starting（EV-31）→ uploading → finishing（EV-33，服务端在拼块、算哈希）→ done；中途 failed / cancelled
export const UPLOAD_ACTIVE = ['starting', 'uploading', 'finishing'];
export const isUploading = (st) => !!st && UPLOAD_ACTIVE.includes(st.phase);

export const chunkCount = (size, chunkBytes) => Math.ceil(size / chunkBytes);

// 选文件时先挡掉的：空文件、超过提供方上限（EV-10 max_artifact_bytes）
export function checkArtifactFile(file, maxBytes) {
  if (!file) return null;
  if (!file.size) return 'empty';
  if (maxBytes && file.size > maxBytes) return 'too_large';
  return null;
}

// 网络断开、网关和服务端临时错误才值得自动重试；4xx 重试也一样
const retryable = (e) => !e?.status || e.status >= 500 || e.status === 408 || e.status === 429;
// 这几种错误时，已经传的块作废，重试要从头开一个新上传
const RESTART = new Set(['upload_not_found', 'upload_hash_mismatch']);

const readSlice = async (file, start, end) => new Uint8Array(await file.slice(start, end).arrayBuffer());
const sleepFor = (ms, signal) => new Promise((resolve, reject) => {
  const t = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(t); reject(new DOMException('aborted', 'AbortError')); }, { once: true });
});

/**
 * file 只用到 name、size，和 read(file, start, end) → Uint8Array。
 * onChange 收到 { phase, filename, size, sent, pct, error, artifact }。
 */
export function createArtifactUpload({
  api, file, onChange, retries = UPLOAD_RETRIES, read = readSlice, sleep = sleepFor, backoff = (n) => 800 * n,
}) {
  let st = { phase: 'idle', filename: file.name, size: file.size, sent: 0, pct: 0, error: null, artifact: null };
  let session = null;
  let next = 0;
  let hashed = -1;
  let hasher = null;
  let missing = null;
  let ctrl = null;

  const set = (patch) => {
    st = { ...st, ...patch };
    st.pct = st.size ? Math.min(100, Math.floor((st.sent / st.size) * 100)) : 0;
    onChange?.(st);
  };
  const chunkOf = (i) => [i * session.chunk_bytes, Math.min(file.size, (i + 1) * session.chunk_bytes)];

  async function put(i, bytes, hashIt) {
    for (let attempt = 0; ; attempt += 1) {
      const req = api.putEvidenceUploadChunk(session.upload_id, i, bytes, { signal: ctrl.signal });
      // 请求在途时算哈希；失败重试同一块时不再算
      if (hashIt && hashed < i) { hasher.update(bytes); hashed = i; }
      try {
        return await req;
      } catch (e) {
        if (e?.name === 'AbortError' || attempt >= retries || !retryable(e)) throw e;
        await sleep(backoff(attempt + 1), ctrl.signal);
      }
    }
  }

  async function run() {
    ctrl = new AbortController();
    try {
      if (!session) {
        set({ phase: 'starting', error: null, sent: 0 });
        session = await api.startEvidenceUpload({ filename: file.name, size: file.size });
        next = 0;
        hashed = -1;
        hasher = createSha256();
        missing = null;
      }
      set({ phase: 'uploading', error: null });
      // 完成时服务端说缺块：只补这几块（哈希早已算完）
      if (missing) {
        for (const i of missing) {
          const [s, e] = chunkOf(i);
          await put(i, await read(file, s, e), false);
        }
        missing = null;
      }
      const n = chunkCount(file.size, session.chunk_bytes);
      while (next < n) {
        const [s, e] = chunkOf(next);
        await put(next, await read(file, s, e), true);
        next += 1;
        set({ sent: e });
      }
      set({ phase: 'finishing' });
      const r = await api.completeEvidenceUpload(session.upload_id, { sha256: hasher.hex() });
      set({ phase: 'done', artifact: r.artifact });
    } catch (e) {
      if (e?.name === 'AbortError' || ctrl.signal.aborted) { set({ phase: 'cancelled' }); return; }
      if (e?.code === 'upload_incomplete' && Array.isArray(e.details?.missing)) missing = e.details.missing;
      if (RESTART.has(e?.code)) session = null;
      set({ phase: 'failed', error: e });
    }
  }

  return {
    get state() { return st; },
    start: () => (st.phase === 'idle' ? run() : Promise.resolve()),
    retry: () => (st.phase === 'failed' ? run() : Promise.resolve()),
    cancel() {
      if (!isUploading(st) && st.phase !== 'failed') return;
      if (isUploading(st)) ctrl?.abort();
      else set({ phase: 'cancelled' });
    },
  };
}
