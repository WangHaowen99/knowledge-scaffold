import { isSessionWaitingForUserStatus } from './sessionModel.js';

export const INTERACTION_CANCELLED_TYPE = 'interaction_cancelled';
export const INTERACTION_CANCELLED_NOTICE = '用户取消了操作';

export function findPendingInteraction(messages) {
  for (let index = (messages || []).length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role === 'user') {
      return null;
    }
    if (
      message?.role === 'assistant' &&
      isSessionWaitingForUserStatus(message.status) &&
      message.interaction?.id
    ) {
      const runId = String(
        message.runId || message.interaction.runId || '',
      ).trim();
      return runId && !message.interaction.runId ?
        { ...message.interaction, runId } :
        message.interaction;
    }
  }
  return null;
}

export function applyInteractionCancellation(messages, cancellation = {}) {
  const interactionId = String(cancellation.interaction_id || '').trim();
  if (!interactionId) {
    return messages || [];
  }

  const existing = (messages || []).some(
    (message) =>
      message?.metadata?.type === INTERACTION_CANCELLED_TYPE &&
      String(message?.metadata?.interaction_id || '').trim() === interactionId,
  );
  if (existing) {
    return messages || [];
  }

  const next = [...(messages || [])];
  for (let index = next.length - 1; index >= 0; index -= 1) {
    const message = next[index];
    if (
      message?.role === 'assistant' &&
      String(message?.interaction?.id || '').trim() === interactionId &&
      isSessionWaitingForUserStatus(message.status)
    ) {
      next[index] = {
        ...message,
        status: 'cancelled',
        isLoading: false,
        meta: {
          ...(message.meta || {}),
          cancel_reason: INTERACTION_CANCELLED_TYPE,
        },
      };
      break;
    }
  }

  next.push({
    id: `interaction-cancelled-${interactionId}`,
    role: 'system',
    content: String(
      cancellation.display_message || INTERACTION_CANCELLED_NOTICE,
    ),
    metadata: {
      type: INTERACTION_CANCELLED_TYPE,
      interaction_id: interactionId,
      cancelled_at: cancellation.cancelled_at || '',
    },
  });
  return next;
}
