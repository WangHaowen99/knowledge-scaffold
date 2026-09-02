export const ASSISTANT_SCROLL_ACTION = Object.freeze({
  HOLD: 'hold',
  TAIL_INSTANT: 'tail-instant',
  FOCUS_INSTANT: 'focus-instant',
  FOCUS_SMOOTH: 'focus-smooth',
});

export function assistantScrollAction({
  replayingHistory = false,
  historyJustFinished = false,
  followingTail = true,
  hasNewInteraction = false,
} = {}) {
  if (replayingHistory) return ASSISTANT_SCROLL_ACTION.HOLD;
  if (hasNewInteraction) {
    return historyJustFinished
      ? ASSISTANT_SCROLL_ACTION.FOCUS_INSTANT
      : ASSISTANT_SCROLL_ACTION.FOCUS_SMOOTH;
  }
  return followingTail
    ? ASSISTANT_SCROLL_ACTION.TAIL_INSTANT
    : ASSISTANT_SCROLL_ACTION.HOLD;
}
