// SSE 事件流（契约 1.11）：fetch 流式读取，带令牌头、按事件 id 回放、心跳超时重连
// 对话流的 id 是整数 seq，重连带 after=<seq>；项目流、用户流的 id 是 "<epoch>.<n>"，
// 重连带 last_event_id，断档时服务端先发 stream.reset，前端按 resync 重拉快照。

export const HEARTBEAT_TIMEOUT_MS = 45000;
const DEFAULT_RETRY_MS = 3000;
const MAX_RETRY_MS = 30000;

// 增量解析 text/event-stream：push 一段文本，返回解析出的完整帧
export function createSSEParser() {
  let buffer = '';
  let data = [];
  let id = null;
  let retry = null;
  const flushFrame = (out) => {
    if (data.length) out.push({ id, data: data.join('\n') });
    data = [];
    id = null;
  };
  return {
    get retry() { return retry; },
    push(text) {
      buffer += text;
      const out = [];
      let idx;
      // 行尾可以是 \n、\r\n 或 \r
      while ((idx = buffer.search(/\r\n|\r|\n/)) >= 0) {
        const line = buffer.slice(0, idx);
        const sep = buffer[idx] === '\r' && buffer[idx + 1] === '\n' ? 2 : 1;
        if (buffer[idx] === '\r' && idx + 1 === buffer.length) break; // 等下一段确认是否 \r\n
        buffer = buffer.slice(idx + sep);
        if (line === '') { flushFrame(out); continue; }
        if (line.startsWith(':')) continue;
        const colon = line.indexOf(':');
        const field = colon < 0 ? line : line.slice(0, colon);
        let value = colon < 0 ? '' : line.slice(colon + 1);
        if (value.startsWith(' ')) value = value.slice(1);
        if (field === 'data') data.push(value);
        else if (field === 'id') id = value;
        else if (field === 'retry' && /^\d+$/.test(value)) retry = Number(value);
      }
      return out;
    },
  };
}

// 断线重连时带的查询参数
export function resumeQuery(kind, lastId) {
  if (lastId == null || lastId === '') return {};
  return kind === 'conversation' ? { after: lastId } : { last_event_id: lastId };
}

export function backoffDelay(attempt, retryMs = DEFAULT_RETRY_MS) {
  if (attempt <= 0) return 0;
  return Math.min(MAX_RETRY_MS, retryMs * 2 ** (attempt - 1));
}

/**
 * 一条事件流连接。
 * @param {object} o
 * @param {'conversation'|'project'|'user'|'logs'} o.kind
 * @param {(query: object) => string} o.url 生成请求地址（含 resume 查询参数）
 * @param {Function} o.fetchImpl
 * @param {() => string} o.getToken
 * @param {() => Promise<boolean>} o.refresh 刷新令牌
 * @param {(env: object) => void} o.onEvent 收到业务事件（已去掉 ping、按 seq 去重）
 * @param {(data: object) => void} [o.onReset] 收到 stream.reset
 * @param {(status: string) => void} [o.onStatus] connecting|open|reconnecting|closed|failed
 * @param {(err: object) => void} [o.onAuthRequired]
 * @param {string|number|null} [o.lastId] 从哪条之后开始（对话流先拉历史再接流）
 */
export class EventStream {
  constructor(o) {
    this.o = o;
    this.kind = o.kind;
    this.lastId = o.lastId ?? null;
    this.attempt = 0;
    this.retryMs = DEFAULT_RETRY_MS;
    this.stopped = true;
    this.timers = o.timers || globalThis;
    this.heartbeatMs = o.heartbeatMs || HEARTBEAT_TIMEOUT_MS;
    this.ctrl = null;
    this.watchdog = null;
    this.retryTimer = null;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  stop() {
    this.stopped = true;
    this.clearTimers();
    this.ctrl?.abort();
    this.ctrl = null;
    this.status('closed');
  }

  status(s) { this.o.onStatus?.(s); }

  clearTimers() {
    if (this.watchdog) this.timers.clearTimeout(this.watchdog);
    if (this.retryTimer) this.timers.clearTimeout(this.retryTimer);
    this.watchdog = null;
    this.retryTimer = null;
  }

  armWatchdog() {
    if (this.watchdog) this.timers.clearTimeout(this.watchdog);
    this.watchdog = this.timers.setTimeout(() => {
      // 45 秒没有任何帧：判定断线
      this.ctrl?.abort();
      this.scheduleReconnect();
    }, this.heartbeatMs);
  }

  scheduleReconnect(immediate = false) {
    if (this.stopped) return;
    this.clearTimers();
    this.attempt += 1;
    this.status('reconnecting');
    const delay = immediate ? 0 : backoffDelay(this.attempt, this.retryMs);
    this.retryTimer = this.timers.setTimeout(() => this.connect(), delay);
  }

  async connect() {
    if (this.stopped) return;
    const ctrl = new AbortController();
    this.ctrl?.abort();
    this.ctrl = ctrl;
    if (this.attempt === 0) this.status('connecting');
    const headers = { Accept: 'text/event-stream' };
    const token = this.o.getToken?.();
    if (token) headers.Authorization = `Bearer ${token}`;
    if (this.lastId != null && this.kind !== 'conversation') headers['Last-Event-ID'] = String(this.lastId);
    let res;
    try {
      res = await this.o.fetchImpl(this.o.url(resumeQuery(this.kind, this.lastId)), { headers, signal: ctrl.signal });
    } catch {
      if (ctrl === this.ctrl) this.scheduleReconnect();
      return;
    }
    if (ctrl !== this.ctrl || this.stopped) return;
    if (!res.ok) {
      await this.handleOpenError(res);
      return;
    }
    this.attempt = 0;
    this.status('open');
    this.armWatchdog();
    const parser = createSSEParser();
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        if (ctrl !== this.ctrl) return;
        this.armWatchdog();
        const frames = parser.push(decoder.decode(value, { stream: true }));
        if (parser.retry) this.retryMs = parser.retry;
        for (const f of frames) {
          if (this.handleFrame(f) === 'reconnect') {
            ctrl.abort();
            this.scheduleReconnect(true);
            return;
          }
        }
      }
    } catch {
      if (ctrl !== this.ctrl) return;
    }
    if (ctrl === this.ctrl && !this.stopped) this.scheduleReconnect();
  }

  async handleOpenError(res) {
    if (res.status === 401) {
      const ok = await this.o.refresh?.();
      if (ok) { this.scheduleReconnect(true); return; }
      this.stopped = true;
      this.clearTimers();
      this.status('failed');
      this.o.onAuthRequired?.({ status: 401 });
      return;
    }
    if (res.status === 403 || res.status === 404) {
      this.stopped = true;
      this.clearTimers();
      this.status('failed');
      return;
    }
    this.scheduleReconnect();
  }

  handleFrame(frame) {
    let env;
    try {
      env = JSON.parse(frame.data);
    } catch {
      return null;
    }
    if (!env || typeof env !== 'object') return null;
    const id = frame.id ?? env.id ?? null;
    if (env.type === 'ping') return null;
    if (env.type === 'auth.expired') {
      this.o.refresh?.().then((ok) => {
        if (!ok) this.o.onAuthRequired?.({ status: 401 });
      });
      return 'reconnect';
    }
    if (env.type === 'stream.reset') {
      this.o.onReset?.(env.data || {});
      return null;
    }
    if (id != null && id !== '') {
      if (this.kind === 'conversation') {
        const seq = Number(id);
        if (Number.isFinite(seq)) {
          if (this.lastId != null && seq <= Number(this.lastId)) return null; // 回放重复
          this.lastId = seq;
        }
      } else {
        this.lastId = id;
      }
    }
    this.o.onEvent(env);
    return null;
  }
}
