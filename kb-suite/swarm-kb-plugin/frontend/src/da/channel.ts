// Event channel to the da-analyst plugin backend (server pushes harness events).

export type Wire = { t: string; [key: string]: unknown };
type Listener = (msg: Wire) => void;
type StatusListener = (connected: boolean) => void;

const WS_PATH = '/ws/plugins/da-analyst';

export class DaChannel {
  private ws: WebSocket | null = null;
  private queue: string[] = [];
  private listeners = new Set<Listener>();
  private statusListeners = new Set<StatusListener>();
  private retry = 0;
  connected = false;

  constructor() {
    this.open();
  }

  private open(): void {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${window.location.host}${WS_PATH}`);
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.setConnected(true);
      for (const msg of this.queue.splice(0)) ws.send(msg);
    };
    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(String(event.data)) as Wire;
        for (const fn of this.listeners) fn(msg);
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      this.setConnected(false);
      window.setTimeout(() => this.open(), Math.min(10000, 500 * 2 ** this.retry++));
    };
  }

  private setConnected(value: boolean): void {
    this.connected = value;
    for (const fn of this.statusListeners) fn(value);
  }

  on(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  onStatus(fn: StatusListener): () => void {
    this.statusListeners.add(fn);
    return () => this.statusListeners.delete(fn);
  }

  send(msg: Record<string, unknown>): void {
    const text = JSON.stringify(msg);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(text);
    else this.queue.push(text);
  }
}

export const channel = new DaChannel();
