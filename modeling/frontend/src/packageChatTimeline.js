function timestamp(value) {
  const parsed = Date.parse(String(value ?? ''));
  return Number.isFinite(parsed) ? parsed : null;
}

function structuredPlainText(message) {
  const blocks = message?.blocks ?? [];
  if (blocks.length !== 1 || blocks[0]?.type !== 'text') return null;
  return String(blocks[0].text ?? '').trim();
}

/**
 * 服务端把用户消息写库后会立刻经事件流回来，与本地乐观插入的同一条消息重复。
 * 本地 turn 只是尚未回流时的临时占位，因此服务端一旦出现同角色同内容的消息就丢弃它。
 * 发送期间输入被禁用，同一时刻最多只有一条在途的本地用户消息，不会误伤重复提问。
 */
function withoutEchoedTurns(turns, structuredMessages) {
  if (!turns.length || !structuredMessages.length) return turns;
  const echoed = new Set();
  for (const message of structuredMessages) {
    const text = structuredPlainText(message);
    if (text) echoed.add(`${message.role}\u0000${text}`);
  }
  return turns.filter((turn) => (
    !echoed.has(`${turn?.role}\u0000${String(turn?.text ?? '').trim()}`)
  ));
}

export function mergePackageChatTimeline({ turns = [], structuredMessages = [] }) {
  const legacyBase = -1_000_000_000_000_000;
  const visibleTurns = withoutEchoedTurns(turns, structuredMessages);
  const items = [
    ...visibleTurns.map((item, index) => ({
      kind: 'turn', item, turnIndex: index,
      order: timestamp(item?.createdAt) ?? legacyBase + index,
      tie: index,
    })),
    ...structuredMessages.map((item, index) => ({
      kind: 'structured', item,
      order: timestamp(item?.createdAt) ?? legacyBase + visibleTurns.length + index,
      tie: visibleTurns.length + index,
    })),
  ];
  return items.sort((left, right) => left.order - right.order || left.tie - right.tie);
}

export function latestStructuredActionMessageId(timeline = []) {
  return [...timeline].reverse().find((entry) => (
    entry.kind === 'structured'
      && (entry.item?.blocks ?? []).some((block) => block?.type === 'next_actions')
  ))?.item?.id ?? null;
}

export function canUseStructuredMessageActions({
  busy = false,
  constructionStatus = 'active',
  message,
  messageId,
  latestActionMessageId,
}) {
  const packageCheckReview = (message?.blocks ?? []).some((block) => (
    block?.type === 'status' && String(block.title ?? '').startsWith('处理校验发现：')
  ));
  return !busy
    && (constructionStatus !== 'completed' || packageCheckReview)
    && Boolean(messageId)
    && messageId === latestActionMessageId;
}

export function isMaterialAnalysisRunning(aiConstruction) {
  return (aiConstruction?.tasks ?? []).some((task) => (
    ['material_analysis', 'package_baseline_review'].includes(task?.kind)
      && task?.status === 'running'
  ));
}

const MILESTONE_CONFIRM_ACTION = {
  goal_confirmed: 'confirm_goal',
  materials_ready: 'confirm_materials',
  process_confirmed: 'confirm_process',
  knowledge_confirmed: 'confirm_knowledge',
  deterministic_confirmed: 'confirm_deterministic',
  construction_completed: 'confirm_construction',
};

export function confirmedMilestoneActionIds(aiConstruction) {
  return (aiConstruction?.milestones ?? []).flatMap((milestone) => {
    const actionId = milestone?.status === 'confirmed'
      ? MILESTONE_CONFIRM_ACTION[milestone?.id]
      : null;
    return actionId ? [actionId] : [];
  });
}

export function milestoneNavigationTarget(milestoneId) {
  return milestoneId === 'construction_completed' ? 'scenario' : '';
}

function structuredText(message) {
  const parts = (message?.blocks ?? []).flatMap((block) => {
    if (block?.type === 'text' && block.text) return [String(block.text).trim()];
    if (block?.type === 'change_plan' && block.summary) {
      return [`已提出建模调整方案：${String(block.summary).trim()}`];
    }
    return [];
  }).filter(Boolean);
  return parts.join('\n');
}

export function buildPackageChatHistory({ turns = [], structuredMessages = [] }, limit = 12) {
  return mergePackageChatTimeline({ turns, structuredMessages })
    .flatMap((entry) => {
      const role = entry.item?.role;
      if (role !== 'user' && role !== 'assistant') return [];
      const value = entry.kind === 'turn'
        ? String(entry.item?.text ?? '').trim()
        : structuredText(entry.item);
      return value ? [{ role, text: value }] : [];
    })
    .slice(-Math.max(1, limit));
}
