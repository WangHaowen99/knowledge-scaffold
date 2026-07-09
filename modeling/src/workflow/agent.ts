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
// 防死循环保险；长文档探索（大纲→检索→分页精读）需要更多回合，可用 env 调
const MAX_TURNS = Number(process.env.MODELING_AGENT_MAX_TURNS ?? 12);

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

// 捕获型 agent 的协议恢复：跑完未捕获到工具产物（模型把结果写成了正文）时，
// 轻推一次要求改用工具提交。所有 submit_* / ask_user 类 agent 通用。
export async function runAgentCaptured(
  agent: any,
  prompt: string,
  captured: () => boolean,
  nudge: string,
): Promise<unknown[]> {
  let messages = await runAgent(agent, prompt);
  if (!captured()) messages = await runAgent(agent, nudge);
  return messages;
}
