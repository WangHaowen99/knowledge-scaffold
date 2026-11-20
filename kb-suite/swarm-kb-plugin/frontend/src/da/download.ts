// A file DA pushed is fetched to the plugin host by the harness; the page reads it from the
// plugin in pieces over its socket and lets the browser save it.
import { channel, type Wire } from './channel';

const PIECE_TIMEOUT_MS = 30000;

export function decodeBase64(text: string): Uint8Array<ArrayBuffer> {
  const bin = atob(text);
  const out = new Uint8Array(new ArrayBuffer(bin.length));
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function readPiece(hostSessionId: string, name: string, offset: number): Promise<Wire> {
  const clientId = `file-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return new Promise((resolve, reject) => {
    const done = () => {
      window.clearTimeout(timer);
      off();
    };
    const timer = window.setTimeout(() => {
      done();
      reject(new Error('下载超时'));
    }, PIECE_TIMEOUT_MS);
    const off = channel.on((msg) => {
      if (msg.client_id !== clientId) return;
      done();
      if (msg.t === 'file_chunk') resolve(msg);
      else reject(new Error(String(msg.message ?? '下载失败')));
    });
    channel.send({ type: 'file', host_session_id: hostSessionId, name, offset, client_id: clientId });
  });
}

/** Reads the whole file, then hands it to the browser as a download. */
export async function downloadPushedFile(hostSessionId: string, name: string, mimeType?: string): Promise<void> {
  const parts: Uint8Array<ArrayBuffer>[] = [];
  for (let offset = 0; ; ) {
    const piece = await readPiece(hostSessionId, name, offset);
    const bytes = decodeBase64(String(piece.data ?? ''));
    parts.push(bytes);
    offset += bytes.length;
    if (piece.eof === true || bytes.length === 0) break;
  }
  const url = URL.createObjectURL(new Blob(parts, { type: mimeType || 'application/octet-stream' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10000);
}
