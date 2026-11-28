// 输入框与运行状态行的纯函数：右下角按钮是发送还是停止、运行中插的话送到哪了、跑了多久

// 有内容就发送（运行中发文字是插话、发命令是排在这一轮之后）；没内容时运行中是停止，空闲时灰掉
export function sendButtonMode({ canStop, hasContent, disabled }) {
  if (disabled) return 'off';
  if (hasContent) return 'send';
  return canStop ? 'stop' : 'off';
}

// 插话发出这么久还没被助手看到，就写明在等什么，并给「打断并发送」
export const STEER_WAIT_MS = 30000;

const clock = (iso) => {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

export function fmtWait(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分`;
}

// 运行状态行的计时：一小时内 m:ss，超过一小时 h:mm:ss
export function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/**
 * 用户气泡下面的送达说明（契约 message.delivery）。返回 null 表示不写。
 * stopAndSend：等得久了，给「打断并发送」
 */
export function deliveryNote(item, now = Date.now()) {
  switch (item.delivery) {
    case 'steer': {
      const d = item.delivery_state || { state: 'pending' };
      if (d.state === 'seen') return { text: d.at ? `助手已看到 ${clock(d.at)}` : '助手已看到' };
      if (d.state === 'returned') return { text: '已撤回' };
      if (d.state === 'new_turn') return null;
      const sent = item.ts ? new Date(item.ts).getTime() : NaN;
      const waited = now - sent;
      if (!(waited >= STEER_WAIT_MS)) return { text: '等助手看到' };
      const sub = d.waiting_on?.kind === 'subagent' ? d.waiting_on.title : '';
      return { text: `${sub ? `子任务「${sub}」结束后` : '这一步做完后'}助手才能看到 · 已等 ${fmtWait(waited)}`, stopAndSend: true };
    }
    case 'deferred': return { text: '下一步开始前带给助手' };
    case 'queued': return { text: '排队中' };
    default: return null;
  }
}

// 没发出去的字按对话各存一份（会话存储）：切对话、切模式、去待处理再回来都不丢，也不串到别的对话
const draftKey = (pid, cid) => `onto.wb.draft.${pid}.${cid}`;

export function loadDraft(storage, pid, cid) {
  if (!storage || !pid || !cid) return '';
  try { return storage.getItem(draftKey(pid, cid)) || ''; } catch { return ''; }
}

export function saveDraft(storage, pid, cid, text) {
  if (!storage || !pid || !cid) return;
  try {
    if (text) storage.setItem(draftKey(pid, cid), text);
    else storage.removeItem(draftKey(pid, cid));
  } catch { /* 存不下（隐私模式、满了）：只是少了跨对话保留 */ }
}

// 打断时撤回的话放回输入框：接在已经敲了的字后面，各占一行
export function restoreText(current, returned) {
  return [current, ...(returned || [])].filter((t) => t && t.trim()).join('\n');
}

// 提交中再点同一类入口（双击「提取对象」、连点建议条）：请求回来之前直接拦下，不发第二个请求、不画气泡。
// 服务端另有连续重复提交的 409 message_duplicate（这一轮没结束时同一条不受理）
export const SUBMITTING_TEXT = '消息正在提交，请勿重复操作';

export function createSubmitGuard() {
  let busy = false;
  return {
    busy: () => busy,
    async run(fn) {
      busy = true;
      try {
        return await fn();
      } finally {
        busy = false;
      }
    },
  };
}

// 本地先画气泡之前判断：这一轮还没停下、又发了和上一条相同的文字，服务端会按重复拒收，先不画，免得画了又删
export function likelyDuplicate(lastSent, { cid, text, sessionState }) {
  const trimmed = (text || '').trim();
  if (!trimmed || !lastSent || lastSent.cid !== cid || lastSent.text !== trimmed) return false;
  return ['running', 'connecting', 'queued'].includes(sessionState);
}
