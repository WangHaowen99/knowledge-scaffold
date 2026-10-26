// 本体构建进度提醒：一轮跑完、agent 提问或请求确认、构建出错时，用户不在页面上就弹系统通知、
// 标签页标题闪烁，把用户叫回来。只覆盖当前标签页正在显示的会话。
// 判断逻辑（createBuildAlertTracker / planAlert / notificationAvailability）不碰 DOM：
// 便于单测，后续给切走的会话做后台订阅时也可直接复用。

// 本轮耗时不足该秒数的不提醒"已完成"，避免短问答也弹窗
export const MIN_ROUND_SECONDS = 60;
// 服务重启遗留的 SIGTERM 报错，与 ChatPane 的展示过滤一致
const RESTART_EXIT = /exit code[:\s]*143/;
const ROUND_STATES = new Set(['running', 'queued', 'connecting']);

// rank 越大越优先：标题只显示优先级最高的提醒，同级的新提醒替换旧的
export const ALERTS = {
  done: { rank: 1, label: '【已完成】', body: '本体构建已完成这一步，可以继续对话' },
  question: { rank: 2, label: '【待确认】', body: '需要你回答问题' },
  permission: { rank: 2, label: '【待确认】', body: '需要你确认操作' },
  error: { rank: 2, label: '【出错】', body: '构建出错，请查看' },
};

// 引擎把这一轮明确失败（如模型调用失败）写在 result 里，不另发 session_error；与后端 claude._result_failed 一致
function isFailedResult(event) {
  return Boolean(event.is_error) || String(event.subtype || '').startsWith('error');
}

// 逐个喂入会话事件（含回放的历史事件，用来还原本轮开始时间等状态），
// 返回应发的提醒类型（ALERTS 的键）或 null。历史事件只更新状态，不提醒。
export function createBuildAlertTracker({ minRoundSeconds = MIN_ROUND_SECONDS } = {}) {
  let state = null;
  let roundStart = null;
  let interrupted = false;
  let failed = false;
  const requests = new Set();
  const endRound = () => {
    roundStart = null;
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
        case 'user_message':
          // 运行中追加的消息不算新一轮
          if (roundStart === null) roundStart = event.ts ?? null;
          return null;
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
          if (ROUND_STATES.has(state)) {
            if (roundStart === null) roundStart = event.ts ?? null;
            return null;
          }
          const start = roundStart;
          const wasInterrupted = interrupted;
          const wasFailed = failed;
          endRound();
          // 只有真正跑过（running → idle）才算结束：排队中取消也会直接回到 idle
          if (history || state !== 'idle' || previous !== 'running' || wasInterrupted) return null;
          // 出错是高优先级，不论耗时长短都提醒
          if (wasFailed) return 'error';
          // 开始时间不可知（历史被截断）时，本轮必然已跑了很久，照常提醒
          if (start !== null && event.ts != null && event.ts - start < minRoundSeconds) return null;
          return 'done';
        }
        default:
          return null;
      }
    },
  };
}

const AVAILABILITY_TEXT = {
  insecure:
    '当前为 HTTP 访问，无法弹出系统通知，只有标签页标题闪烁。如需系统通知，请联系管理员改用 HTTPS 访问',
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
const FLASH_INTERVAL_MS = 1000;

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

let flash = null; // { kind, baseTitle, timer }
const shown = new Map(); // tag → Notification，同一会话只留最新一条
let listening = false;

function onPresenceChange() {
  if (!userPresent()) return;
  if (flash) {
    clearInterval(flash.timer);
    document.title = flash.baseTitle;
    flash = null;
  }
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
  const alert = ALERTS[kind];
  if (flash && ALERTS[flash.kind].rank > alert.rank) return;
  const baseTitle = flash?.baseTitle ?? document.title;
  if (flash) clearInterval(flash.timer);
  let marked = true;
  document.title = alert.label + baseTitle;
  // 后台标签页的定时器会被浏览器降频，闪烁可能变慢，但标题前缀仍会出现
  const timer = setInterval(() => {
    marked = !marked;
    document.title = marked ? alert.label + baseTitle : baseTitle;
  }, FLASH_INTERVAL_MS);
  flash = { kind, baseTitle, timer };
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
