const CODE_TITLE = {
  PACKAGE_CHECK_REQUIRED: '需要先完成知识包校验',
  PACKAGE_CHECK_FAILED: '知识包还有阻断错误',
  KNOWLEDGE_EVIDENCE_REQUIRED: '有确认知识缺少可追溯证据',
  PENDING_INTERACTIONS: '还有待确认事项未处理',
  REPAIR_TARGET_UNRESOLVED: '这条问题需要先确定修改目标',
  PACKAGE_WARNINGS_UNREVIEWED: '非阻断警告尚未复核',
};

const LOCATION_HINT = {
  assistant: '在下方建模助手里处理',
  scenario: '在「场景推演」页签处理',
};

function detailLines(data) {
  if (Array.isArray(data?.problems) && data.problems.length) {
    // 后端已经算出具体缺证据的条目，全部丢弃只显示数量会让专家无从下手
    return data.problems.slice(0, 8).map((item) => `· ${String(item)}`)
      .concat(data.problems.length > 8 ? [`· 还有 ${data.problems.length - 8} 项…`] : []);
  }
  if (Array.isArray(data?.candidates) && data.candidates.length) {
    return data.candidates.map((item) => `· ${item.targetKind}：${item.targetKey}`);
  }
  if (Array.isArray(data?.pendingInteractions) && data.pendingInteractions.length) {
    return data.pendingInteractions.slice(0, 8).map((item) => (
      `· ${item.title}（${LOCATION_HINT[item.location] ?? '在建模助手里处理'}）`
    ));
  }
  return [];
}

/**
 * 把「确认完成建模」被拦下的 409 变成一张可操作卡片。
 * 后端在响应里声明了 nextActions，前端只负责渲染，不再为每个 code 写特判。
 */
export function completionBlockerMessage(error) {
  const data = error?.data;
  const actions = Array.isArray(data?.nextActions) ? data.nextActions : [];
  const code = String(data?.code ?? '');
  if (!code || !actions.length) return null;

  const reason = String(error?.message ?? '').trim() || '确认完成建模被拦下';
  const lines = detailLines(data);
  return {
    id: `completion-blocked-${code}`,
    role: 'assistant',
    createdAt: new Date().toISOString(),
    blocks: [
      { type: 'status', title: CODE_TITLE[code] ?? '确认完成建模被拦下', summary: reason },
      ...(lines.length ? [{ type: 'text', text: lines.join('\n') }] : []),
      { type: 'next_actions', title: '选择下一步', actions },
    ],
  };
}
