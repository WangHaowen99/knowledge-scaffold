/** Opens evaluation for a fixed Hub version. Navigation URLs carry only immutable Hub references;
 * a cross-origin evaluation page receives the login through window messages, never through the URL. */
import { api, authStore } from './api.js';

export const READY = 'onto-eval-ready';
export const HANDOFF = 'onto-eval-handoff';
const SAME_ORIGIN = '/evaluation/';
// 与评测部署的默认宿主端口一致；评测在其他主机、端口或经 HTTPS 时由 ONTO_WEB_EVAL_PUBLIC_URL 指定
const DEFAULT_PORT = 8090;
let cachedEntry = null;

export function defaultEvaluationBase(here) {
  return `http://${here.hostname}:${DEFAULT_PORT}${SAME_ORIGIN}`;
}

export function evaluationUrl(release, base = SAME_ORIGIN) {
  if (!release?.hub_package_id || !release?.release_id) throw new Error('缺少 Hub 包版本引用');
  const query = new URLSearchParams({ hub_package_id: release.hub_package_id, release_id: release.release_id });
  if (release.package_digest) query.set('package_digest', release.package_digest);
  return `${base}?${query}`;
}

// Dialogs prefetch the entry so the click can open the evaluation window before any await.
export function prefetchEvaluationEntry(load = api.getEvaluationEntry) {
  return load().then((entry) => { cachedEntry = entry; return entry; });
}

export async function checkEvaluation(base, { fetchImpl = fetch, crossOrigin = false } = {}) {
  let response;
  try { response = await fetchImpl(`${base}api/health`, { cache: 'no-store' }); }
  catch {
    throw new Error(crossOrigin
      ? `无法连接评测服务 ${base}，请检查评测端口是否对浏览器开放，或由部署方设置 ONTO_WEB_EVAL_PUBLIC_URL`
      : '无法连接评测服务，请检查服务是否启动及同源 /evaluation/ 代理配置');
  }
  if (!response.ok) {
    if (response.status === 404) {
      throw new Error(crossOrigin
        ? `评测地址 ${base} 不存在，请由部署方检查 ONTO_WEB_EVAL_PUBLIC_URL`
        : '评测路由未配置或不存在，请将同源 /evaluation/ 代理到独立评测服务');
    }
    throw new Error(`评测服务健康检查失败（HTTP ${response.status}），请检查独立评测服务及代理配置`);
  }
  const body = await response.json().catch(() => ({}));
  if (body.service !== 'evaluation') throw new Error('评测代理未配置或响应不匹配，请将 /evaluation/ 指向独立评测服务');
}

function handOver({ popup, origin, messages, tokens, timeoutMs }) {
  const stop = () => { clearTimeout(timer); messages.removeEventListener('message', onMessage); };
  const timer = setTimeout(stop, timeoutMs);
  function onMessage(event) {
    if (event.source !== popup || event.origin !== origin || event.data?.type !== READY) return;
    const { access_token: accessToken, refresh_token: refreshToken } = tokens();
    if (accessToken) popup.postMessage({ type: HANDOFF, access_token: accessToken, refresh_token: refreshToken }, origin);
    stop();
  }
  messages.addEventListener('message', onMessage);
}

export async function openEvaluation(release, {
  entry = cachedEntry,
  loadEntry = prefetchEvaluationEntry,
  fetchImpl = fetch,
  navigate = (url) => window.location.assign(url),
  openWindow = () => window.open('about:blank', '_blank'),
  here = window.location,
  messages = window,
  tokens = () => ({ access_token: authStore.token, refresh_token: authStore.refreshToken }),
  handoffTimeoutMs = 120000,
} = {}) {
  const base = (entry || await loadEntry()).url || defaultEvaluationBase(here);
  const target = new URL(base, here.href);
  if (target.origin === here.origin) {
    await checkEvaluation(base, { fetchImpl });
    navigate(evaluationUrl(release, base));
    return;
  }
  const popup = openWindow();
  if (!popup) throw new Error('浏览器拦截了评测窗口，请允许本站弹出窗口后重试');
  try {
    await checkEvaluation(target.href, { fetchImpl, crossOrigin: true });
  } catch (error) {
    popup.close();
    throw error;
  }
  handOver({ popup, origin: target.origin, messages, tokens, timeoutMs: handoffTimeoutMs });
  popup.location.href = evaluationUrl(release, target.href);
}
