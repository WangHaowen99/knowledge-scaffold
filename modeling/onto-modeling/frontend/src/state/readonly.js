// 写入范围只读判定（详设 4.3、契约 1.7 / 7.6 ScopeLock）
// 助手写入范围内的条目在确认生效或撤回前只读；两态：助手运行中 / 修改待确认；
// 自主构建下持续到这一片自动合入。范围外照常编辑。

export const LOCK_LABEL = {
  assistant_running: '助手运行中',
  pending_confirm: '修改待确认',
  autobuild_slice: '自主构建中',
};

export function lockState(lock) {
  return lock && LOCK_LABEL[lock.state] ? lock.state : 'none';
}

// 简单 glob：** 跨目录，* 不跨目录，? 单个字符
export function globToRegExp(glob) {
  let re = '';
  for (let i = 0; i < glob.length; i += 1) {
    const ch = glob[i];
    if (ch === '*') {
      if (glob[i + 1] === '*') { re += '.*'; i += 1; if (glob[i + 1] === '/') i += 1; } else re += '[^/]*';
    } else if (ch === '?') re += '[^/]';
    else re += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${re}$`);
}

/**
 * target 可以是 EntryRef（{id}）、ArtifactRef（{type, key}）、{path} 或 "<kind>:<key>" 字符串
 */
export function isLocked(lock, target) {
  if (lockState(lock) === 'none' || !target) return false;
  const t = typeof target === 'string' ? (target.includes('/') && !target.includes(':') ? { path: target } : { id: target }) : target;
  if (t.id && (lock.entries || []).some((e) => e.id === t.id)) return true;
  if (t.type && t.key && (lock.artifacts || []).some((a) => a.type === t.type && a.key === t.key)) return true;
  if (t.path && (lock.paths || []).some((p) => globToRegExp(p).test(t.path))) return true;
  return false;
}

/**
 * 编辑器、卡片要显示的只读说明；不在范围内返回 null。
 * actions：interrupt = 回到对话里打断它；confirm = 去确认；run = 查看自主构建运行
 */
export function readonlyInfo(lock, target) {
  if (target !== undefined && !isLocked(lock, target)) return null;
  const st = lockState(lock);
  if (st === 'none') return null;
  if (st === 'assistant_running') {
    return {
      state: st,
      label: LOCK_LABEL[st],
      text: lock.reason_text || '助手正在改这部分内容，确认生效或撤回后才能编辑',
      actions: [{ kind: 'interrupt', label: '回到对话里打断它' }],
    };
  }
  if (st === 'pending_confirm') {
    return {
      state: st,
      label: LOCK_LABEL[st],
      text: '助手的修改还没确认，确认生效或撤回后才能编辑',
      actions: [{ kind: 'confirm', label: '去确认' }],
    };
  }
  return {
    state: st,
    label: LOCK_LABEL[st],
    text: lock.slice ? `自主构建正在做「${lock.slice.name}」，这一片自动合入后才能编辑` : '自主构建正在改这部分内容，这一片自动合入后才能编辑',
    actions: [{ kind: 'run', label: '查看运行' }],
  };
}

// 「这一轮改了什么」卡上的锁定说明
export function roundLockNote(lock) {
  const st = lockState(lock);
  if (st === 'pending_confirm' || st === 'none') return '确认生效前，这些条目在 Studio 和插件页里只读；撤回这一轮也会解除';
  if (st === 'assistant_running') return '助手运行中，这些条目在 Studio 和插件页里只读；这一轮结束后确认生效或撤回才解除';
  return '这一片自动合入前，这些条目在 Studio 和插件页里只读';
}

/**
 * 输入框状态：运行中也能补充说明（deferred），归档、只读维护模式、自主构建运行中另有限制
 */
export function composerState({ sessionState, archived, maintenance, autobuildRunning }) {
  if (archived) return { disabled: true, placeholder: '项目已归档，只能查看' };
  if (maintenance?.active && maintenance.mode === 'read_only') {
    return { disabled: true, placeholder: maintenance.message || '平台正在维护，暂时只能查看' };
  }
  if (autobuildRunning) return { disabled: false, placeholder: '补充说明不打断运行，会在下一个安全点送达', delivery: 'deferred' };
  if (sessionState === 'running' || sessionState === 'connecting') {
    return { disabled: false, placeholder: '运行中也能补充说明，这一步结束后送达', delivery: 'deferred' };
  }
  if (sessionState === 'queued') return { disabled: false, placeholder: '正在排队，补充说明会和消息一起送达', delivery: 'queued' };
  if (maintenance?.active && maintenance.mode === 'reject_new_sessions' && sessionState !== 'running') {
    return { disabled: false, placeholder: maintenance.message || '平台正在维护，暂停新会话', warn: true };
  }
  return { disabled: false, placeholder: '给助手补充说明，可拖入文件或粘贴截图…' };
}
