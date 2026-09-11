import type { FastifyError, FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';

import { mapArtifactHttpError } from './artifacts/http-error.js';
import { mapEvaluationHttpError } from './evaluation/http-error.js';

/**
 * HTTP 层只负责把业务异常转换成稳定的公开响应。
 *
 * 业务模块之间不应该互相 import 路由文件来处理错误，否则很容易出现：
 * - 某个异常没有 statusCode，Fastify 默认按 500 返回；
 * - 只有同步分支被 try/catch，Promise rejection 直接落到默认错误页；
 * - 内部异常 message 被原样写进响应。
 *
 * 这里故意只依赖错误对象的公开字段（name/code/statusCode/details/next），
 * 不依赖具体业务服务，避免循环依赖。各路由仍可在此之前提供更丰富的专用
 * payload；根级 handler 负责兜底和脱敏。
 */

export interface HttpErrorContext {
  /** 当前操作的稳定错误码（仅在异常没有 code 时使用）。 */
  code?: string;
  /** 未知异常的日志标签。 */
  logMessage?: string;
  /** 未知异常对外展示的固定文案。 */
  message?: string;
  /** 未知异常的 HTTP 状态；默认 500。 */
  statusCode?: number;
  /** 是否把业务异常的 message 放入 reason/display。 */
  includeReason?: boolean;
}

export interface NormalizedHttpError {
  statusCode: number;
  code: string;
  message: string;
  /** 经过白名单/脱敏后的附加字段。 */
  details: Record<string, unknown>;
  next: string[];
  includeReason: boolean;
}

const INTERNAL_MESSAGE = '服务暂时不可用，请稍后重试';
const DEFAULT_MESSAGES: Record<number, string> = {
  400: '请求内容不正确，请检查后重试',
  401: '登录状态已失效，请重新登录',
  403: '没有权限执行此操作',
  404: '请求的内容不存在',
  406: '请求格式不符合接口要求',
  409: '内容已更新或操作冲突，请刷新后重试',
  413: '请求内容过大，请缩小后重试',
  415: '请求格式不受支持',
  422: '提交内容未通过校验，请检查后重试',
  429: '请求过于频繁，请稍后重试',
  502: '上游服务暂时不可用，请稍后重试',
  503: INTERNAL_MESSAGE,
  504: '上游服务响应超时，请稍后重试',
};

/** 没有显式 statusCode 的稳定错误码。 */
const CODE_STATUS: Record<string, number> = {
  // 认证与权限
  AUTH_REQUIRED: 401,
  TOKEN_REQUIRED: 401,
  EVALUATION_AUTH_REQUIRED: 401,
  MCP_AUTH_REQUIRED: 401,
  MCP_TOKEN_INVALID: 401,
  MCP_TOKEN_EXPIRED: 401,
  MCP_TOKEN_REVOKED: 401,
  MCP_TOKEN_UNKNOWN: 401,
  PASSWORD_CHANGE_REQUIRED: 403,
  FORBIDDEN: 403,
  UNAUTHORIZED: 401,
  AGENT_SERVICE_FORBIDDEN: 403,
  MCP_ORIGIN_FORBIDDEN: 403,
  MCP_PROJECT_FORBIDDEN: 403,
  MCP_SCOPE_FORBIDDEN: 403,
  MCP_SCOPE_MISMATCH: 403,

  // 资源不存在
  PROJECT_NOT_FOUND: 404,
  GRAPH_NOT_FOUND: 404,
  TASK_NOT_FOUND: 404,
  MILESTONE_NOT_FOUND: 404,
  RUN_NOT_FOUND: 404,
  INTERACTION_NOT_FOUND: 404,
  DOCUMENT_NOT_FOUND: 404,
  DOCUMENT_INVALID_DIRECTIVES: 400,
  DOCUMENT_REVISION_CONFLICT: 409,
  EVALUATION_NOT_FOUND: 404,
  FILE_NOT_FOUND: 404,
  RESOURCE_NOT_FOUND: 404,
  OBJECT_NOT_FOUND: 404,
  OPERATION_NOT_FOUND: 404,
  RULE_NOT_FOUND: 404,
  CHECK_FINDING_NOT_FOUND: 404,
  VALIDATION_REPORT_NOT_FOUND: 404,
  CHANGE_PLAN_NOT_FOUND: 404,
  CHANGE_SET_NOT_FOUND: 404,
  TABLE_NOT_FOUND: 404,
  EVIDENCE_NOT_FOUND: 404,
  SCRIPT_CONTRACT_NOT_FOUND: 404,
  MATERIAL_IMPACT_NOT_FOUND: 404,
  MATERIAL_IMPACT_RESOURCE_NOT_FOUND: 404,
  MODELING_BRIEF_NOT_FOUND: 404,
  PACKAGE_DRAFT_NOT_FOUND: 404,
  PACKAGE_DRAFT_FILE_NOT_FOUND: 404,
  PACKAGE_SOURCE_NOT_FOUND: 404,
  FUSION_PLAN_NOT_FOUND: 404,
  FUSION_SOURCE_NOT_FOUND: 404,
  DIAGRAM_NOT_FOUND: 404,
  FLOW_NODE_NOT_FOUND: 404,
  GATE_NOT_FOUND: 404,
  ELEMENT_NOT_FOUND: 404,
  MCP_ENDPOINT_NOT_FOUND: 404,
  MCP_PROJECT_NOT_FOUND: 404,
  MCP_TOOL_NOT_FOUND: 404,
  PROJECT_TRANSFER_JOB_NOT_FOUND: 404,

  // 并发、过期和前置状态
  REVISION_CONFLICT: 409,
  WORKING_REVISION_CHANGED: 409,
  STALE_CONFIRMATION: 409,
  DELETE_PREVIEW_STALE: 409,
  RESOURCE_HASH_CONFLICT: 409,
  ITEM_HASH_CONFLICT: 409,
  IDEMPOTENCY_CONFLICT: 409,
  CHANGE_PLAN_ALREADY_ACTIVE: 409,
  CHANGE_PLAN_CLOSED: 409,
  CHANGE_PLAN_DECISION_CONFLICT: 409,
  CHANGE_PLAN_VERSION_CONFLICT: 409,
  CHANGE_PLAN_STALE_RUN: 409,
  CHANGE_PLAN_TASK_ALREADY_SETTLED: 409,
  CHANGE_PLAN_APPROVAL_REQUIRED: 409,
  CHANGE_PLAN_INCOMPLETE: 409,
  CHANGE_PLAN_REQUIRES_CHANGE_SET: 409,
  CHANGE_PLAN_SCOPE_APPROVAL_REQUIRED: 409,
  CHANGE_SET_FROZEN: 409,
  CHANGE_SET_OUT_OF_SCOPE: 409,
  CHANGE_SET_RESULT_UNKNOWN: 409,
  CHANGE_SET_DEPENDENCY_MISSING: 409,
  CHANGE_SET_ATOMIC_ROLLBACK: 503,
  CHANGE_SET_TEMPORARILY_UNAVAILABLE: 503,
  OPERATION_ALREADY_RUNNING: 409,
  OPERATION_ALREADY_EXISTS: 409,
  RULE_ALREADY_EXISTS: 409,
  RESOURCE_ALREADY_EXISTS: 409,
  CANDIDATE_CONFLICT: 409,
  CANDIDATE_REVALIDATION_REQUIRED: 409,
  CANDIDATE_DECISION_REQUIRED: 409,
  CANDIDATE_NOT_PENDING: 409,
  NO_CHANGES: 409,
  MILESTONE_NOT_READY: 409,
  INTERACTION_CLOSED: 409,
  MATERIAL_ANALYSIS_PACKAGE_STALE: 409,
  MATERIAL_ANALYSIS_DOCUMENTS_STALE: 409,
  MATERIAL_IMPACT_SCOPE_ALREADY_RESOLVED: 409,
  INCREMENT_ROUND_BLOCKED: 409,
  INCREMENT_ROUND_NOT_STARTED: 409,
  PACKAGE_DRAFT_REVISION_CONFLICT: 409,
  PACKAGE_DRAFT_BASE_REVISION_STALE: 409,
  PACKAGE_DRAFT_BASE_VERSION_STALE: 409,
  PACKAGE_DRAFT_REQUIRES_PUBLISH: 409,
  PACKAGE_DRAFT_NO_CHANGES: 409,
  PACKAGE_NOT_EDITABLE: 409,
  PACKAGE_REVISION_MISSING: 409,
  PACKAGE_REVISION_REQUIRED: 409,
  PACKAGE_VERSION_INVALID: 400,
  PACKAGE_VERSION_CONFLICT: 409,
  PACKAGE_NAME_CONFLICT: 409,
  PACKAGE_REPAIR_IN_PROGRESS: 409,
  RELEASE_VERSION_MISMATCH: 409,
  RELEASE_GATE_BLOCKED: 409,
  FUSION_DECISIONS_REQUIRED: 409,
  FUSION_INVALID_VERSION: 409,
  FUSION_INVALID_PLAN: 400,
  FUSION_PLAN_IMMUTABLE: 409,
  FUSION_PLAN_NOT_READY: 409,
  FUSION_QUALITY_FAILED: 409,
  FUSION_REFERENCE_NOT_CLOSED: 409,
  FUSION_SCOPE_NOT_CLOSED: 409,
  FUSION_DUPLICATE_SOURCE: 409,
  FUSION_NO_CHANGES: 409,
  PROJECT_BUSY: 409,
  PROJECT_EDITING_BUSY: 409,
  PROJECT_WRITE_BUSY: 409,
  GRAPH_IMPORT_ALREADY_RUNNING: 409,
  GRAPH_IMPORT_PACKAGE_STALE: 409,
  GRAPH_ISOLATION_DATA_EXISTS: 409,
  RELEASE_CANDIDATE_REQUIRED: 409,
  PACKAGE_CANDIDATE_REJECTED: 422,
  FUSION_REVISION_CONFLICT: 409,
  FUSION_SOURCE_CHANGED: 409,
  FUSION_WORKSPACE_BUSY: 409,

  // 输入/校验
  INVALID_INPUT: 400,
  INVALID_GOAL_INPUT: 400,
  INVALID_PROCESS_INTERVIEW_INPUT: 400,
  INVALID_AGENT_CONTEXT: 400,
  INVALID_JSON: 400,
  INVALID_TRANSITION: 409,
  INVALID_TASK_PLAN: 400,
  INVALID_WORKING_PACKAGE: 400,
  GRAPH_IMPORT_INPUT: 400,
  GRAPH_IMPORT_EMPTY_PLAN: 422,
  GRAPH_IMPORT_WRITE_VERIFICATION_FAILED: 502,
  GRAPH_IMPORT_EMPTY_SHEET: 422,
  GRAPH_IMPORT_NO_IMPORTABLE_ROWS: 422,
  GRAPH_IMPORT_NO_MAPPED_COLUMNS: 422,
  GRAPH_IMPORT_NO_OBJECT_MAPPING: 422,
  DOMAIN_PATCH_INVALID: 400,
  DOMAIN_ACTIONS_INVALID: 400,
  DOMAIN_ACTION_NOT_ALLOWED: 409,
  DOMAIN_ITEM_NOT_FOUND: 404,
  DOMAIN_REFERENCE_NOT_FOUND: 404,
  DOMAIN_ITEM_EXISTS: 409,
  DOMAIN_ITEM_AMBIGUOUS: 409,
  DOMAIN_ACTION_CONFLICT: 409,
  DOMAIN_TOOL_VALIDATION_FAILED: 422,
  OBJECT_MAPPING_INVALID: 422,
  SCHEMA_OBJECT_MISSING: 422,
  SCHEMA_MAPPING_MISSING: 422,
  SCHEMA_SHEET_MAPPING_INCOMPLETE: 422,
  SCHEMA_SHEET_OBJECT_MAPPING_MISSING: 422,
  SCHEMA_SHEET_OBJECT_MAPPING_DUPLICATED: 422,
  SCHEMA_OBJECT_DEFINITION_MISMATCH: 422,
  SCHEMA_OBJECT_FIELDS_MISMATCH: 422,
  SCHEMA_OBJECT_SOURCE_MISMATCH: 422,
  SCHEMA_SHEET_DISPOSITION_MISMATCH: 422,
  PACKAGE_INTEGRITY_FAILED: 422,
  PACKAGE_CHECK_FAILED: 422,
  PACKAGE_CHECK_REQUIRED: 409,
  PACKAGE_DRAFT_PARSE_FAILED: 422,
  PACKAGE_DRAFT_VALIDATION_FAILED: 422,
  PACKAGE_DRAFT_FILE_NOT_PUBLISHABLE: 422,
  CANDIDATE_VALIDATION_FAILED: 422,
  VALIDATION_RETRY_EXHAUSTED: 422,
  SUBMISSION_BLOCKED: 422,
  KNOWLEDGE_CONTRACT_INVALID: 422,
  KNOWLEDGE_FILE_INVALID: 422,
  KNOWLEDGE_EVIDENCE_REQUIRED: 422,
  TABULAR_MATERIAL_REQUIRED: 422,
  PHYSICAL_SCHEMA_REQUIRED: 422,
  SCRIPT_TEST_REQUIRED: 422,
  SCRIPT_SCOPE_NOT_CONFIRMED: 409,
  SCRIPT_CONTRACT_CONFLICT: 409,
  SCRIPT_OPERATION_ALREADY_BOUND: 409,
  STUDIO_FIELD_INVALID: 400,
  STUDIO_WRITE_LOCKED: 409,
  MATERIAL_PURPOSE_REQUIRED: 409,
  MATERIAL_PURPOSE_REVIEW_REQUIRED: 409,
  MATERIAL_PURPOSE_REVIEW_INVALID: 400,
  MATERIAL_PURPOSE_REVIEW_CONFLICT: 409,
  // 上游/模型
  AGENT_UNAVAILABLE: 503,
  CORE_UNAVAILABLE: 503,
  MODEL_SERVICE_UNAVAILABLE: 503,
  MODEL_QUOTA_EXHAUSTED: 429,
  MODEL_RATE_LIMITED: 429,
  MODEL_REQUEST_TIMEOUT: 504,
  READ_TASK_TIMEOUT: 504,
  MODEL_CONTEXT_LENGTH_EXCEEDED: 422,
  LLM_CONFIG_UNAVAILABLE: 503,
  LLM_CONFIG_INVALID: 502,
  EVAL_DB_TOOL_UNAVAILABLE: 503,
  EVAL_DB_QUERY_FAILED: 502,
  EVAL_DB_ADMIN_UNAVAILABLE: 503,
  DATA_SOURCE_INVALID_INPUT: 400,
  DATA_SOURCE_PACKAGE_REQUIRED: 409,
  DATA_SOURCE_CONNECTION_FAILED: 502,
  DATA_SOURCE_SYNC_FAILED: 502,
  EVALUATION_DATABASE_IMPORT_CAPACITY_EXCEEDED: 429,
  EVALUATION_DATABASE_IMPORT_ALREADY_RUNNING: 409,
  EVALUATION_NOT_STARTED: 409,
  EVALUATION_START_FAILED: 502,
  EVALUATION_RUN_FAILED: 502,
  MCP_SESSION_CAPACITY_REACHED: 503,
  MCP_SESSION_TOKEN_LIMIT_REACHED: 429,
  MCP_SESSION_PROJECT_LIMIT_REACHED: 429,
  MCP_SESSION_USER_LIMIT_REACHED: 429,
  MCP_SESSION_IP_LIMIT_REACHED: 429,
  MCP_SESSION_PRINCIPAL_MISMATCH: 403,
  MCP_SCOPE_INVALID: 400,
  MCP_TOKEN_NAME_REQUIRED: 400,
  MCP_TOKEN_NAME_TOO_LONG: 400,
  MCP_TOKEN_TTL_INVALID: 400,
  MCP_TOKEN_SECRET_INVALID: 503,
  MCP_INTERNAL_ERROR: 500,
  FILE_READ_TEMPORARILY_UNAVAILABLE: 503,
  SANDBOX_UNAVAILABLE: 503,
  AGENT_RUN_CREATE_RETRYABLE: 503,
  LLM_NOT_CONFIGURED: 409,
};

/** 历史工件/租约层保留的小写码，不能交给大写后缀规则处理。 */
const LOWERCASE_CODE_STATUS: Record<string, number> = {
  context_length_exceeded: 422,
  invalid_input: 400,
  invalid_project: 400,
  invalid_project_transfer: 400,
  invalid_document: 400,
  invalid_path: 400,
  not_found: 404,
  project_not_found: 404,
  project_busy: 409,
  package_not_found: 404,
  file_not_found: 404,
  revision_not_found: 404,
  not_initialized: 404,
  candidate_not_found: 404,
  revision_conflict: 409,
  busy: 409,
  expired: 409,
  not_owner: 409,
  commit_in_progress: 409,
  candidate_not_pending: 409,
  candidate_validation_failed: 422,
  no_change: 409,
  empty_change: 400,
  workspace_not_empty: 409,
  invalid_repository: 503,
};

const NAME_STATUS: Record<string, number> = {
  ProjectLeaseError: 409,
  AuthoringStateError: 409,
  ControlledRepairControllerError: 409,
  FlowchartDraftError: 409,
  MermaidImportError: 400,
  GraphImportInputError: 400,
  GraphImportPackageStaleError: 409,
  GraphImportAlreadyRunningError: 409,
  GraphIsolationDataExistsError: 409,
  EvaluationDatabaseImportAlreadyRunningError: 409,
  EvaluationDatabaseImportCapacityError: 429,
  ProjectTransferJobNotFoundError: 404,
  OperationAlreadyRunningError: 409,
  DocumentReadOffsetError: 400,
  DocumentReadSummaryRequiredError: 400,
  DocumentReadIncompleteError: 409,
  DocumentReadStalledError: 409,
  LlmDefaultRevisionConflictError: 409,
  LlmDefaultValidationError: 400,
  UserLlmSettingsValidationError: 400,
  LlmExtraParamsValidationError: 400,
  LlmConfigEncryptionKeyError: 503,
  LlmDefaultSyncError: 503,
  PackageCandidateValidationError: 422,
  PackageCandidateRejectedError: 422,
  OptimizationExpertReviewRequiredError: 409,
  PackageDraftParseError: 422,
  ArtifactRevisionIndexError: 400,
  PackageFusionServiceError: 400,
  ProjectTransferError: 400,
  ModelingMcpError: 400,
  ModelingMcpToolError: 400,
  AgentCoreError: 409,
  ChangeControlError: 409,
  StudioReadError: 400,
  StudioWriteError: 400,
};

const CODE_MESSAGE: Record<string, string> = {
  EVALUATION_AUTH_REQUIRED: '登录状态已失效，请重新登录后再运行评测',
  PROJECT_NOT_FOUND: '项目不存在',
  RESOURCE_NOT_FOUND: '请求的内容不存在',
  FILE_NOT_FOUND: '文件不存在',
  MCP_INTERNAL_ERROR: 'MCP 服务内部错误',
  MCP_AUTH_REQUIRED: '缺少 Bearer Token',
  MCP_TOKEN_INVALID: 'MCP Token 无效或已过期',
  MCP_TOKEN_EXPIRED: 'MCP Token 已过期',
  MCP_TOKEN_REVOKED: 'MCP Token 已吊销',
  MCP_TOKEN_UNKNOWN: 'MCP Token 未登记或已失效',
  AGENT_UNAVAILABLE: '建模助手暂时不可用',
  CORE_UNAVAILABLE: '建模核心暂时不可用',
  MODEL_REQUEST_TIMEOUT: '上游服务响应超时，请稍后重试',
  MODEL_SERVICE_UNAVAILABLE: '模型服务暂时不可用，请稍后重试',
  MODEL_QUOTA_EXHAUSTED: '模型额度已用尽，请稍后重试或切换模型',
  MODEL_RATE_LIMITED: '模型请求过于频繁，请稍后重试',
  MODEL_CONTEXT_LENGTH_EXCEEDED: '模型上下文长度超出限制，请减少资料范围或输出长度后重试',
  context_length_exceeded: '模型上下文长度超出限制，请减少资料范围或输出长度后重试',
  LLM_NOT_CONFIGURED: '尚未配置可用的建模模型',
  LLM_CONFIG_UNAVAILABLE: '模型配置暂时不可用，请稍后重试',
  EVAL_DB_TOOL_UNAVAILABLE: '评测数据服务暂时不可用，请稍后重试',
  EVAL_DB_QUERY_FAILED: '评测数据服务暂时不可用，请稍后重试',
  EVALUATION_DATABASE_IMPORT_CAPACITY_EXCEEDED: '当前已有评测库导入任务正在执行，请等待完成后重试',
  EVALUATION_DATABASE_IMPORT_ALREADY_RUNNING: '当前项目已有评测库导入任务正在后台执行',
  MCP_SESSION_CAPACITY_REACHED: 'MCP 会话容量已满，请稍后重试',
  MCP_SESSION_TOKEN_LIMIT_REACHED: '当前 Token 的 MCP 会话数已达上限',
  MCP_SESSION_PROJECT_LIMIT_REACHED: '当前项目的 MCP 会话数已达上限',
  MCP_SESSION_USER_LIMIT_REACHED: '当前用户的 MCP 会话数已达上限',
  MCP_SESSION_IP_LIMIT_REACHED: '当前来源地址的 MCP 会话数已达上限',
  MCP_SCOPE_FORBIDDEN: 'MCP Token 不具备执行此操作的权限',
  MCP_PROJECT_FORBIDDEN: '当前用户已无权访问该项目',
  MCP_ENDPOINT_NOT_FOUND: 'MCP endpoint 不存在',
  MCP_PROJECT_NOT_FOUND: '项目不存在',
  MCP_TOOL_NOT_FOUND: 'MCP 工具不存在',
  CHANGE_PLAN_NOT_FOUND: '建模计划不存在',
  CHANGE_SET_NOT_FOUND: 'Change Set 不存在',
  TABLE_NOT_FOUND: '数据表不存在',
  EVIDENCE_NOT_FOUND: '证据不存在',
  SCRIPT_CONTRACT_NOT_FOUND: '脚本契约不存在',
  FUSION_PLAN_NOT_FOUND: '融合计划不存在',
  FUSION_SOURCE_NOT_FOUND: '来源知识包不存在',
  PACKAGE_DRAFT_NOT_FOUND: '知识包草稿不存在',
  PACKAGE_DRAFT_FILE_NOT_FOUND: '知识包草稿文件不存在',
  MILESTONE_NOT_READY: '当前里程碑尚未满足完成条件',
  NO_CHANGES: '当前内容没有产生修改',
  CANDIDATE_CONFLICT: '候选修订状态已变化，请刷新后重试',
  FST_PARTS_LIMIT: '上传内容包含过多分段',
  FST_FILES_LIMIT: '一次上传的文件数超过限制',
  FST_FIELDS_LIMIT: '上传表单的字段数超过限制',
  FST_REQ_FILE_TOO_LARGE: '上传文件过大，请缩小后重试',
  FST_PROTO_VIOLATION: '上传表单包含不允许的字段名',
  FST_INVALID_MULTIPART_CONTENT_TYPE: '请使用 multipart/form-data 上传文件',
  FST_INVALID_JSON_FIELD_ERROR: '上传表单中的 JSON 字段不正确',
  INTERNAL_ERROR: INTERNAL_MESSAGE,
};

const SENSITIVE_KEY = /(?:authorization|cookie|password|secret|token|api[_-]?key|stack|trace|sql|technical|cause)/iu;
const SAFE_CODE = /^[A-Za-z][A-Za-z0-9_.:-]{0,127}$/u;

// 仅对明确属于建模业务域的码启用后缀推断。数据库/网络库的 errno
// （如 SQLITE_BUSY、ECONNRESET）不能因为恰好带有 _BUSY/_RESET 就变成
// 一个可重试的业务 409，否则会再次把内部故障伪装成客户端错误。
const BUSINESS_CODE_PREFIX = /^(?:AGENT|AUTH|CANDIDATE|CHANGE|CHECK|CONTROLLED|DOMAIN|DOCUMENT|EVAL|EVALUATION|FILE|FLOW|FUSION|GATE|GENERATION|GRAPH|INCREMENT|INTERACTION|INVALID|KNOWLEDGE|LEASE|LLM|MATERIAL|MCP|MERMAID|MILESTONE|MODEL|MODELING|OBJECT|OPERATION|OPTIMIZATION|PACKAGE|PASSWORD|PHYSICAL|PROJECT|READ|RELEASE|REPAIR|RESOURCE|REVISION|RULE|RUN|SANDBOX|SCHEMA|SCRIPT|STALE|STUDIO|SUBMISSION|TABLE|TASK|TOOL|UNIFIED|VALIDATION|WORKING|WRITE)(?:_|$)/u;

const FASTIFY_PUBLIC_CODE = /^(?:FST_ERR_(?:VALIDATION|BAD_URL|NOT_FOUND|CTP_(?:BODY_TOO_LARGE|EMPTY_JSON_BODY|INVALID_JSON_BODY|INVALID_MEDIA_TYPE|INVALID_CONTENT_LENGTH))|FST_ERR_BAD_STATUS_CODE|FST_(?:PARTS_LIMIT|FILES_LIMIT|FIELDS_LIMIT|REQ_FILE_TOO_LARGE|PROTO_VIOLATION|INVALID_MULTIPART_CONTENT_TYPE|INVALID_JSON_FIELD_ERROR))$/u;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function errorName(error: unknown): string {
  return isRecord(error) ? text(error.name) : '';
}

function errorCode(error: unknown): string {
  return isRecord(error) ? text(error.code) : '';
}

function validStatus(value: unknown): number | null {
  const status = Number(value);
  return Number.isInteger(status) && status >= 400 && status <= 599 ? status : null;
}

function compactMessage(value: unknown, maxLength = 2_000): string {
  if (typeof value !== 'string') return '';
  const normalized = value
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/gu, ' ')
    .replace(/\s+/gu, ' ')
    .trim();
  if (!normalized || /^internal server error$/iu.test(normalized)) return '';
  return normalized.length <= maxLength ? normalized : `${normalized.slice(0, maxLength - 3)}...`;
}

function safeDetails(value: unknown, depth = 0): Record<string, unknown> {
  if (!isRecord(value) || depth > 5) return {};
  const output: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (SENSITIVE_KEY.test(key)) continue;
    if (depth >= 5) {
      output[key] = '<redacted>';
    } else if (Array.isArray(item)) {
      output[key] = item.slice(0, 100).map((entry) => (
        isRecord(entry) ? safeDetails(entry, depth + 1) : entry
      ));
    } else if (isRecord(item)) {
      output[key] = safeDetails(item, depth + 1);
    } else if (typeof item !== 'function' && typeof item !== 'symbol') {
      output[key] = item;
    }
  }
  return output;
}

function nextActions(error: unknown): string[] {
  if (!isRecord(error) || !Array.isArray(error.next)) return [];
  return error.next
    .filter((item): item is string => typeof item === 'string')
    .map((item) => compactMessage(item, 500))
    .filter(Boolean)
    .slice(0, 20);
}

function statusForCode(code: string): number | null {
  if (LOWERCASE_CODE_STATUS[code] !== undefined) return LOWERCASE_CODE_STATUS[code];
  if (CODE_STATUS[code]) return CODE_STATUS[code];
  // 这些后缀是跨模块共用的稳定约定，只在明确的全大写业务码上启用。
  if (!/^[A-Z][A-Z0-9_]*$/u.test(code) || !BUSINESS_CODE_PREFIX.test(code)) return null;
  if (/_NOT_FOUND$/u.test(code)) return 404;
  if (/(?:_CONFLICT|_STALE|_BUSY|_LOCKED|_ALREADY_RUNNING)$/u.test(code)) return 409;
  if (/_FORBIDDEN$/u.test(code)) return 403;
  if (/(?:_TIMEOUT|_TIMED_OUT)$/u.test(code)) return 504;
  if (/(?:_UNAVAILABLE|_UNREACHABLE)$/u.test(code)) return 503;
  if (/^INVALID_/u.test(code)) return 400;
  if (/(?:_INVALID|_REQUIRED|_MISSING)$/u.test(code)) return 400;
  return null;
}

function isKnownCode(code: string): boolean {
  return Boolean(code && (
    LOWERCASE_CODE_STATUS[code] !== undefined
    || CODE_STATUS[code] !== undefined
    || BUSINESS_CODE_PREFIX.test(code)
  ));
}

function statusForError(error: unknown, code: string, context: HttpErrorContext): number {
  const explicit = validStatus(isRecord(error) ? (error.statusCode ?? error.status) : undefined);
  // 只有已知业务错误或 Fastify 自带的公开协议错误才能携带自己的状态码。
  // 任意底层 Error 即使偶然挂了 statusCode，也不能借此把数据库/网络故障
  // 伪装成 4xx；调用方若确实完成了输入分类，可以显式传 context.statusCode。
  if (explicit !== null && (isKnownStructuredError(error) || isKnownCode(code)
    || FASTIFY_PUBLIC_CODE.test(errorCode(error)))) return explicit;
  const byCode = statusForCode(code);
  if (byCode !== null) return byCode;
  const byName = NAME_STATUS[errorName(error)];
  if (byName !== undefined) return byName;
  return validStatus(context.statusCode) ?? 500;
}

function isKnownStructuredError(error: unknown): boolean {
  const name = errorName(error);
  return [
    'AgentCoreError',
    'ChangeControlError',
    'PackageFusionServiceError',
    'StudioReadError',
    'StudioWriteError',
    'PackageDraftError',
    'ModelingMcpToolError',
    'ModelingMcpError',
    'GraphImportInputError',
    'GraphImportPackageStaleError',
    'GraphImportAlreadyRunningError',
    'GraphIsolationDataExistsError',
    'AuthoringStateError',
    'ProjectLeaseError',
    'ArtifactRevisionIndexError',
    'ProjectTransferError',
    'OptimizationExpertReviewRequiredError',
    'PackageCandidateRejectedError',
    'PackageCandidateValidationError',
    'DocumentReadOffsetError',
    'DocumentReadSummaryRequiredError',
    'DocumentReadIncompleteError',
    'DocumentReadStalledError',
    'LlmDefaultValidationError',
    'LlmDefaultRevisionConflictError',
    'UserLlmSettingsValidationError',
    'LlmExtraParamsValidationError',
    'LlmConfigEncryptionKeyError',
    'LlmDefaultSyncError',
  ].includes(name);
}

/**
 * 将任意异常归一化为 HTTP 响应信息。此函数不抛异常，适合直接放进
 * Fastify setErrorHandler；未知异常永远使用通用 500 文案。
 */
export function normalizeHttpError(
  error: unknown,
  context: HttpErrorContext = {},
): NormalizedHttpError {
  const artifact = mapArtifactHttpError(error);
  if (artifact) {
    return {
      statusCode: artifact.statusCode,
      code: artifact.code,
      message: artifact.message,
      details: {},
      next: [],
      includeReason: context.includeReason ?? false,
    };
  }

  const evaluation = mapEvaluationHttpError(error);
  if (evaluation) {
    return {
      statusCode: evaluation.statusCode,
      code: evaluation.code,
      message: evaluation.message,
      details: {},
      next: [],
      includeReason: context.includeReason ?? false,
    };
  }

  const rawCode = errorCode(error);
  const code = SAFE_CODE.test(rawCode) ? rawCode : '';
  const statusCode = statusForError(error, code, context);
  const known = isKnownStructuredError(error) || isKnownCode(code);
  const hasExplicitPublicStatus = validStatus(isRecord(error) ? (error.statusCode ?? error.status) : undefined) !== null;
  const shouldExposeMessage = statusCode < 500 && (known || (hasExplicitPublicStatus && (
    FASTIFY_PUBLIC_CODE.test(errorCode(error))
    || Boolean(context.statusCode)
  )));
  const message = statusCode >= 500
    ? (CODE_MESSAGE[code] && code !== 'INTERNAL_ERROR'
      ? CODE_MESSAGE[code]
      : (context.message || DEFAULT_MESSAGES[statusCode] || INTERNAL_MESSAGE))
    : (CODE_MESSAGE[code] || (shouldExposeMessage ? compactMessage(isRecord(error) ? error.message : '') : '')
      || context.message || DEFAULT_MESSAGES[statusCode] || '请求处理失败');

  // 未知异常不沿用 Node/Fastify 的 errno；对外统一使用 INTERNAL_ERROR。
  const publicCode = statusCode >= 500 && !known && code !== 'INTERNAL_ERROR'
    ? (context.code && SAFE_CODE.test(context.code) ? context.code : 'INTERNAL_ERROR')
    : (code || context.code || (statusCode >= 500 ? 'INTERNAL_ERROR' : `HTTP_${statusCode}`));
  const details = statusCode < 500 && known && isRecord(error) ? safeDetails(error.details) : {};
  return {
    statusCode,
    code: SAFE_CODE.test(publicCode) ? publicCode : 'INTERNAL_ERROR',
    message,
    details,
    next: statusCode < 500 ? nextActions(error) : [],
    includeReason: context.includeReason ?? known,
  };
}

export function httpErrorPayload(normalized: NormalizedHttpError): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    code: normalized.code,
    error: normalized.message,
  };
  if (normalized.includeReason && normalized.statusCode < 500) {
    payload.reason = normalized.message;
    payload.display = { text: normalized.message };
  }
  if (normalized.next.length) payload.next = normalized.next;
  for (const [key, value] of Object.entries(normalized.details)) {
    if (key === 'code' || key === 'error' || key === 'reason' || key === 'display') continue;
    payload[key] = value;
  }
  return payload;
}

export function sendHttpError(
  reply: FastifyReply,
  error: unknown,
  context: HttpErrorContext = {},
): FastifyReply {
  const normalized = normalizeHttpError(error, context);
  if (normalized.statusCode >= 500) {
    reply.log.error({ err: error }, context.logMessage || 'HTTP 请求执行失败');
  }
  return reply.code(normalized.statusCode).send(httpErrorPayload(normalized));
}

/** 在所有业务路由注册前安装根级兜底，覆盖异步 handler rejection。 */
export function installHttpErrorHandler(app: FastifyInstance): void {
  app.setErrorHandler((error: FastifyError, request: FastifyRequest, reply: FastifyReply) => {
    if (reply.sent) return reply;
    return sendHttpError(reply, error, {
      logMessage: `${request.method} ${request.url} 请求执行失败`,
    });
  });
}

export { INTERNAL_MESSAGE };
