// 传输层：生产走浏览器 fetch；开发 / 测试构建在 VITE_API_MOCK=1 时换成 mock 服务端。
// __API_MOCK__ 由 vite.config.js 按构建模式注入，生产构建恒为 false，整段 mock 被摇掉、不打进包里。

export async function createTransport() {
  // eslint-disable-next-line no-undef
  if (__API_MOCK__) {
    const { createMockServer } = await import('./mock/index.js');
    const server = createMockServer();
    window.__ontoMock = server;
    return {
      isMock: true,
      fetch: server.fetch,
      upload: (url, form, headers, onProgress) => simulatedUpload(server.fetch, url, form, headers, onProgress),
    };
  }
  return {
    isMock: false,
    fetch: (url, init) => window.fetch(url, init),
    upload: xhrUpload,
  };
}

async function simulatedUpload(fetchImpl, url, form, headers, onProgress) {
  for (let p = 10; p < 100; p += 18) {
    onProgress?.(p);
    await new Promise((r) => setTimeout(r, 90));
  }
  const res = await fetchImpl(url, { method: 'POST', headers, body: form });
  onProgress?.(100);
  return res;
}

// 上传要进度条，fetch 拿不到上传进度，改用 XHR
function xhrUpload(url, form, headers, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    Object.entries(headers || {}).forEach(([k, v]) => xhr.setRequestHeader(k, v));
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress?.(Math.round((e.loaded / e.total) * 100)); };
    xhr.onload = () => resolve(new Response(xhr.responseText || null, {
      status: xhr.status, headers: { 'Content-Type': xhr.getResponseHeader('Content-Type') || 'application/json' },
    }));
    xhr.onerror = () => reject(new TypeError('network error'));
    xhr.send(form);
  });
}
