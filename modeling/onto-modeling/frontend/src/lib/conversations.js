// 会话下拉的展示规则：排序、标题兜底、相对时间、运行中标记（纯函数，便于单测）

export const UNTITLED = '新会话';

export function sortConversations(conversations) {
  return [...(conversations || [])].sort(
    (a, b) => (b.updated_at || 0) - (a.updated_at || 0),
  );
}

export function conversationTitle(conversation) {
  return conversation?.title?.trim() || UNTITLED;
}

// ts / now 均为秒；now 可注入以便单测
export function conversationTime(ts, now = Date.now() / 1000) {
  if (!ts) return '';
  const diff = now - ts;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)} 天前`;
  return new Date(ts * 1000).toLocaleDateString();
}

// live 为 GET /conversations 返回的引擎绑定状态；只有它绑定且正忙的那条算"运行中"
export function isRunning(live, conversationId) {
  return Boolean(live?.busy && live.conversation_id === conversationId);
}
