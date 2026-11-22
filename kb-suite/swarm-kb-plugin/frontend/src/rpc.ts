// RPC client for the kb-manager plugin backend.
//
// The plugin's methods are registered on the host's web channel
// (`plugin.kb_manager.<method>`, see plugin/kb-manager/extension.py), so the
// page opens its own connection to the host's `/ws` and speaks the host's
// frame protocol: `{type: 'req', id, method, params}` out,
// `{type: 'res', id, ok, payload, error, code}` back. Event frames the host
// pushes on that connection are ignored.

export class RpcError extends Error {
  code: string;
  status: number;
  constructor(code: string, message: string, status = 400) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

type Pending = {
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
  timer: number;
};

type Listener = (connected: boolean) => void;

type ResFrame = {
  type?: string;
  id?: string;
  ok?: boolean;
  payload?: { result?: unknown; status?: number };
  error?: string;
  code?: string;
};

const METHOD_PREFIX = 'plugin.kb_manager.';
// the host page keeps its login session id here (same origin, so the iframe can read it)
const AUTH_SESSION_KEY = 'jiuwenswarm.auth.sessionId';

class RpcClient {
  private ws: WebSocket | null = null;
  private seq = 0;
  private pending = new Map<string, Pending>();
  private queue: string[] = [];
  private listeners = new Set<Listener>();
  private retry = 0;
  connected = false;

  constructor() {
    this.open();
  }

  private url(): string {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const query = new URLSearchParams();
    try {
      const session = window.localStorage.getItem(AUTH_SESSION_KEY);
      if (session) query.set('auth_session', session);
    } catch {
      // storage blocked: the host's cookie still identifies the browser
    }
    const qs = query.toString();
    return `${proto}://${window.location.host}/ws${qs ? `?${qs}` : ''}`;
  }

  private open(): void {
    const ws = new WebSocket(this.url());
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.setConnected(true);
      for (const msg of this.queue.splice(0)) ws.send(msg);
    };
    ws.onmessage = (event) => {
      let msg: ResFrame;
      try {
        msg = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (msg.type !== 'res' || !msg.id) return;
      const entry = this.pending.get(msg.id);
      if (!entry) return;
      this.pending.delete(msg.id);
      window.clearTimeout(entry.timer);
      if (msg.ok) entry.resolve(msg.payload?.result);
      else entry.reject(new RpcError(msg.code ?? 'error', msg.error ?? '请求失败', msg.payload?.status ?? 400));
    };
    ws.onclose = () => {
      this.setConnected(false);
      for (const [id, entry] of this.pending) {
        window.clearTimeout(entry.timer);
        entry.reject(new RpcError('disconnected', '与插件后端的连接已断开'));
        this.pending.delete(id);
      }
      const delay = Math.min(10000, 500 * 2 ** this.retry++);
      window.setTimeout(() => this.open(), delay);
    };
  }

  private setConnected(value: boolean): void {
    this.connected = value;
    for (const fn of this.listeners) fn(value);
  }

  onStatus(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  call<T = unknown>(method: string, params: Record<string, unknown> = {}, timeoutMs = 120000): Promise<T> {
    const id = `kbm_${Date.now().toString(36)}_${++this.seq}`;
    const payload = JSON.stringify({ type: 'req', id, method: `${METHOD_PREFIX}${method}`, params });
    return new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(id);
        reject(new RpcError('timeout', '请求超时'));
      }, timeoutMs);
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject, timer });
      if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(payload);
      else this.queue.push(payload);
    });
  }
}

export const rpc = new RpcClient();
