import { Agent } from '@earendil-works/pi-agent-core';
import type { LlmDeps } from '../llm/provider.js';
import type { AgentTool } from './types.js';

// pi 集成层：构造 agent、跑一轮、拿可序列化快照。
// 关键（spike 已验证）：可恢复状态 = agent.state.messages 数组，纯 JSON。
export function makeAgent(
  llm: LlmDeps,
  systemPrompt: string,
  tools: AgentTool[],
  messages: unknown[] = [],
): Agent {
  return new Agent({
    streamFn: llm.streamFn,
    getApiKey: llm.getApiKey,
    initialState: {
      model: llm.model,
      systemPrompt,
      tools: tools as any,
      messages: messages as any,
    },
  });
}

// 跑一轮 prompt 并等空闲；返回可序列化的 messages 快照。
// 保险：单次 run 超过 MAX_TURNS 个回合仍未收敛（如模型反复调用不存在的工具）则 abort，
// 防止死循环挂死 HTTP 请求。
const MAX_TURNS = 8;

export async function runAgent(agent: Agent, prompt: string): Promise<unknown[]> {
  let turns = 0;
  const unsub = agent.subscribe((event: any) => {
    if (event?.type === 'turn_end' && ++turns >= MAX_TURNS) agent.abort();
  });
  try {
    await agent.prompt(prompt);
    await agent.waitForIdle();
  } finally {
    unsub();
  }
  return agent.state.messages as unknown[];
}

// 从快照恢复并继续（注入一条用户消息后续跑，如专家应答）。
export async function resumeAgent(agent: Agent, userMessage: string): Promise<unknown[]> {
  await agent.prompt(userMessage);
  await agent.waitForIdle();
  return agent.state.messages as unknown[];
}
