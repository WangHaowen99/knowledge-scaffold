// 本体构建进度提醒：一轮跑完、agent 提问或请求确认、构建出错时，用户不在页面上就弹系统通知、
// 标签页标题与图标按提醒色闪烁，把用户叫回来。只覆盖当前标签页正在显示的会话。
// 判断逻辑（createBuildAlertTracker / planAlert / notificationAvailability / flashFrame）不碰 DOM：
// 便于单测，后续给切走的会话做后台订阅时也可直接复用。

// 服务重启遗留的 SIGTERM 报错，与 ChatPane 的展示过滤一致
const RESTART_EXIT = /exit code[:\s]*143/;
const ROUND_STATES = new Set(['running', 'queued', 'connecting']);

// rank 越大越优先：标题只显示优先级最高的提醒，同级的新提醒替换旧的。
// 标签页底色网页改不了，颜色靠标签图标（color）与标题前的彩色圆点（dot）体现
export const ALERTS = {
  done: { rank: 1, label: '【已完成】', dot: '🟢', color: '#2f9e44', body: '本体构建已完成这一步，可以继续对话' },
  question: { rank: 2, label: '【待确认】', dot: '🟠', color: '#f08c00', body: '需要你回答问题' },
  permission: { rank: 2, label: '【待确认】', dot: '🟠', color: '#f08c00', body: '需要你确认操作' },
  error: { rank: 2, label: '【出错】', dot: '🔴', color: '#e03131', body: '构建出错，请查看' },
};

// 闪烁的一帧：提醒态（marked）显示圆点 + 前缀标题与变色图标，原样态还原标题、图标（favicon 为 null）
export function flashFrame(kind, marked, baseTitle) {
  const alert = ALERTS[kind];
  if (!marked) return { title: baseTitle, favicon: null };
  return { title: `${alert.dot}${alert.label}${baseTitle}`, favicon: alertFaviconHref(alert.color) };
}

// 与 public/favicon.svg 同形的标签图标，仅底色换成提醒色
export function alertFaviconHref(color) {
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">' +
    `<rect width="64" height="64" rx="14" fill="${color}"/>` +
    '<text x="32" y="44" font-family="PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif" ' +
    'font-size="36" font-weight="700" fill="#ffffff" text-anchor="middle">知</text></svg>';
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

// 引擎把这一轮明确失败（如模型调用失败）写在 result 里，不另发 session_error；与后端 claude._result_failed 一致
function isFailedResult(event) {
  return Boolean(event.is_error) || String(event.subtype || '').startsWith('error');
}

// 逐个喂入会话事件（含回放的历史事件，用来还原会话状态），
// 返回应发的提醒类型（ALERTS 的键）或 null。历史事件只更新状态，不提醒。
export function createBuildAlertTracker() {
  let state = null;
  let interrupted = false;
  let failed = false;
  const requests = new Set();
  const endRound = () => {
    interrupted = false;
    failed = false;
  };
  return {
    // 打开会话时服务端报告的当前状态：历史被截断、看不到状态事件时兜底
    seed(initialState) {
      if (state === null) state = initialState || null;
    },
    next(event, { history = false } = {}) {
      switch (event.type) {
        case 'session_interrupted':
          interrupted = true;
          return null;
        case 'result':
          // 以本轮最后一个 result 为准
          failed = isFailedResult(event);
          return null;
        case 'permission_request': {
          const fresh = !requests.has(event.request_id);
          requests.add(event.request_id);
          if (history || !fresh) return null;
          return event.tool_name === 'AskUserQuestion' ? 'question' : 'permission';
        }
        case 'session_error':
          endRound();
          if (history || RESTART_EXIT.test(event.error || '')) return null;
          return 'error';
        case 'session_state': {
          const previous = state;
          state = event.state;
          if (ROUND_STATES.has(state)) return null;
          const wasInterrupted = interrupted;
          const wasFailed = failed;
          endRound();
          // 只有真正跑过（running → idle）才算结束：排队中取消也会直接回到 idle
          if (history || state !== 'idle' || previous !== 'running' || wasInterrupted) return null;
          return wasFailed ? 'error' : 'done';
        }
        default:
          return null;
      }
    },
  };
}

const AVAILABILITY_TEXT = {
  insecure:
    '当前为 HTTP 访问，无法弹出系统通知，只有标签页标题和图标闪烁。如需系统通知，请联系管理员改用 HTTPS 访问',
  unsupported: '当前浏览器不支持系统通知',
  denied: '浏览器已禁止本站通知，需要在浏览器设置里开启',
  default: '首次发起构建时会请求通知权限',
  granted: '',
};

// 系统通知能否弹出：按 安全上下文 → 浏览器支持 → 权限状态 的顺序判断，命中第一条即返回
export function notificationAvailability({ secure, supported, permission }) {
  let code = 'granted';
  if (!secure) code = 'insecure';
  else if (!supported) code = 'unsupported';
  else if (permission === 'denied') code = 'denied';
  else if (permission !== 'granted') code = 'default';
  return { code, text: AVAILABILITY_TEXT[code] };
}

// 用户在页面上时什么都不做；不在时标题闪烁，开关开着且已授权才弹系统通知
export function planAlert({ present, enabled, availability }) {
  if (present) return { flash: false, notify: false };
  return { flash: true, notify: Boolean(enabled) && availability.code === 'granted' };
}

// ---- 浏览器侧：开关、权限、标题闪烁、系统通知 ----

const ENABLED_KEY = 'onto-desktop-notify';
const ASKED_KEY = 'onto-desktop-notify-asked';
const FLASH_INTERVAL_MS = 500;

export function isDesktopNotifyEnabled() {
  return localStorage.getItem(ENABLED_KEY) !== 'off';
}

export function setDesktopNotifyEnabled(enabled) {
  localStorage.setItem(ENABLED_KEY, enabled ? 'on' : 'off');
}

export function currentAvailability() {
  const supported = typeof Notification !== 'undefined';
  return notificationAvailability({
    secure: window.isSecureContext,
    supported,
    permission: supported ? Notification.permission : undefined,
  });
}

// 须在用户操作（点击、发送）中调用，浏览器才会弹出申请框
export async function requestNotificationPermission() {
  if (currentAvailability().code !== 'default') return currentAvailability();
  localStorage.setItem(ASKED_KEY, '1');
  try {
    await Notification.requestPermission();
  } catch {
    /* 申请失败按未授权处理 */
  }
  return currentAvailability();
}

// 首次发起构建时自动申请一次；用户关掉申请框或拒绝后不再自动申请，可在设置页手动申请
export function requestPermissionOnFirstBuild() {
  if (!isDesktopNotifyEnabled() || localStorage.getItem(ASKED_KEY)) return;
  requestNotificationPermission();
}

// 页面可见且窗口有焦点才算"人在页面上"：窗口没被完全遮挡、但用户在别的程序里忙时也要提醒
function userPresent() {
  return document.visibilityState === 'visible' && document.hasFocus();
}

let flash = null; // { kind, baseTitle, baseIcon, ticker }
const shown = new Map(); // tag → Notification，同一会话只留最新一条
let listening = false;

// 后台标签页主线程定时器会被浏览器节流（≥1 秒，隐藏久了可能 1 分钟一次）；
// Worker 里的定时器受限更少，用它驱动闪烁，创建失败时退回主线程定时器
function startTicker(intervalMs, onTick) {
  try {
    const source = `setInterval(() => postMessage(0), ${intervalMs});`;
    const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
    const worker = new Worker(url);
    URL.revokeObjectURL(url);
    worker.onmessage = onTick;
    return () => worker.terminate();
  } catch {
    const timer = setInterval(onTick, intervalMs);
    return () => clearInterval(timer);
  }
}

function iconLink() {
  return document.querySelector('link[rel~="icon"]');
}

function renderFrame(frame) {
  document.title = frame.title;
  const link = iconLink();
  if (link) link.href = frame.favicon ?? flash.baseIcon;
}

function stopFlash() {
  if (!flash) return;
  flash.ticker();
  renderFrame({ title: flash.baseTitle, favicon: null });
  flash = null;
}

function onPresenceChange() {
  if (!userPresent()) return;
  stopFlash();
  // 人回来了，通知中心里的旧提醒已无意义
  for (const notification of shown.values()) notification.close();
  shown.clear();
}

function listenPresence() {
  if (listening) return;
  listening = true;
  document.addEventListener('visibilitychange', onPresenceChange);
  window.addEventListener('focus', onPresenceChange);
}

function startFlash(kind) {
  if (flash && ALERTS[flash.kind].rank > ALERTS[kind].rank) return;
  const baseTitle = flash?.baseTitle ?? document.title;
  const baseIcon = flash?.baseIcon ?? iconLink()?.getAttribute('href');
  flash?.ticker();
  let marked = true;
  flash = { kind, baseTitle, baseIcon, ticker: () => {} };
  renderFrame(flashFrame(kind, marked, baseTitle));
  flash.ticker = startTicker(FLASH_INTERVAL_MS, () => {
    marked = !marked;
    renderFrame(flashFrame(kind, marked, baseTitle));
  });
}

function showNotification(kind, { projectName, tag }) {
  // 先关掉同一会话的上一条再弹：同 tag 直接替换时系统不会再次弹出
  shown.get(tag)?.close();
  try {
    const notification = new Notification(
      projectName ? `${projectName} · 本体构建` : '本体构建',
      { body: ALERTS[kind].body, tag, silent: true },
    );
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
    notification.onclose = () => {
      if (shown.get(tag) === notification) shown.delete(tag);
    };
    shown.set(tag, notification);
  } catch {
    /* 部分浏览器只允许经 Service Worker 弹通知：退化为只闪标题 */
  }
}

// kind 取 ALERTS 的键；tag 用会话 ID，同一会话的通知（含多个标签页）合并为一条
export function notifyBuild(kind, { projectName, tag }) {
  listenPresence();
  const plan = planAlert({
    present: userPresent(),
    enabled: isDesktopNotifyEnabled(),
    availability: currentAvailability(),
  });
  if (plan.flash) startFlash(kind);
  if (plan.notify) showNotification(kind, { projectName, tag });
}
