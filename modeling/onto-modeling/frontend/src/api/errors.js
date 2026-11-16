// 统一错误体（契约 1.5）与错误码 → 中文提示（契约第 2 章）
// 服务端的 message 已是给用户看的中文短句；本表保证没有 message（反代 502、网络断开）
// 或需要按 details 拼出具体数字时，界面上的说法仍然一致。

export class ApiError extends Error {
  constructor({ status, code, message, details, traceId, raw }) {
    super(message || code || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code || 'internal_error';
    this.details = details ?? null;
    this.traceId = traceId || null;
    this.serverMessage = message || '';
    this.raw = raw;
  }

  get isConflict() { return this.status === 409 && this.code === 'revision_conflict'; }
  get isLocked() { return this.status === 423 || this.code === 'write_scope_locked'; }
  get needsConfirmation() { return this.status === 428 || this.code === 'confirmation_required'; }
  get isAuth() { return this.status === 401; }
  get isMaintenance() { return this.code === 'maintenance_read_only' || this.code === 'maintenance_no_new_sessions'; }
}

const SOURCE_LABEL = {
  assistant: '助手', web: '网页', studio: 'Studio', mcp: 'MCP 客户端', autobuild: '自主构建',
  import: '导入', plugin: '插件', revert: '回退',
};

export function relativeTime(iso, now = Date.now()) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return '刚刚';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} 分钟前`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} 小时前`;
  const d = Math.round(h / 24);
  return `${d} 天前`;
}

// 409 revision_conflict 的提示：「文件已被助手修改（2 分钟前，版本 v0.13）」
export function describeConflict(details, now = Date.now()) {
  if (!details) return '内容已被修改，请刷新对比后再保存';
  const who = SOURCE_LABEL[details.changed_by] || '其他人';
  const parts = [relativeTime(details.changed_at, now), details.version_label ? `版本 ${details.version_label}` : '']
    .filter(Boolean);
  return `文件已被${who}修改${parts.length ? `（${parts.join('，')}）` : ''}，请刷新对比后再保存`;
}

const fmtSeconds = (s) => {
  if (s == null) return '';
  if (s < 60) return `约 ${s} 秒后可以重试`;
  return `约 ${Math.ceil(s / 60)} 分钟后可以重试`;
};

// code → 中文；函数形式按 details 拼出具体内容
export const ERROR_MESSAGES = {
  // 2.1 通用
  invalid_argument: '填写的内容不符合要求，请检查后再试',
  invalid_cursor: '列表条件变了，请刷新后重试',
  cursor_stale: '列表内容已更新，正在重新加载',
  unauthenticated: '未登录或登录已失效，请重新登录',
  token_expired: '登录已过期，请重新登录',
  forbidden: '你的账号没有这个操作的权限',
  scope_forbidden: '令牌的权限范围不够',
  project_owner_only: '这个项目只归所有者，你不能打开',
  project_unowned: '这个项目还没有所有者，请联系管理员指定',
  not_found: '要找的内容不存在',
  project_not_found: '项目不存在或已被删除',
  endpoint_removed: '这个功能已下线',
  payload_too_large: (d) => (d?.limit_bytes ? `内容超过上限（${formatBytes(d.limit_bytes)}）` : '内容超过上限'),
  unsupported_media_type: (d) => (d?.allowed?.length ? `不支持这种文件类型，可以上传：${d.allowed.join(' ')}` : '不支持这种文件类型'),
  confirmation_required: '这个操作需要先确认',
  rate_limited: (d) => `请求过于频繁，请稍后再试${d?.retry_after_s != null ? `（${fmtSeconds(d.retry_after_s)}）` : ''}。`,
  // 服务端的说法带「今天 / 本月」和千分位（V 阶段 toast 是「已超出配额（上限 2000000，已用 3314435）」）
  quota_exceeded: (d, message) => message || (d?.limit != null
    ? `已超出配额（上限 ${Number(d.limit).toLocaleString('en-US')}，已用 ${d.used != null ? Number(d.used).toLocaleString('en-US') : '—'}）`
    : '已超出配额'),
  too_many_streams: '同时打开的页面太多，请关掉一些再试',
  internal_error: (d) => `服务出了点问题${d?.error_id ? `，错误编号 ${d.error_id}` : ''}，请稍后再试`,
  upstream_unavailable: '依赖的服务暂时连不上，请稍后再试',
  service_unavailable: '服务还没准备好，请稍后再试',
  maintenance_read_only: (d) => d?.message || '平台正在维护，暂时只能查看',
  maintenance_no_new_sessions: (d) => d?.message || '平台正在维护，暂停新会话',
  metadata_unavailable: '项目信息暂时读不到，请稍后再试',
  // 2.2 认证与账号
  invalid_credentials: '登录失败：用户名或密码不正确。连续失败 5 次后需要等 1 分钟再试。',
  account_locked: (d) => `连续失败次数过多，账号已暂时锁定${d?.retry_after_s != null ? `（${fmtSeconds(d.retry_after_s)}）` : ''}。`,
  refresh_invalid: '登录已过期，请重新登录',
  registration_disabled: '注册功能未开启，请联系管理员开通账号。',
  username_taken: '用户名已被占用，换一个试试',
  weak_password: '密码不满足规则',
  old_password_incorrect: '旧密码不正确',
  user_admin_unavailable: '账号服务没有配置管理令牌，暂时不能修改',
  user_not_found: '用户不存在',
  transfer_target_invalid: '移交对象不是建模者或不存在',
  invalid_owner: '所有者用户名不合法',
  // 2.3 项目、上传、文件
  project_name_required: '请输入项目名称',
  project_name_too_long: (d) => `项目名称不能超过 ${d?.max || 200} 个字符`,
  project_name_conflict: (d) => (d?.conflict?.attempted_name ? `你已经有一个叫「${d.conflict.attempted_name}」的项目` : '你已经有一个同名项目'),
  confirm_name_mismatch: '输入的名称和项目名不一致',
  project_archived: '项目已归档，只能查看',
  project_busy: '项目正在被占用，删除没有完成，请稍后再试',
  engine_unavailable: '这个引擎在当前部署里没有安装',
  archive_invalid: '压缩包无法解析，或含有不安全的路径',
  unsupported_archive_kind: '这个压缩包不能从这里导入',
  upload_not_found: '上传的文件已不存在，请重新选择',
  upload_expired: '上传的文件已过期（超过 24 小时），请重新选择',
  upload_purpose_mismatch: '上传的文件用途不对，请重新选择',
  path_forbidden: '不能访问这个路径',
  managed_path: '这个目录要在专门的页面里修改',
  file_not_found: '文件不存在',
  binary_not_previewable: '这个文件不能按文本预览，请下载查看',
  preview_unavailable: '服务端暂时不能预览这个文件',
  invalid_filename: '文件名不合法',
  // Studio 草稿基于旧版本（develop !608）：服务端的说法更具体（「正式条目已更新，请刷新后基于最新内容重新编辑」）
  revision_conflict: (d, msg) => (d?.draft_state === 'stale' && msg ? msg : describeConflict(d)),
  // 2.4 写入路径、变更集、版本
  write_scope_locked: (d) => (d?.state === 'pending_confirm'
    ? '这些条目和助手这一轮的修改有冲突，在待处理里选好保留哪边后才能编辑'
    : d?.state === 'autobuild_slice'
      ? '自主构建正在改这些条目，完成后才能编辑'
      : '助手正在改这些条目，这一轮结束后才能编辑'),
  check_rejected: '检查发现硬伤，没有保存',
  gate_failed: '门禁检查没有通过',
  changeset_not_found: '这组修改已不存在',
  changeset_in_progress: '助手还在跑，先打断或等它停下来',
  changeset_closed: '这一轮已经撤回过了',
  merge_conflict: '确认时发现冲突，已放进待处理',
  head_moved: '内容已更新，请刷新后再确认',
  invalid_commit: '版本号不合法',
  nothing_to_revert: '当前内容和目标版本一致，不用回退',
  tag_not_found: '标记不存在',
  entry_referenced: '这个条目被其他条目引用，不能删除',
  // 2.5 会话与执行
  conversation_not_found: '对话不存在或已删除',
  session_busy: '助手正在运行，停止后再试',
  message_empty: '请输入内容或添加附件',
  engine_not_installed: '会话引擎暂时不可用',
  permission_request_gone: '这个请求已处理或已超时',
  question_gone: '这个问题已回答或已失效',
  answer_invalid: '请先选择一个选项',
  not_queued: '当前不在排队',
  next_occupied: (d) => `已经排着「${d?.label || '一条命令'}」`,
  message_duplicate: '相同消息正在处理中，等这一步结束或先停止再重试',
  next_not_found: '没有排着的命令',
  exec_not_found: '执行记录不存在',
  subagent_not_found: '这个子任务的记录找不到了',
  // F7 起能力升级、输入改过时「继续」按新版重新执行，只有做完了的才拒绝
  exec_not_resumable: '这一步已经做完了',
  suggestion_stale: '情况变了，建议已更新',
  nothing_to_retry: '没有可以重试的一轮',
  title_invalid: '对话名称须为 1–60 个字符',
  // 2.6 流程与蓝图
  node_not_found: '这一步不存在',
  node_not_skippable: '主流程和必经步骤不能跳过',
  node_not_skipped: '这一步没有被跳过',
  flow_assembly_failed: '流程组装没有通过',
  blueprint_not_found: '还没有建模蓝图',
  blueprint_question_pending: '先回答蓝图里关于建模目标的问题',
  slice_invalid: '分段调整不合法',
  low_confidence_batch: '把握低的变更点不能批量接受，请单独确认',
  // 2.7 插件与产物
  pack_not_found: '找不到这个能力包',
  pack_version_not_found: '找不到这个版本',
  pack_incompatible: '兼容检查没有通过',
  pack_required: '必装的能力不能停用',
  pack_not_enabled: (d) => (d?.effective === 'next_message' ? '下一条消息生效' : '本项目没有启用这个能力'),
  pack_already_enabled: '已经启用了',
  // 私有上传与插件草稿安装（PL-22、PD-05）
  static_check_failed: '静态检查有失败项',
  pack_name_taken: '包名已被占用',
  pack_quota_exceeded: (d) => `私有包版本数到上限${d?.limit ? `（${d.limit} 个）` : ''}，找管理员清理`,
  pack_unavailable: (d) => ({
    offline: '离线部署不可用', not_applicable: '本项目不适用', not_installed: '平台还没有安装',
  }[d?.reason] || '这个能力暂时不可用'),
  page_api_unsupported: (d) => `平台页面版本不够${d?.required ? `，需要平台 ${d.required}` : ''}`,
  assembly_failed: '装配失败，项目保持原版本',
  artifact_type_unknown: '这类产物没有登记，或登记它的能力没有启用',
  artifact_not_found: '产物不存在',
  artifact_schema_invalid: '内容不符合格式要求',
  artifact_readonly: '这个产物只读',
  format_migration_required: '产物还是旧格式，要先迁移',
  knowledge_duplicate: '已有同名或同范围的知识条目',
  // 2.8 证据源
  source_not_found: '数据源不存在',
  source_not_authorized: '本项目没有获授权使用这个数据源',
  gateway_unavailable: '连接器网关连不上',
  connection_test_failed: '测试连接没有通过',
  outside_access_window: '不在访问时间窗口内',
  confirmation_expired: '确认已过期，请重新确认',
  anchor_invalid: '出处格式不合法',
  anchor_not_found: '找不到这个出处',
  // 2.10 自主构建
  autobuild_running: '自主构建正在运行，先暂停或等它结束',
  slice_reverted: '这一段已经撤回',
  autobuild_not_running: '自主构建不在可以控制的状态',
  run_not_found: '这次自主构建运行不存在',
  ledger_entry_not_found: '这条自动决定不存在',
  suspension_not_found: '这个挂起的问题不存在或已回答',
  slice_not_revertible: '这一段不能回退（未生效、已回退过，或后面的分段依赖它）',
  preview_expired: '预览已过期，请重新预览',
};

export function formatBytes(n) {
  if (n == null) return '';
  if (n >= 1024 * 1024) return `${Math.round((n / 1024 / 1024) * 10) / 10} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

const BY_STATUS = {
  0: '网络连接失败，请检查网络后重试',
  400: '请求不合法',
  401: '未登录或登录已失效，请重新登录',
  403: '没有权限',
  404: '要找的内容不存在',
  409: '内容已被修改，请刷新后重试',
  413: '内容超过上限',
  423: '内容正在被助手修改，暂时只读',
  428: '这个操作需要先确认',
  429: '请求过于频繁，请稍后再试',
  500: '服务出了点问题，请稍后再试',
  502: '依赖的服务暂时连不上，请稍后再试',
  503: '服务暂时不可用，请稍后再试',
};

// 给界面用的一句话
export function errorText(err) {
  if (!err) return '';
  if (!(err instanceof ApiError)) return err.message || BY_STATUS[0];
  const entry = ERROR_MESSAGES[err.code];
  if (typeof entry === 'function') return entry(err.details, err.serverMessage);
  if (entry) return entry;
  return err.serverMessage || BY_STATUS[err.status] || BY_STATUS[500];
}

// 解析非 2xx 响应为 ApiError；响应体不是统一错误体时按状态码兜底
export async function errorFromResponse(response) {
  let body = null;
  try {
    const text = await response.text();
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (body && typeof body === 'object' && (body.code || body.message)) {
    return new ApiError({
      status: response.status,
      code: body.code,
      message: body.message || body.error,
      details: body.details,
      traceId: body.trace_id,
      raw: body,
    });
  }
  return new ApiError({ status: response.status, code: statusCode(response.status), message: '' });
}

function statusCode(status) {
  return {
    400: 'invalid_argument', 401: 'unauthenticated', 403: 'forbidden', 404: 'not_found', 413: 'payload_too_large',
    429: 'rate_limited', 502: 'upstream_unavailable', 503: 'service_unavailable',
  }[status] || 'internal_error';
}
