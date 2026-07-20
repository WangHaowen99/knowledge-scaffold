import { isSessionWaitingForUserStatus } from './sessionModel.js';

export function findPendingInteraction(messages) {
  for (let index = (messages || []).length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role === 'user') return null;
    if (
      message?.role === 'assistant'
      && isSessionWaitingForUserStatus(message.status)
      && message.interaction?.id
    ) {
      return message.interaction;
    }
  }
  return null;
}
