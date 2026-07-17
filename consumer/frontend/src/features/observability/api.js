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
