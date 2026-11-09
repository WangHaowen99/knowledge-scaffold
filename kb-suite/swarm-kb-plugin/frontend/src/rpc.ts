// WebSocket RPC client for the kb-manager plugin backend.

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

const WS_PATH = '/ws/plugins/kb-manager';

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
    return `${proto}://${window.location.host}${WS_PATH}`;
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
      let msg: { id?: string; ok?: boolean; result?: unknown; error?: { code: string; message: string; status: number } };
      try {
        msg = JSON.parse(String(event.data));
      } catch {
        return;
      }
      const entry = msg.id ? this.pending.get(msg.id) : undefined;
      if (!entry || !msg.id) return;
      this.pending.delete(msg.id);
      window.clearTimeout(entry.timer);
      if (msg.ok) entry.resolve(msg.result);
      else entry.reject(new RpcError(msg.error?.code ?? 'error', msg.error?.message ?? '请求失败', msg.error?.status));
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
    const id = `r${++this.seq}`;
    const payload = JSON.stringify({ id, method, params });
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
