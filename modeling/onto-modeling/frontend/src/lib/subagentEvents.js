/**
 * 子代理事件归并：生命周期（开始/终态）与可展开的实时步骤流。
 *
 * 两个内核的事件形态不同，归一化在这里收口：
 * - Jiuwen：`subagent_activity` 事件，同 `step_id` 后到的事件只更新该行状态；
 * - Claude：带 `parent_tool_use_id` 的子代理消息（本来就实时流过来）。
 *
 * 纯函数、不依赖 React，便于单独验证。
 */

const DISPATCH_TOOLS = ['Task', 'Agent'];

/**
 * 带派遣调用 id 的 Claude 子代理消息会折叠进对应子任务卡，主对话流里看不到，
 * 所以也不该计入"新消息"。
 */
export function isSubagentCardMessage(event) {
  return event.type === 'assistant_message' && Boolean(event.parent_tool_use_id);
}

/** Task/Agent tool_use_id → {description, agentType, status, durationMs, summary, done} */
export function buildSubagentLifecycle(events) {
  const lifecycle = new Map();
  for (const event of events) {
    if (event.type !== 'subagent_started' && event.type !== 'subagent_finished') continue;
    const keys = [event.tool_use_id, event.task_id].filter(Boolean);
    if (!keys.length) continue;
    const previous = keys.map((key) => lifecycle.get(key)).find(Boolean) || {};
    const next = {
      // 两个 id 都随记录带着走：终态事件可能只带 task_id（Claude 的
      // task_notification 允许 tool_use_id 为空），仍要更新卡片那一侧的索引
      taskId: event.task_id || previous.taskId || '',
      toolUseId: event.tool_use_id || previous.toolUseId || '',
      description: event.description || previous.description || '',
      agentType: event.agent_type || previous.agentType || '',
      summary: event.summary || previous.summary || '',
      status:
        event.type === 'subagent_finished'
          ? event.status || 'completed'
          : previous.status || 'running',
      durationMs: event.usage?.duration_ms ?? previous.durationMs ?? null,
      done: event.type === 'subagent_finished' || previous.done === true,
    };
    [next.toolUseId, next.taskId, ...keys]
      .filter(Boolean)
      .forEach((key) => lifecycle.set(key, next));
  }
  return lifecycle;
}

// 后台派遣时 CLI 当即返回的启动回执：只说明子代理已开始，不是它的结果
const BACKGROUND_RECEIPT_PREFIX = 'Async agent launched';
// 引擎关闭（含空闲回收）或出错：此前没结束的子代理不会再有终态
const SESSION_END_STATES = ['closed', 'error'];

export function isBackgroundReceipt(result) {
  return String(result?.excerpt || '').startsWith(BACKGROUND_RECEIPT_PREFIX);
}

/** 最后一个会话结束类事件（打断、引擎关闭或出错）在历史中的下标；没有则为 -1。 */
export function lastSessionEndIndex(events) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (
      event.type === 'session_interrupted' ||
      event.type === 'session_error' ||
      (event.type === 'session_state' && SESSION_END_STATES.includes(event.state))
    ) {
      return index;
    }
  }
  return -1;
}

/**
 * 子任务卡状态，以生命周期终态和子代理的真实结果为准：后台派遣时的回执只是启动回执，不代表子代理已结束。
 *
 * - 有终态，或已有回执且不是后台启动回执（同步派遣返回的结果）→ 已结束，失败、停止按终态与回执判定；
 * - 否则为运行中，回执是后台启动回执时标为后台运行；派遣之后会话已结束则为已中断。
 *
 * 同步结果不必等终态事件：生命周期事件可能缺失或匹配不上，卡片不能因此一直转圈。
 *
 * @param {{lifecycle?: object, result?: {isError: boolean, excerpt: string}, sessionEnded: boolean}} input
 * @returns {{done: boolean, failed: boolean, stopped: boolean, interrupted: boolean, background: boolean}}
 */
export function resolveSubagentState({ lifecycle, result, sessionEnded }) {
  const background = isBackgroundReceipt(result);
  const finished = lifecycle?.done === true;
  if (finished || (result !== undefined && !background)) {
    const status = finished ? lifecycle.status : '';
    return {
      done: true,
      failed: status === 'failed' || Boolean(result?.isError),
      stopped: status === 'stopped' || status === 'killed',
      interrupted: false,
      background,
    };
  }
  if (sessionEnded) {
    return { done: true, failed: false, stopped: true, interrupted: true, background };
  }
  return { done: false, failed: false, stopped: false, interrupted: false, background };
}

/**
 * @returns {{cards: Set<string>, steps: Map<string, object[]>}}
 *   cards = 本次历史里存在卡片的派遣调用 id（卡片被历史截断时，子代理消息要退回
 *   主工具组呈现，不能被吞掉）；steps = 每张卡自己的步骤序列。
 */
export function buildSubagentFlow(events, toolResults) {
  const cards = new Set();
  for (const event of events) {
    if (event.type !== 'assistant_message' || event.subagent) continue;
    for (const block of event.blocks) {
      if (block.kind === 'tool_use' && DISPATCH_TOOLS.includes(block.tool)) {
        cards.add(block.tool_use_id);
      }
    }
  }

  const steps = new Map();
  const positions = new Map();
  const listOf = (parent) => {
    if (!steps.has(parent)) steps.set(parent, []);
    return steps.get(parent);
  };

  for (const event of events) {
    if (event.type === 'subagent_activity') {
      const parent = event.tool_use_id || event.task_id;
      if (!parent) continue;
      const list = listOf(parent);
      const stepKey = `${parent}:${event.step_id}`;
      const at = positions.get(stepKey);
      const previous = at === undefined ? null : list[at];
      const step = {
        key: stepKey,
        kind: event.kind === 'text' ? 'text' : 'tool',
        text: event.text || previous?.text || '',
        tool: event.tool || previous?.tool || '',
        input: event.input || previous?.input || null,
        status: event.status ?? previous?.status ?? null,
      };
      if (at === undefined) {
        positions.set(stepKey, list.length);
        list.push(step);
      } else {
        list[at] = step;
      }
      continue;
    }
    if (!isSubagentCardMessage(event) || !cards.has(event.parent_tool_use_id)) continue;
    const list = listOf(event.parent_tool_use_id);
    for (const [index, block] of event.blocks.entries()) {
      const key = `${event.seq}-${index}`;
      if (block.kind === 'tool_use') {
        const result = toolResults.get(block.tool_use_id);
        list.push({
          key,
          kind: 'tool',
          tool: block.tool,
          input: block.input,
          excerpt: result?.excerpt,
          status: result === undefined ? null : result.isError ? 'error' : 'ok',
        });
      } else if (block.kind === 'text' && block.text?.trim()) {
        list.push({ key, kind: 'text', text: block.text.trim() });
      }
    }
  }
  return { cards, steps };
}
