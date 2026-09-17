import { Agent } from '@earendil-works/pi-agent-core';
import type { LlmDeps } from '../llm/provider.js';
import {
  isModelContextLengthExceeded as isContextLengthError,
  normalizeModelError,
} from '../llm/errors.js';
import {
  AgentCaptureAttemptTracker,
  type AgentCaptureAttemptDiagnostics,
  type AgentCaptureErrorKind,
  type AgentCaptureOutcome,
} from './agent-capture-diagnostics.js';
import type { AgentTool } from './types.js';

export {
  MODEL_CONTEXT_LENGTH_EXCEEDED_CODE,
  ModelContextLengthExceededError,
  ModelContextWindowExceededError,
  isModelContextLengthExceeded,
} from '../llm/errors.js';

export type AgentEventObserver = (event: unknown) => void;
export type { AgentCaptureAttemptDiagnostics } from './agent-capture-diagnostics.js';
export interface RunAgentOptions {
  timeoutMs?: number;
  signal?: AbortSignal;
  // null 表示调用方提供了自己的终止条件（如全文读取的无进展保护）。
  maxTurns?: number | null;
}
export interface RunAgentCaptureOptions extends RunAgentOptions {
  /**
   * Structured-output agents often need a short continuation after the model
   * writes prose or an invalid tool payload. Keep the continuation in the
   * same agent conversation so the source text is not sent again.
   */
  maxCaptureAttempts?: number;
  /** Throw a classified error when all structured capture attempts are exhausted. */
  requireCapture?: boolean;
  /** Clear a partial or truncated capture before the next structured attempt. */
  resetCapture?: () => void | Promise<void>;
  captureDiagnostics?: {
    targetToolName: string;
    /** Treat compatibility or legacy submission tools as successful captures. */
    acceptedToolNames?: string[];
    onAttempt: (diagnostics: AgentCaptureAttemptDiagnostics) => void | Promise<void>;
  };
  /** Ask the provider to call this tool during capture-only attempts. */
  requiredToolName?: string;
  /** Reuse a negotiated provider tool-choice mode across related captures. */
  toolChoiceState?: StructuredToolChoiceState;
}
export interface MakeAgentOptions {
  transformContext?: (messages: unknown[]) => unknown[] | Promise<unknown[]>;
  /** Request this registered tool when the selected provider exposes a compatible tool-choice mode. */
  requiredToolName?: string;
  /** Select how strongly the provider should require the registered target tool. */
  toolChoiceMode?: StructuredToolChoiceMode;
}

export type StructuredToolChoiceMode = 'named' | 'required' | 'auto';

export interface StructuredToolChoiceState {
  mode: StructuredToolChoiceMode;
}

type RequiredToolChoice =
  | 'auto'
  | 'any'
  | 'required'
  | { type: 'function'; function: { name: string } }
  | { type: 'tool'; name: string };

function requiredToolChoice(
  api: unknown,
  toolName: string,
  mode: StructuredToolChoiceMode,
): RequiredToolChoice | undefined {
  if (mode === 'auto') {
    switch (api) {
      case 'openai-completions':
      case 'mistral-conversations':
      case 'anthropic-messages':
      case 'bedrock-converse-stream':
      case 'google-generative-ai':
      case 'google-vertex':
        return 'auto';
      default:
        return undefined;
    }
  }
  if (mode === 'required') {
    switch (api) {
      case 'openai-completions':
      case 'mistral-conversations':
        return 'required';
      case 'anthropic-messages':
      case 'bedrock-converse-stream':
      case 'google-generative-ai':
      case 'google-vertex':
        return 'any';
      default:
        return undefined;
    }
  }
  switch (api) {
    case 'openai-completions':
    case 'mistral-conversations':
      return { type: 'function', function: { name: toolName } };
    case 'anthropic-messages':
    case 'bedrock-converse-stream':
      return { type: 'tool', name: toolName };
    case 'google-generative-ai':
    case 'google-vertex':
      // Google can require a tool call, but its API cannot select a specific tool.
      return 'any';
    default:
      // Responses APIs and custom stream functions do not share a named-tool contract.
      return undefined;
  }
}

const STRUCTURED_TOOL_CHOICE_MODES: readonly StructuredToolChoiceMode[] = ['named', 'required', 'auto'];
const TOOL_CHOICE_REFERENCE = /tool[_\s-]?choice|toolchoice|工具选择|具名(?:工具|函数)/iu;
const TOOL_CHOICE_REJECTION = /unsupported|not\s+(?:supported|allowed|implemented|recognized)|invalid|unknown|unrecognized|not\s+one\s+of|must\s+be|only.{0,80}(?:allowed|supported|accepted)|requires?.{0,80}(?:enable|parser|configur)|不支持|不允许|非法|无效|未识别|只能|必须为/iu;

function searchableModelErrorText(error: unknown): string {
  if (error instanceof Error) {
    const detail = error as Error & { code?: unknown; error?: unknown; body?: unknown; cause?: unknown };
    return [error.name, error.message, detail.code, detail.error, detail.body, detail.cause]
      .flatMap((value) => {
        if (typeof value === 'string') return [value];
        if (value === undefined || value === null) return [];
        try { return [JSON.stringify(value)]; } catch { return [String(value)]; }
      })
      .join(' ');
  }
  if (typeof error === 'string') return error;
  try { return JSON.stringify(error) ?? String(error); } catch { return String(error); }
}

export function isToolChoiceCompatibilityError(error: unknown): boolean {
  const text = searchableModelErrorText(error);
  return TOOL_CHOICE_REFERENCE.test(text) && TOOL_CHOICE_REJECTION.test(text);
}

export function nextStructuredToolChoiceMode(
  mode: StructuredToolChoiceMode,
): StructuredToolChoiceMode | undefined {
  const index = STRUCTURED_TOOL_CHOICE_MODES.indexOf(mode);
  return index >= 0 ? STRUCTURED_TOOL_CHOICE_MODES[index + 1] : undefined;
}

export type AgentCaptureFailureOutcome = Exclude<AgentCaptureOutcome, 'captured' | 'model_error'>;
export type AgentCaptureErrorCode =
  | 'STRUCTURED_OUTPUT_TRUNCATED'
  | 'STRUCTURED_TOOL_VALIDATION_FAILED'
  | 'STRUCTURED_TOOL_NOT_CALLED';

const CAPTURE_ERROR_CODES: Record<AgentCaptureFailureOutcome, AgentCaptureErrorCode> = {
  output_truncated: 'STRUCTURED_OUTPUT_TRUNCATED',
  tool_validation_failed: 'STRUCTURED_TOOL_VALIDATION_FAILED',
  tool_not_called: 'STRUCTURED_TOOL_NOT_CALLED',
};

const CAPTURE_FAILURE_PRIORITY: Record<AgentCaptureFailureOutcome, number> = {
  tool_not_called: 1,
  tool_validation_failed: 2,
  output_truncated: 3,
};

const KNOWN_MODEL_APIS = new Set([
  'openai-completions',
  'mistral-conversations',
  'openai-responses',
  'azure-openai-responses',
  'openai-codex-responses',
  'anthropic-messages',
  'bedrock-converse-stream',
  'google-generative-ai',
  'google-vertex',
]);

function errorName(error: unknown): string | undefined {
  if (!error || (typeof error !== 'object' && typeof error !== 'function')) return undefined;
  try {
    const name = (error as Record<string, unknown>).name;
    return typeof name === 'string' ? name : undefined;
  } catch {
    return undefined;
  }
}

function captureErrorKind(error: unknown): AgentCaptureErrorKind {
  if (isContextLengthError(error)) return 'context_length_exceeded';
  switch (errorName(error)) {
    case 'AgentRunTimeoutError': return 'request_timeout';
    case 'AbortError': return 'aborted';
    default: return 'provider_error';
  }
}

function enrichCaptureDiagnostics(
  diagnostics: AgentCaptureAttemptDiagnostics,
  agent: any,
  error?: unknown,
): AgentCaptureAttemptDiagnostics {
  const model = agent?.state?.model;
  const modelApi = typeof model?.api === 'string' && KNOWN_MODEL_APIS.has(model.api)
    ? model.api
    : 'custom';
  const errorKind = error === undefined ? undefined : captureErrorKind(error);
  return {
    ...diagnostics,
    modelApi,
    ...(errorKind ? { errorKind } : {}),
  };
}

async function reportCaptureDiagnostics(
  onAttempt: (diagnostics: AgentCaptureAttemptDiagnostics) => void | Promise<void>,
  diagnostics: AgentCaptureAttemptDiagnostics,
): Promise<void> {
  try {
    await onAttempt({ ...diagnostics });
  } catch {
    // 诊断链路不能改变结构化提交的业务结果。
  }
}

function reportCaptureEvent(observe: AgentEventObserver | undefined, event: unknown): void {
  try {
    observe?.(event);
  } catch {
    // Observability must not alter structured capture behavior.
  }
}

function restoreAgentMessages(agent: any, messageCount: number): void {
  const messages = agent?.state?.messages;
  if (Array.isArray(messages) && messages.length > messageCount) messages.splice(messageCount);
}

async function runAgentWithRequiredTool(
  agent: any,
  prompt: string,
  targetToolName: string | undefined,
  toolChoiceMode: StructuredToolChoiceMode,
  observe: AgentEventObserver | undefined,
  options: RunAgentOptions,
): Promise<unknown[]> {
  if (!targetToolName) return runAgent(agent, prompt, observe, options);
  const registeredTools = Array.isArray(agent?.state?.tools) ? agent.state.tools : [];
  if (!registeredTools.some((tool: any) => tool?.name === targetToolName)) {
    throw new Error(`强制调用的工具未注册：${targetToolName || '<empty>'}`);
  }

  const originalStreamFn = agent.streamFn;
  const toolChoice = requiredToolChoice(agent?.state?.model?.api, targetToolName, toolChoiceMode);
  if (toolChoice !== undefined) {
    agent.streamFn = (model: unknown, context: unknown, streamOptions: Record<string, unknown> | undefined) => (
      originalStreamFn(model, context, { ...(streamOptions ?? {}), toolChoice })
    );
  }
  try {
    return await runAgent(agent, prompt, observe, options);
  } finally {
    if (toolChoice !== undefined && agent.streamFn !== originalStreamFn) agent.streamFn = originalStreamFn;
  }
}

function captureFailureMessage(
  diagnostics: AgentCaptureAttemptDiagnostics & { outcome: AgentCaptureFailureOutcome },
  maxAttempts: number,
): string {
  const exhausted = `已自动尝试 ${maxAttempts} 次`;
  if (diagnostics.outcome === 'output_truncated') {
    return `模型输出达到长度上限，结构化提交未完成（${exhausted}）`;
  }
  if (diagnostics.outcome === 'tool_validation_failed') {
    const validation = diagnostics.validationError
      ? `：${diagnostics.validationError.replace(/\n/gu, '；')}`
      : '';
    return `提交工具参数校验失败${validation}（${exhausted}）`;
  }
  return `模型未调用 ${diagnostics.targetToolName} 提交结构化结果（${exhausted}）`;
}

export class AgentCaptureError extends Error {
  readonly code: AgentCaptureErrorCode;

  constructor(
    readonly diagnostics: AgentCaptureAttemptDiagnostics & { outcome: AgentCaptureFailureOutcome },
    readonly maxAttempts: number,
  ) {
    super(captureFailureMessage(diagnostics, maxAttempts));
    this.name = 'AgentCaptureError';
    this.code = CAPTURE_ERROR_CODES[diagnostics.outcome];
  }
}

export class AgentRunTimeoutError extends Error {
  readonly code = 'MODEL_REQUEST_TIMEOUT';

  constructor(readonly timeoutMs: number) {
    super(`模型任务执行超时（${Math.ceil(timeoutMs / 1_000)} 秒），本轮未应用任何修改，请重试`);
    this.name = 'AgentRunTimeoutError';
  }
}

// pi 集成层：构造 agent、跑一轮、拿可序列化快照。
// 关键（spike 已验证）：可恢复状态 = agent.state.messages 数组，纯 JSON。
export function makeAgent(
  llm: LlmDeps,
  systemPrompt: string,
  tools: AgentTool[],
  messages: unknown[] = [],
  options: MakeAgentOptions = {},
): Agent {
  // Some providers keep a session-level tool registry and warn when the same
  // tool id appears more than once. Preserve the last definition so retries
  // can refresh a tool intentionally without registering duplicate ids.
  const uniqueTools = [...new Map(tools.map((tool) => [tool.name, tool])).values()];
  const requiredToolName = options.requiredToolName;
  if (requiredToolName !== undefined && !uniqueTools.some((tool) => tool.name === requiredToolName)) {
    throw new Error(`强制调用的工具未注册：${requiredToolName || '<empty>'}`);
  }
  const toolChoiceMode = options.toolChoiceMode ?? 'named';
  const toolChoice = requiredToolName === undefined
    ? undefined
    : requiredToolChoice(llm.model?.api, requiredToolName, toolChoiceMode);
  const streamFn = requiredToolName === undefined
    ? llm.streamFn
    : (model: unknown, context: unknown, streamOptions: Record<string, unknown> | undefined) => llm.streamFn(
        model,
        context,
        {
          ...(streamOptions ?? {}),
          ...(toolChoice === undefined ? {} : { toolChoice }),
        },
      );
  return new Agent({
    streamFn,
    getApiKey: llm.getApiKey,
    initialState: {
      model: llm.model,
      systemPrompt,
      tools: uniqueTools as any,
      messages: messages as any,
    },
    ...(options.transformContext ? {
      transformContext: async (contextMessages) => options.transformContext!(contextMessages) as any,
    } : {}),
  });
}

// 跑一轮 prompt 并等空闲；返回可序列化的 messages 快照。
// 保险：单次 run 超过 MAX_TURNS 个回合仍未收敛（如模型反复调用不存在的工具）则 abort，
// 防止死循环挂死 HTTP 请求。
// 防死循环保险；长文档探索（大纲→检索→分页精读）需要更多回合，可用 env 调
const MAX_TURNS = Number(process.env.MODELING_AGENT_MAX_TURNS ?? 12);

export async function runAgent(
  agent: Agent,
  prompt: string,
  observe?: AgentEventObserver,
  options: RunAgentOptions = {},
): Promise<unknown[]> {
  if (options.signal?.aborted) {
    throw options.signal.reason instanceof Error ? options.signal.reason : new Error('模型任务已取消');
  }
  let turns = 0;
  let turnLimitReached = false;
  let timeout: NodeJS.Timeout | undefined;
  let run: Promise<void> | undefined;
  let removeAbortListener: () => void = () => {};
  const initialMessageCount = Array.isArray(agent.state?.messages) ? agent.state.messages.length : 0;
  const configuredMaxTurns = options.maxTurns === null ? null : Number(options.maxTurns ?? MAX_TURNS);
  const maxTurns = configuredMaxTurns === null
    ? null
    : Number.isFinite(configuredMaxTurns) && configuredMaxTurns > 0
      ? Math.floor(configuredMaxTurns)
      : 12;
  const originalStreamFn = agent.streamFn;
  const canGuardModelRequests = maxTurns !== null && typeof originalStreamFn === 'function';
  let guardedStreamFn: typeof agent.streamFn | undefined;
  if (canGuardModelRequests) {
    const turnLimit = maxTurns!;
    guardedStreamFn = (model, context, streamOptions) => {
      if (turns >= turnLimit) {
        turnLimitReached = true;
        try { agent.abort(); } catch { /* turn limit error takes precedence */ }
        throw new Error('Agent turn limit reached');
      }
      return originalStreamFn(model, context, streamOptions);
    };
    agent.streamFn = guardedStreamFn;
  }
  const unsub = agent.subscribe((event: any) => {
    const artificialTurnLimitAbort = turnLimitReached
      && event?.type === 'turn_end'
      && event?.message?.stopReason === 'aborted';
    if (!artificialTurnLimitAbort) observe?.(event);
    if (event?.type !== 'turn_end') return;
    turns += 1;
    if (!canGuardModelRequests && maxTurns !== null && turns >= maxTurns) {
      turnLimitReached = true;
      agent.abort();
    }
  });
  try {
    run = (async () => {
      await agent.prompt(prompt);
      await agent.waitForIdle();
    })();
    const contenders: Promise<unknown>[] = [run];
    const timeoutMs = Number(options.timeoutMs ?? 0);
    if (Number.isFinite(timeoutMs) && timeoutMs > 0) {
      contenders.push(new Promise<never>((_resolve, reject) => {
        timeout = setTimeout(() => reject(new AgentRunTimeoutError(timeoutMs)), timeoutMs);
      }));
    }
    if (options.signal) {
      contenders.push(new Promise<never>((_resolve, reject) => {
        const onAbort = () => {
          try { agent.abort(); } catch { /* 中断原因优先 */ }
          reject(options.signal?.reason instanceof Error ? options.signal.reason : new Error('模型任务已取消'));
        };
        options.signal!.addEventListener('abort', onAbort, { once: true });
        removeAbortListener = () => options.signal?.removeEventListener('abort', onAbort);
      }));
    }
    await Promise.race(contenders);
    const currentRunMessages = (agent.state?.messages ?? []).slice(initialMessageCount);
    const providerFailure: any = [...currentRunMessages].reverse().find((message: any) => (
      message?.role === 'assistant' && message?.stopReason === 'error'
    ));
    const providerErrorValue =
      providerFailure?.errorMessage
      ?? (providerFailure ? 'Model provider returned an error' : undefined)
      ?? (turnLimitReached ? undefined : (agent.state as any)?.errorMessage)
      ?? undefined;
    if (providerErrorValue !== undefined && providerErrorValue !== null
      && String(providerErrorValue).trim()) {
      const normalized = normalizeModelError(
        providerErrorValue,
        (agent.state as any)?.model?.contextWindow,
      );
      throw normalized === providerErrorValue
        ? new Error(providerErrorValue instanceof Error ? providerErrorValue.message : String(providerErrorValue))
        : normalized;
    }
  } catch (error) {
    if (error instanceof AgentRunTimeoutError) {
      try { agent.abort(); } catch { /* 超时错误优先 */ }
    }
    if (options.signal?.aborted) {
      try { agent.abort(); } catch { /* 中断原因优先 */ }
      // abort 只发起停止动作；等待底层 provider 流真正退出，避免取消接口返回后仍有回调继续运行。
      await run?.catch(() => undefined);
      throw options.signal.reason instanceof Error
        ? options.signal.reason
        : new Error('模型任务已取消');
    }
    throw normalizeModelError(error, (agent.state as any)?.model?.contextWindow);
  } finally {
    if (timeout) clearTimeout(timeout);
    removeAbortListener();
    unsub();
    if (guardedStreamFn && agent.streamFn === guardedStreamFn) agent.streamFn = originalStreamFn;
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
// 轻推一次要求改用工具提交。所有需要结构化提交的 agent 通用。
export async function runAgentCaptured(
  agent: any,
  prompt: string,
  captured: () => boolean,
  nudge: string,
  observe?: AgentEventObserver,
  options: RunAgentCaptureOptions = {},
): Promise<unknown[]> {
  const {
    maxCaptureAttempts = 2,
    requireCapture = false,
    resetCapture,
    captureDiagnostics,
    requiredToolName,
    toolChoiceState = { mode: 'named' },
    ...runOptions
  } = options;
  const attempts = Number.isFinite(maxCaptureAttempts) && maxCaptureAttempts > 0
    ? Math.floor(maxCaptureAttempts)
    : 2;
  if (requireCapture && !captureDiagnostics) {
    throw new Error('requireCapture 必须同时配置 captureDiagnostics');
  }
  if (captureDiagnostics) {
    let messages: unknown[] = [];
    let previousDiagnostics: AgentCaptureAttemptDiagnostics | undefined;
    let mostSevereFailure: AgentCaptureAttemptDiagnostics & { outcome: AgentCaptureFailureOutcome } | undefined;
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      const attemptPrompt = attempt === 1
        ? prompt
        : captureRetryPrompt(nudge, previousDiagnostics!);
      let tracker = new AgentCaptureAttemptTracker(
        attempt,
        captureDiagnostics.targetToolName,
        captureDiagnostics.acceptedToolNames,
      );
      while (true) {
        const initialMessageCount = Array.isArray(agent?.state?.messages) ? agent.state.messages.length : 0;
        try {
          messages = await runAgentWithRequiredTool(
            agent,
            attemptPrompt,
            requiredToolName,
            toolChoiceState.mode,
            (event) => {
              tracker.observe(event);
              observe?.(event);
            },
            // 纯提交型捕获默认单轮即可；显式传入 maxTurns 的调用方（如分段分析需要
            // 多轮提交产品事实批次）必须保留自己的轮数预算，否则会被截断成单轮。
            { ...runOptions, maxTurns: runOptions.maxTurns === undefined ? 1 : runOptions.maxTurns },
          );
          break;
        } catch (error) {
          const nextMode = requiredToolName && isToolChoiceCompatibilityError(error)
            ? nextStructuredToolChoiceMode(toolChoiceState.mode)
            : undefined;
          if (nextMode) {
            const fromMode = toolChoiceState.mode;
            restoreAgentMessages(agent, initialMessageCount);
            toolChoiceState.mode = nextMode;
            reportCaptureEvent(observe, {
              type: 'tool_choice_fallback',
              targetToolName: captureDiagnostics.targetToolName,
              fromMode,
              toMode: nextMode,
              modelApi: enrichCaptureDiagnostics(tracker.finish(false), agent).modelApi,
            });
            tracker = new AgentCaptureAttemptTracker(
              attempt,
              captureDiagnostics.targetToolName,
              captureDiagnostics.acceptedToolNames,
            );
            continue;
          }
          const diagnostics = {
            ...enrichCaptureDiagnostics(
              tracker.finish(false, { modelError: true }),
              agent,
              error,
            ),
            toolChoiceMode: toolChoiceState.mode,
          };
          await reportCaptureDiagnostics(captureDiagnostics.onAttempt, diagnostics);
          reportCaptureEvent(observe, { type: 'capture_attempt_end', diagnostics });
          throw error;
        }
      }
      const captureSucceeded = captured();
      const diagnostics = {
        ...enrichCaptureDiagnostics(tracker.finish(captureSucceeded), agent),
        toolChoiceMode: toolChoiceState.mode,
      };
      previousDiagnostics = diagnostics;
      await reportCaptureDiagnostics(captureDiagnostics.onAttempt, diagnostics);
      reportCaptureEvent(observe, { type: 'capture_attempt_end', diagnostics });
      if (diagnostics.outcome === 'captured') return messages;
      if (diagnostics.outcome === 'model_error') {
        throw new Error('Model provider returned an error');
      }
      const failure = diagnostics as AgentCaptureAttemptDiagnostics & { outcome: AgentCaptureFailureOutcome };
      if (!mostSevereFailure
        || CAPTURE_FAILURE_PRIORITY[failure.outcome] > CAPTURE_FAILURE_PRIORITY[mostSevereFailure.outcome]) {
        mostSevereFailure = failure;
      }
      if (attempt < attempts) await resetCapture?.();
    }
    if (requireCapture && mostSevereFailure) {
      throw new AgentCaptureError(
        mostSevereFailure,
        attempts,
      );
    }
    return messages;
  }

  let messages = await runAgent(agent, prompt, observe, runOptions);
  for (let attempt = 1; attempt < attempts && !captured(); attempt += 1) {
    messages = await runAgent(
      agent,
      `${nudge}\n不要重复分析原文；只修正上一次工具参数并立即再次调用提交工具。`,
      observe,
      runOptions,
    );
  }
  return messages;
}

function captureRetryPrompt(nudge: string, diagnostics: AgentCaptureAttemptDiagnostics): string {
  let correction: string;
  if (diagnostics.outcome === 'output_truncated') {
    correction = '上一次响应达到输出长度上限，工具参数可能被截断。请压缩非必要描述并去除重复内容，优先保证必填字段和 JSON 结构完整。';
  } else if (diagnostics.outcome === 'tool_validation_failed') {
    correction = diagnostics.validationError
      ? `上一次工具参数未通过校验。请逐项修正以下字段错误，不要复用损坏的 JSON：\n${diagnostics.validationError}`
      : '上一次工具参数未通过校验。请重新生成完整参数，确保所有必填字段、字段类型和 JSON 结构符合工具定义。';
  } else {
    correction = `上一次未调用目标提交工具。不要输出正文，直接调用 ${diagnostics.targetToolName}。`;
  }
  return `${nudge}\n${correction}\n不要重复分析原文；只提交修正后的结构化结果。`;
}
