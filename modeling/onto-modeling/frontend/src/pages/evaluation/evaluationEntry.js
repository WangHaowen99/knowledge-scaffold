// 打开独立评测页面评测一个固定的 Hub 版本（ADR-0092）：地址里只带不可变的 Hub 引用；
// 评测页面与建模页面不同源，登录经窗口消息交给它，从不放进地址

export const READY = 'onto-eval-ready';
export const HANDOFF = 'onto-eval-handoff';
const EVALUATION_PATH = '/evaluation/';
// 与评测部署的默认宿主端口一致；评测在其他主机、端口或经 HTTPS 时由 ONTO_WEB_EVAL_PUBLIC_URL 指定
const DEFAULT_PORT = 5180;
let cachedEntry = null;

export function defaultEvaluationBase(here) {
  return `http://${here.hostname}:${DEFAULT_PORT}${EVALUATION_PATH}`;
}

// runType 为 solver 时评测页面的新建向导预选「求解验证」
export function evaluationUrl(release, base, runType = '') {
  if (!release?.ontology_id || !release?.release_id) throw new Error('缺少 Hub 包版本引用');
  const query = new URLSearchParams({ ontology_id: release.ontology_id, release_id: release.release_id });
  if (release.package_digest) query.set('package_digest', release.package_digest);
  if (runType === 'solver') query.set('run_type', 'solver');
  return `${base}?${query}`;
}

// 先取好入口地址：点击时要在任何 await 之前打开窗口，否则会被浏览器拦截
export function prefetchEvaluationEntry(load) {
  return load().then((entry) => { cachedEntry = entry; return entry; });
}

export async function checkEvaluation(base, { fetchImpl = fetch } = {}) {
  let response;
  try { response = await fetchImpl(`${base}api/health`, { cache: 'no-store' }); }
  catch { throw new Error(`无法连接评测服务 ${base}，请检查独立评测地址和端口`); }
  if (!response.ok) throw new Error(`独立评测服务请求失败（HTTP ${response.status}），请检查 ONTO_WEB_EVAL_PUBLIC_URL`);
  const body = await response.json().catch(() => ({}));
  if (body.service !== 'evaluation') throw new Error('独立评测服务响应不匹配，请检查评测地址');
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
  loadEntry,
  fetchImpl = fetch,
  openWindow = () => window.open('about:blank', '_blank'),
  here = window.location,
  messages = window,
  tokens,
  handoffTimeoutMs = 120000,
  runType = '',
} = {}) {
  const base = (entry || await prefetchEvaluationEntry(loadEntry)).url || defaultEvaluationBase(here);
  let target;
  try { target = new URL(base); } catch { throw new Error('评测服务必须配置完整的独立 http(s) 地址'); }
  if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password || target.search || target.hash || target.pathname !== EVALUATION_PATH) {
    throw new Error('评测服务必须配置不含凭据、查询参数的完整 http(s) 地址，路径为 /evaluation/');
  }
  if (target.origin === here.origin) throw new Error('评测服务必须使用与建模页面不同的独立来源');
  if (here.protocol === 'https:' && target.protocol !== 'https:') throw new Error('HTTPS 建模页面必须配置 HTTPS 独立评测地址');
  const popup = openWindow();
  if (!popup) throw new Error('浏览器拦截了评测窗口，请允许本站弹出窗口后重试');
  try {
    await checkEvaluation(target.href, { fetchImpl });
  } catch (error) {
    popup.close();
    throw error;
  }
  handOver({ popup, origin: target.origin, messages, tokens, timeoutMs: handoffTimeoutMs });
  popup.location.href = evaluationUrl(release, target.href, runType);
}
