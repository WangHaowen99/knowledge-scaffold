// 把传输层、客户端、接口函数、事件流组装成应用用的一组服务
import { API_PREFIX, createClient, createTokenStore } from '../api/client.js';
import { createApi } from '../api/endpoints.js';
import { errorFromResponse } from '../api/errors.js';
import { bindFileTransport } from '../api/files.js';
import { EventStream } from '../api/sse.js';
import { createTransport } from '../api/transport.js';

export async function createServices({ onAuthRequired, onMaintenance }) {
  const transport = await createTransport();
  const tokens = createTokenStore(window.localStorage);
  let opsBase = '';
  const client = createClient({
    fetchImpl: transport.fetch,
    tokens,
    opsBaseUrl: () => opsBase,
    onAuthRequired,
    onMaintenance,
  });
  const api = createApi(client);
  // 保留的预览 / 编辑组件（drawio、Excel、HTML）经 src/api/files.js 读写文件，也走这套传输层
  bindFileTransport({ client, fetch: transport.fetch, tokens });

  return {
    transport,
    tokens,
    client,
    api,
    setOpsBase(v) { opsBase = v || ''; },

    // U-01 带进度的上传
    async upload(file, purpose, { projectId, onProgress } = {}) {
      const form = new FormData();
      form.append('file', file);
      form.append('purpose', purpose);
      if (projectId) form.append('project_id', projectId);
      const headers = tokens.access ? { Authorization: `Bearer ${tokens.access}` } : {};
      const res = await transport.upload(`${API_PREFIX}/uploads`, form, headers, onProgress);
      if (!res.ok) throw await errorFromResponse(res);
      return res.json();
    },

    // 下载：生产用带 token 的直链（大文件不进内存），mock 下走 fetch 取 blob
    async download(path, fallbackName) {
      if (!transport.isMock) {
        const a = document.createElement('a');
        a.href = client.withToken(`${API_PREFIX}${path}`);
        a.rel = 'noopener';
        document.body.appendChild(a);
        a.click();
        a.remove();
        return;
      }
      const res = await client.request('GET', path, { raw: true });
      const blob = await res.blob();
      const cd = res.headers.get('Content-Disposition') || '';
      const m = cd.match(/filename="([^"]+)"/);
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = m ? decodeURIComponent(m[1]) : fallbackName;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    },

    // 开一条事件流（契约 1.11）
    openStream({ kind, url, lastId, onEvent, onReset, onStatus }) {
      const s = new EventStream({
        kind,
        url,
        lastId,
        fetchImpl: transport.fetch,
        getToken: () => tokens.access,
        refresh: () => client.refresh(),
        onEvent,
        onReset,
        onStatus,
        onAuthRequired,
      });
      s.start();
      return () => s.stop();
    },
  };
}
