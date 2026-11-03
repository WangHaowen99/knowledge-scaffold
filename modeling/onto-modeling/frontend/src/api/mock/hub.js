// mock 的 SSE 推送：每条流一个缓冲，打开时按 after / last_event_id 回放，再接实时
const enc = new TextEncoder();

export class StreamHub {
  constructor() {
    this.channels = new Map();
  }

  channel(key, kind) {
    if (!this.channels.has(key)) {
      this.channels.set(key, { kind, buffer: [], subs: new Set(), n: 0, epoch: 'm1' });
    }
    return this.channels.get(key);
  }

  // 持久事件：写进缓冲并推给所有订阅者；transient=true 时不带 id、不进缓冲
  emit(key, kind, env, { transient = false, seq } = {}) {
    const ch = this.channel(key, kind);
    let full = { ts: new Date().toISOString(), ...env };
    if (!transient) {
      ch.n = seq != null ? Math.max(ch.n, seq) : ch.n + 1;
      const id = kind === 'conversation' ? String(seq ?? ch.n) : `${ch.epoch}.${ch.n}`;
      full = { ...full, id };
      ch.buffer.push(full);
      if (ch.buffer.length > 2000) ch.buffer.shift();
    }
    const frame = `${full.id ? `id: ${full.id}\n` : ''}data: ${JSON.stringify(full)}\n\n`;
    ch.subs.forEach((ctrl) => {
      try { ctrl.enqueue(enc.encode(frame)); } catch { ch.subs.delete(ctrl); }
    });
    return full;
  }

  // 预置历史（对话流的 seq 由调用方给）
  seed(key, kind, events) {
    const ch = this.channel(key, kind);
    events.forEach((e) => {
      ch.buffer.push(e);
      const n = Number(e.id);
      if (Number.isFinite(n)) ch.n = Math.max(ch.n, n);
    });
  }

  history(key) {
    return this.channels.get(key)?.buffer || [];
  }

  open(key, kind, query) {
    const ch = this.channel(key, kind);
    let ctrlRef;
    let ping;
    const body = new ReadableStream({
      start(ctrl) {
        ctrlRef = ctrl;
        ctrl.enqueue(enc.encode('retry: 3000\n\n'));
        let replay = [];
        if (kind === 'conversation') {
          const after = Number(query.get('after') || 0);
          replay = ch.buffer.filter((e) => Number(e.id) > after);
        } else {
          const last = query.get('last_event_id');
          if (last) {
            const i = ch.buffer.findIndex((e) => e.id === last);
            if (i < 0 || !last.startsWith(`${ch.epoch}.`)) {
              ctrl.enqueue(enc.encode(`data: ${JSON.stringify({ type: 'stream.reset', data: { reason: 'gap', resync: ['FL-01', 'IN-01', 'CS-06', 'PL-12'] } })}\n\n`));
            } else replay = ch.buffer.slice(i + 1);
          }
        }
        replay.forEach((e) => ctrl.enqueue(enc.encode(`id: ${e.id}\ndata: ${JSON.stringify(e)}\n\n`)));
        ch.subs.add(ctrl);
        ping = setInterval(() => {
          try { ctrl.enqueue(enc.encode(`data: ${JSON.stringify({ type: 'ping', data: {} })}\n\n`)); } catch { clearInterval(ping); }
        }, 15000);
      },
      cancel() {
        clearInterval(ping);
        ch.subs.delete(ctrlRef);
      },
    });
    return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }
}
