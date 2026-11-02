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

// 写入范围 ScopeLock.paths 是 Python fnmatch 模式（后端 fnmatchcase）：* 跨目录（** 同 *），? 任一字符，
// [seq] / [!seq] 字符类，没有闭合的 [ 按字面；字面量的 [ ] * ? 由后端转义成 [[]、[]]、[*]、[?]。
// 按 CPython fnmatch.translate 的规则翻成 JS 正则，两边判断同一条路径结果一致（gaps「B5 合并」第 2 条）
export function fnmatchToRegExp(pat) {
  let re = '';
  let i = 0;
  const n = pat.length;
  while (i < n) {
    const c = pat[i];
    i += 1;
    if (c === '*') {
      while (i < n && pat[i] === '*') i += 1;
      re += '.*';
    } else if (c === '?') re += '.';
    else if (c === '[') {
      let j = i;
      if (j < n && pat[j] === '!') j += 1;
      if (j < n && pat[j] === ']') j += 1;
      while (j < n && pat[j] !== ']') j += 1;
      if (j >= n) re += '\\[';
      else {
        let stuff = pat.slice(i, j);
        i = j + 1;
        if (!stuff) re += '(?!)';
        else if (stuff === '!') re += '.';
        else {
          const neg = stuff[0] === '!';
          if (neg) stuff = stuff.slice(1);
          // JS 字符类里 ] \ [ 要转义（Python 允许 []a] 这种写法）；区间的 - 原样保留
          stuff = stuff.replace(/[\\\][]/g, '\\$&');
          if (!neg && stuff[0] === '^') stuff = `\\${stuff}`;
          re += `[${neg ? '^' : ''}${stuff}]`;
        }
      }
    } else re += c.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');
  }
  try {
    return new RegExp(`^${re}$`, 's');
  } catch {
    // 区间写反之类 JS 不认的字符类：退回按字面比
    return { test: (s) => s === pat };
  }
}

/**
 * target 可以是 EntryRef（{id}）、ArtifactRef（{type, key}）、{path} 或 "<kind>:<key>" 字符串
 */
export function isLocked(lock, target) {
  if (lockState(lock) === 'none' || !target) return false;
  const t = typeof target === 'string' ? (target.includes('/') && !target.includes(':') ? { path: target } : { id: target }) : target;
  if (t.id && (lock.entries || []).some((e) => e.id === t.id)) return true;
  if (t.type && t.key && (lock.artifacts || []).some((a) => a.type === t.type && a.key === t.key)) return true;
  if (t.path && (lock.paths || []).some((p) => fnmatchToRegExp(p).test(t.path))) return true;
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
    text: lock.slice ? `自主构建正在做「${lock.slice.name}」，这一片完成前只读` : '自主构建正在改这部分内容，这一片完成前只读',
    actions: [{ kind: 'run', label: '查看运行' }],
  };
}

// 「这一轮改了什么」卡上的锁定说明
export function roundLockNote(lock) {
  const st = lockState(lock);
  if (st === 'pending_confirm' || st === 'none') return '确认生效或撤回前，这些条目只读';
  if (st === 'assistant_running') return '助手运行中，这些条目只读';
  return '这一片完成前，这些条目只读';
}

/**
 * 输入框状态：运行中也能补充说明（deferred），归档、只读维护模式、自主构建运行中另有限制
 */
export function composerState({ sessionState, archived, maintenance, autobuildRunning, researching }) {
  if (archived) return { disabled: true, placeholder: '项目已归档，只能查看' };
  if (maintenance?.active && maintenance.mode === 'read_only') {
    return { disabled: true, placeholder: maintenance.message || '平台正在维护，暂时只能查看' };
  }
  if (autobuildRunning) return { disabled: false, placeholder: '补充说明，不打断运行…', delivery: 'deferred' };
  if (sessionState === 'running' || sessionState === 'connecting') {
    return { disabled: false, placeholder: '补充说明，不打断运行…', delivery: 'deferred' };
  }
  if (sessionState === 'queued') return { disabled: false, placeholder: '正在排队，补充说明会和消息一起送达', delivery: 'queued' };
  if (maintenance?.active && maintenance.mode === 'reject_new_sessions' && sessionState !== 'running') {
    return { disabled: false, placeholder: maintenance.message || '平台正在维护，暂停新会话', warn: true };
  }
  // researching：'blueprint' 蓝图待确认（切片规划）；其余真值是读资料
  if (researching === 'blueprint') return { disabled: false, placeholder: '对蓝图有意见直接说…' };
  if (researching) return { disabled: false, placeholder: '给助手补充说明，或者继续拖入资料…' };
  return { disabled: false, placeholder: '给助手补充说明，可拖入文件或粘贴截图…' };
}
