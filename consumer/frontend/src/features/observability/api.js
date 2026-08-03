export async function requestObservabilityJson(request, url, options = {}, fallbackMessage = '请求观测数据失败') {
  const response = await request(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || payload.detail || `${fallbackMessage}（HTTP ${response.status}）`);
    error.status = response.status;
    error.code = payload.code || '';
    throw error;
  }
  return payload;
}

export async function requestObservabilityDownload(
  request,
  url,
  options = {},
  fallbackMessage = 'Speedscope download failed',
) {
  const response = await request(url, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const error = new Error(payload.error || payload.detail || `${fallbackMessage} (HTTP ${response.status})`);
    error.status = response.status;
    error.code = payload.code || '';
    throw error;
  }
  const contentDisposition = response.headers?.get?.('content-disposition') || '';
  const match = contentDisposition.match(/filename="?([^";]+)"?/i);
  return {
    blob: await response.blob(),
    fileName: match?.[1] || '',
  };
}
