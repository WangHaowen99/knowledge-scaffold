/**
 * Groups a flat message array into logical conversation turns.
 *
 * A turn begins when a user message arrives.
 *
 * Turn shape: { index, userMsg, agentMsgs: Message[] }
 */

export function buildConversationTurns(messages) {
  const turns = [];
  const turnRunIndexMaps = [];

  for (const msg of messages) {
    if (msg.role === 'user') {
      turns.push({ index: turns.length + 1, userMsg: msg, agentMsgs: [] });
      turnRunIndexMaps.push(new Map());
    } else if (msg.kind === 'agent_step' && msg.stepLabel === 'turn_stream') {
      if (turns.length === 0) {
        turns.push({ index: 1, userMsg: null, agentMsgs: [] });
        turnRunIndexMaps.push(new Map());
      }
      const currentTurn = turns[turns.length - 1];
      const currentRunIndexMap = turnRunIndexMaps[turnRunIndexMaps.length - 1];
      const runKey = typeof msg.runId === 'string' && msg.runId.trim()
        ? `run:${msg.runId.trim()}`
        : (msg.id ? `id:${msg.id}` : '');

      if (runKey) {
        const existingIndex = currentRunIndexMap.get(runKey);
        if (existingIndex !== undefined) {
          currentTurn.agentMsgs[existingIndex] = msg;
        } else {
          currentRunIndexMap.set(runKey, currentTurn.agentMsgs.length);
          currentTurn.agentMsgs.push(msg);
        }
      } else {
        currentTurn.agentMsgs.push(msg);
      }
    }
    // Other kinds of agent_step messages (router, planner, etc.) are ignored for grouping
  }

  return turns;
}
