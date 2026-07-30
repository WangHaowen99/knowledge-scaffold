function validMessage(message) {
  return message
    && typeof message === 'object'
    && typeof message.id === 'string'
    && Number.isFinite(Number(message.seq))
    && Array.isArray(message.blocks);
}

export function liveAiConstructionMessage(event) {
  if (event?.type !== 'ai_construction.message.written') return null;
  const message = event?.payload?.message;
  return validMessage(message) ? message : null;
}

/**
 * 进度类消息只更新文案、不改变任务图，收到后贴消息即可，无需回源重载 session。
 * 其余消息（含超过内联上限、只发信号的消息）都要触发一次刷新。
 */
export function isProgressOnlyMessageEvent(event) {
  return event?.type === 'ai_construction.message.written'
    && event?.payload?.progressOnly === true
    && Boolean(liveAiConstructionMessage(event));
}

export function upsertAiConstructionMessage(messages = [], message) {
  if (!validMessage(message)) return messages;
  const next = messages.some((item) => item?.id === message.id)
    ? messages.map((item) => item?.id === message.id ? message : item)
    : [...messages, message];
  return next.sort((left, right) => Number(left?.seq ?? 0) - Number(right?.seq ?? 0));
}

export function mergeAiConstructionMessages(...collections) {
  const byId = new Map();
  for (const messages of collections) {
    for (const message of messages ?? []) {
      if (validMessage(message)) byId.set(message.id, message);
    }
  }
  return [...byId.values()].sort(
    (left, right) => Number(left?.seq ?? 0) - Number(right?.seq ?? 0),
  );
}

function messagePage(messages, hasMore) {
  return {
    hasMore: Boolean(hasMore),
    oldestSeq: messages[0]?.seq ?? null,
    newestSeq: messages.at(-1)?.seq ?? null,
  };
}

export function mergeAiConstructionSession(current, refreshed) {
  if (!current || current?.project?.id !== refreshed?.project?.id) return refreshed;
  const currentMessages = current.aiConstructionMessages ?? [];
  const refreshedMessages = refreshed.aiConstructionMessages ?? [];
  const messages = mergeAiConstructionMessages(currentMessages, refreshedMessages);
  const currentOldest = Number(currentMessages[0]?.seq);
  const refreshedOldest = Number(refreshedMessages[0]?.seq);
  const retainedEarlierMessages = Number.isFinite(currentOldest)
    && Number.isFinite(refreshedOldest)
    && currentOldest < refreshedOldest;
  const hasMore = retainedEarlierMessages
    ? current.aiConstructionMessagesPage?.hasMore
    : refreshed.aiConstructionMessagesPage?.hasMore;
  return {
    ...refreshed,
    aiConstructionMessages: messages,
    aiConstructionMessagesPage: messagePage(messages, hasMore),
  };
}

export function prependAiConstructionMessagePage(session, page) {
  const messages = mergeAiConstructionMessages(
    page?.messages ?? [],
    session?.aiConstructionMessages ?? [],
  );
  return {
    ...session,
    aiConstructionMessages: messages,
    aiConstructionMessagesPage: messagePage(messages, page?.hasMore),
  };
}
