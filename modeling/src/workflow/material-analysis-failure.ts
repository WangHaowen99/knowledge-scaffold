import { isModelContextLengthExceeded } from '../llm/errors.js';
import { sanitizeToolValidationError } from './agent-capture-diagnostics.js';

const DEFAULT_ERROR_CODE = 'MATERIAL_ANALYSIS_FAILED';
const MAX_ERROR_CODE_LENGTH = 120;
const MAX_DIAGNOSTIC_MESSAGE_LENGTH = 2_000;
const STABLE_ERROR_CODE = /^[A-Za-z][A-Za-z0-9_.:-]*$/u;
const UNKNOWN_USER_MESSAGE = '资料分析未完成。请重新分析资料；如果仍然失败，请联系管理员查看任务诊断。';
const UNKNOWN_DIAGNOSTIC_MESSAGE = '资料分析意外中断';

type ModelFailureCode =
  | 'MODEL_SERVICE_UNAVAILABLE'
  | 'MODEL_RATE_LIMITED'
  | 'MODEL_REQUEST_TIMEOUT'
  | 'MODEL_QUOTA_EXHAUSTED'
  | 'MODEL_CONTEXT_LENGTH_EXCEEDED';

const MODEL_FAILURE_CODES = new Set<ModelFailureCode>([
  'MODEL_SERVICE_UNAVAILABLE',
  'MODEL_RATE_LIMITED',
  'MODEL_REQUEST_TIMEOUT',
  'MODEL_QUOTA_EXHAUSTED',
  'MODEL_CONTEXT_LENGTH_EXCEEDED',
]);

const MODEL_QUOTA_PATTERNS = [
  /(?:^|\n)(?:insufficient[_-]?quota|quota[_-]?(?:exceeded|exhausted)|billing[_-]?(?:hard[_-]?)?limit(?:[_-]?reached)?)(?:\n|$)/iu,
  /\binsufficient[_-]?quota\b/iu,
  /(?:^|\n)(?:QuotaExceeded|InsufficientQuota|BillingLimit)Error(?:\n|$)/u,
  /\b(?:insufficient|exhausted|depleted)\s+(?:account\s+)?(?:quota|credits?|balance)\b/iu,
  /\b(?:quota|credits?|balance)\s+(?:is\s+|has\s+been\s+)?(?:exhausted|depleted|used\s+up)\b/iu,
  /\bbilling\s+(?:hard\s+)?limit\s+(?:has\s+been\s+)?reached\b/iu,
  /额度.{0,20}(?:耗尽|不足)/u,
];

const MODEL_RATE_LIMIT_PATTERNS = [
  /(?:^|\n)(?:rate[_-]?limit(?:[_-]?exceeded)?|too[_-]?many[_-]?requests|throttl(?:ed|ing(?:[_-]?exception)?))(?:\n|$)/iu,
  /(?:^|\n)(?:RateLimit|TooManyRequests|Throttling)Error(?:\n|$)/u,
  /\brate\s+limit(?:ed|\s+exceeded)?\b/iu,
  /\btoo\s+many\s+requests\b/iu,
  /\brequest(?:s)?\s+(?:was\s+|were\s+|is\s+|are\s+)?throttled\b/iu,
  /\b429\b/u,
  /请求过于频繁|限流/u,
];

const MODEL_REQUEST_TIMEOUT_PATTERNS = [
  /(?:^|\n)(?:ETIMEDOUT|REQUEST[_-]?TIMEOUT|UND_ERR_(?:CONNECT|HEADERS|BODY)_TIMEOUT)(?:\n|$)/u,
  /(?:^|\n)(?:API)?TimeoutError(?:\n|$)/u,
  /\b(?:request|connection|operation)\s+(?:has\s+)?timed\s+out\b/iu,
  /\b(?:request|connection|operation)\s+timeout\b/iu,
  /\bdeadline\s+exceeded\b/iu,
  /超时/u,
];

const MODEL_SERVICE_PATTERNS = [
  /(?:^|\n)Model provider returned an error(?:\n|$)/u,
  /(?:^|\n)(?:ECONNRESET|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|EPIPE|ERR_NETWORK|UND_ERR_SOCKET|SERVICE[_-]?UNAVAILABLE|API[_-]?CONNECTION[_-]?ERROR|INTERNAL[_-]?SERVER[_-]?ERROR)(?:\n|$)/u,
  /(?:^|\n)(?:APIConnection|ServiceUnavailable|InternalServer|Network)Error(?:\n|$)/u,
  /\b(?:service|server)\s+(?:is\s+)?temporarily\s+unavailable\b/iu,
  /\bservice\s+unavailable\b/iu,
  /\b(?:bad\s+gateway|gateway\s+timeout)\b/iu,
  /\bconnection\s+(?:was\s+)?(?:reset|refused)\b/iu,
  /\b(?:socket\s+hang\s+up|network\s+error|fetch\s+failed|failed\s+to\s+connect)\b/iu,
  /\bstream\s+ended\s+without\s+finish_reason\b/iu,
  /\b5\d\d\b/u,
  /网络.{0,12}(?:异常|中断)/u,
];

interface KnownFailurePresentation {
  currentStep: string;
  userMessage: string;
}

const KNOWN_FAILURES: Record<string, KnownFailurePresentation> = {
  STRUCTURED_OUTPUT_TRUNCATED: {
    currentStep: '结构化分析输出被截断',
    userMessage: '资料分析的结构化输出达到模型长度上限，系统已完成自动重试。请重新分析资料；如果仍然失败，请联系管理员检查模型输出配置。',
  },
  STRUCTURED_TOOL_VALIDATION_FAILED: {
    currentStep: '结构化分析参数校验失败',
    userMessage: '资料分析的提交参数未通过结构校验，系统已自动要求模型修正。请重新分析资料；如果仍然失败，请联系管理员查看参数校验记录。',
  },
  STRUCTURED_TOOL_NOT_CALLED: {
    currentStep: '结构化分析未提交',
    userMessage: '模型未调用资料分析提交工具，系统已完成自动重试。请重新分析资料；如果仍然失败，请联系管理员检查所选模型的工具调用支持。',
  },
  DOMAIN_OUTPUT_TRUNCATED: {
    currentStep: '领域汇总输出被截断',
    userMessage: '领域汇总输出达到模型长度上限，系统已完成自动重试并保留全部文档分析结果。请重新分析资料；如果仍然失败，请联系管理员检查模型输出配置。',
  },
  DOMAIN_TOOL_VALIDATION_FAILED: {
    currentStep: '领域汇总参数校验失败',
    userMessage: '领域汇总参数未通过结构校验，系统已自动要求模型修正并保留全部文档分析结果。请重新分析资料；如果仍然失败，请联系管理员查看领域汇总参数校验记录。',
  },
  DOMAIN_TOOL_NOT_CALLED: {
    currentStep: '领域汇总未提交',
    userMessage: '模型未调用领域汇总提交工具，系统已完成自动重试并保留全部文档分析结果。请重新分析资料；如果仍然失败，请联系管理员检查所选模型的工具调用支持。',
  },
  DOMAIN_INPUT_BUDGET_EXCEEDED: {
    currentStep: '领域汇总输入超过安全预算',
    userMessage: '领域汇总在保留每类代表候选和待确认问题后仍超过安全预算。请减少本次资料数量，或将资料拆分为多个批次后重新分析。',
  },
  MATERIAL_ANALYSIS_PACKAGE_STALE: {
    currentStep: '分析期间知识包修订已变化',
    userMessage: '分析期间知识包修订已变化，本次结果未挂接。请基于最新修订重新分析资料。',
  },
  MATERIAL_ANALYSIS_DOCUMENTS_STALE: {
    currentStep: '分析期间建模资料已变化',
    userMessage: '分析期间建模资料已变化，本次结果未挂接。请基于最新资料重新分析。',
  },
  MODEL_SERVICE_UNAVAILABLE: {
    currentStep: '模型服务暂时不可用',
    userMessage: '模型服务当前不可用或连接异常，系统已完成有限重试并保留资料分析断点。请稍后重新分析资料；如果持续失败，请联系管理员检查模型服务状态和网络连接。',
  },
  MODEL_RATE_LIMITED: {
    currentStep: '模型请求受到频率限制',
    userMessage: '模型服务请求过于频繁，系统已降低并发、完成有限重试并保留资料分析断点。请稍后重新分析资料；如果持续失败，请联系管理员调整任务并发或模型服务频率限制。',
  },
  MODEL_REQUEST_TIMEOUT: {
    currentStep: '模型请求超时',
    userMessage: '模型服务未在限定时间内返回，系统已完成有限重试并保留资料分析断点。请稍后重新分析资料；如果持续失败，请联系管理员检查模型响应和超时配置。',
  },
  MODEL_QUOTA_EXHAUSTED: {
    currentStep: '模型服务额度不足',
    userMessage: '模型服务额度或余额已用尽，资料分析已暂停并保留断点。请联系管理员补充额度或切换已授权模型后重新分析资料。',
  },
  MODEL_CONTEXT_LENGTH_EXCEEDED: {
    currentStep: '模型上下文长度超限',
    userMessage: '本次资料与输出超过所选模型的上下文窗口，系统已保留资料分析断点。请减少本次资料数量、拆分批次，或调整已确认的模型上下文配置后重新分析资料。',
  },
};

export interface MaterialAnalysisFailure {
  errorCode: string;
  diagnosticMessage: string;
  userMessage: string;
  currentStep: string;
}

function stringProperty(value: unknown, key: 'code' | 'message' | 'name'): string | undefined {
  if (!value || (typeof value !== 'object' && typeof value !== 'function')) return undefined;
  try {
    const property = (value as Record<string, unknown>)[key];
    return typeof property === 'string' && property.trim() ? property.trim() : undefined;
  } catch {
    return undefined;
  }
}

function stableErrorCode(value: string | undefined): string | undefined {
  return value && value.length <= MAX_ERROR_CODE_LENGTH && STABLE_ERROR_CODE.test(value)
    ? value
    : undefined;
}

function errorCode(error: unknown): string {
  const code = stableErrorCode(stringProperty(error, 'code'));
  if (code) return code;
  const name = stringProperty(error, 'name');
  return name !== 'Error' ? stableErrorCode(name) ?? DEFAULT_ERROR_CODE : DEFAULT_ERROR_CODE;
}

function matchesModelFailure(signals: string, patterns: RegExp[]): boolean {
  return patterns.some((pattern) => pattern.test(signals));
}

function classifyModelFailure(error: unknown, sourceCode: string): ModelFailureCode | undefined {
  if (MODEL_FAILURE_CODES.has(sourceCode as ModelFailureCode)) {
    return sourceCode as ModelFailureCode;
  }
  if (isModelContextLengthExceeded(error)) return 'MODEL_CONTEXT_LENGTH_EXCEEDED';

  const signals = [
    stringProperty(error, 'code'),
    stringProperty(error, 'name'),
    stringProperty(error, 'message'),
  ].filter((value): value is string => Boolean(value)).join('\n');
  if (matchesModelFailure(signals, MODEL_QUOTA_PATTERNS)) return 'MODEL_QUOTA_EXHAUSTED';
  if (matchesModelFailure(signals, MODEL_RATE_LIMIT_PATTERNS)) return 'MODEL_RATE_LIMITED';
  if (matchesModelFailure(signals, MODEL_REQUEST_TIMEOUT_PATTERNS)) return 'MODEL_REQUEST_TIMEOUT';
  if (matchesModelFailure(signals, MODEL_SERVICE_PATTERNS)) return 'MODEL_SERVICE_UNAVAILABLE';
  return undefined;
}

function resolvedErrorCode(error: unknown): string {
  const sourceCode = errorCode(error);
  return Object.hasOwn(KNOWN_FAILURES, sourceCode)
    ? sourceCode
    : classifyModelFailure(error, sourceCode) ?? DEFAULT_ERROR_CODE;
}

function objectProperty(value: unknown, key: string): unknown {
  if (!value || (typeof value !== 'object' && typeof value !== 'function')) return undefined;
  try { return (value as Record<string, unknown>)[key]; } catch { return undefined; }
}

function validationDiagnostic(error: unknown): string | undefined {
  const wrapped = objectProperty(error, 'captureError');
  const diagnostics = objectProperty(error, 'diagnostics') ?? objectProperty(wrapped, 'diagnostics');
  const validationError = objectProperty(diagnostics, 'validationError');
  const sanitized = sanitizeToolValidationError(validationError);
  if (!sanitized) return undefined;
  const fields = sanitized.split('\n').slice(1)
    .map((line) => line.replace(/^\s*-\s*/u, '').trim())
    .filter(Boolean)
    .join('；');
  return fields.slice(0, 1_500) || undefined;
}

export function sanitizeMaterialAnalysisDiagnostic(error: unknown): string {
  const code = resolvedErrorCode(error);
  const base = KNOWN_FAILURES[code]?.currentStep ?? UNKNOWN_DIAGNOSTIC_MESSAGE;
  const validation = code === 'STRUCTURED_TOOL_VALIDATION_FAILED'
    || code === 'DOMAIN_TOOL_VALIDATION_FAILED'
    ? validationDiagnostic(error)
    : undefined;
  return `${base}${validation ? `：${validation}` : ''}`.slice(0, MAX_DIAGNOSTIC_MESSAGE_LENGTH);
}

export function describeMaterialAnalysisFailure(error: unknown): MaterialAnalysisFailure {
  const code = resolvedErrorCode(error);
  const diagnostic = sanitizeMaterialAnalysisDiagnostic(error);
  const known = KNOWN_FAILURES[code];
  return {
    errorCode: code,
    diagnosticMessage: diagnostic,
    userMessage: known?.userMessage ?? UNKNOWN_USER_MESSAGE,
    currentStep: known?.currentStep ?? '分析未完成',
  };
}
