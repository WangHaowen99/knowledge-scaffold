/** Same-origin navigation carries immutable Hub references, never login material. */
export function evaluationUrl(release) {
  if (!release?.hub_package_id || !release?.release_id) throw new Error('缺少 Hub 包版本引用');
  const query = new URLSearchParams({ hub_package_id: release.hub_package_id, release_id: release.release_id });
  if (release.package_digest) query.set('package_digest', release.package_digest);
  return `/evaluation/?${query}`;
}

export async function openEvaluation(release, { fetchImpl = fetch, navigate = (url) => window.location.assign(url) } = {}) {
  const url = evaluationUrl(release);
  let response;
  try { response = await fetchImpl('/evaluation/api/health', { cache: 'no-store' }); }
  catch { throw new Error('无法连接评测服务，请检查服务是否启动及同源 /evaluation/ 代理配置'); }
  if (!response.ok) {
    if (response.status === 404) {
      throw new Error('评测路由未配置或不存在，请将同源 /evaluation/ 代理到独立评测服务');
    }
    throw new Error(`评测服务健康检查失败（HTTP ${response.status}），请检查独立评测服务及代理配置`);
  }
  const body = await response.json().catch(() => ({}));
  if (body.service !== 'evaluation') throw new Error('评测代理未配置或响应不匹配，请将同源 /evaluation/ 指向独立评测服务');
  navigate(url);
}
