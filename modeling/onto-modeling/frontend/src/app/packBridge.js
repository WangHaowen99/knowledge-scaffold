// 上传包 iframe 编辑器与平台之间的窄接口（界面接口 1.0，ADR-0055；契约「包自带界面」一节）。
// iframe 是不同源的（sandbox 只给 allow-scripts），拿不到登录态、发不了请求，只能 postMessage 到这里；
// 平台按下面的白名单代办，越权的、认不出的一律拒绝。只做消息校验与分派，不碰 DOM，可以单测。
//
// 请求：{onto_ui: "1.0", id, method, params}；回应：{onto_ui, id, ok: true, result} 或 {onto_ui, id, ok: false, error}；
// 平台主动推：{onto_ui, event: "artifact.changed" | "readonly.changed", data}。
import { UI_API } from './packUi.js';

export { UI_API };
export const METHODS = ['context.get', 'artifact.read', 'artifact.save', 'chat.command', 'theme.get'];
// 主题变量：只给设计系统里的颜色、圆角、字体这几样
export const THEME_VARS = ['--ink', '--sub', '--muted', '--faint', '--line', '--panel', '--panel2', '--bg', '--blue', '--blue3',
  '--teal', '--amber', '--red', '--green', '--radius', '--font'];

const ID_MAX = 64;
const KEY_RE = /^[^/\\\u0000-\u001f]{1,120}$/;
const TEXT_MAX = 2000;
const SUMMARY_MAX = 200;
const SAVE_KEYS = ['base_revision', 'fields', 'sections', 'content', 'summary'];

export class BridgeError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.code = code;
    Object.assign(this, extra);
  }
}

const isObject = (v) => !!v && typeof v === 'object' && !Array.isArray(v);
const fail = (code, message) => { throw new BridgeError(code, message); };

/**
 * 校验一条来自 iframe 的消息。返回 null 表示不是窄接口消息（忽略，不回）；
 * 不合法时抛 BridgeError（id 认得出就回错误）。
 */
export function parseRequest(data) {
  if (!isObject(data) || !('onto_ui' in data)) return null;
  const id = typeof data.id === 'string' && data.id.length > 0 && data.id.length <= ID_MAX ? data.id : null;
  const err = (code, message) => { throw new BridgeError(code, message, { id }); };
  if (!id) err('invalid_message', '消息要带 id');
  const major = String(data.onto_ui).split('.')[0];
  if (major !== UI_API.split('.')[0]) err('ui_api_unsupported', `平台的界面接口是 ${UI_API}`);
  if (typeof data.method !== 'string' || !METHODS.includes(data.method)) err('method_unknown', `不认识的方法：${String(data.method).slice(0, 40)}`);
  if (data.params !== undefined && !isObject(data.params)) err('invalid_argument', 'params 要是对象');
  return { id, method: data.method, params: data.params || {} };
}

// 只认本包登记的产物类型：类型前缀就是包名
function targetOf(params, ctx) {
  const type = params.type === undefined ? ctx.artifact.type : params.type;
  const key = params.key === undefined ? ctx.artifact.key : params.key;
  if (typeof type !== 'string' || !ctx.types.includes(type)) fail('forbidden', '只能读写本插件登记的产物');
  if (typeof key !== 'string' || !KEY_RE.test(key)) fail('invalid_argument', 'key 不合法');
  return { type, key };
}

function saveBody(params) {
  for (const k of Object.keys(params)) {
    if (!['type', 'key', ...SAVE_KEYS].includes(k)) fail('invalid_argument', `保存不认这个字段：${k.slice(0, 40)}`);
  }
  if (typeof params.base_revision !== 'string' || !params.base_revision) fail('invalid_argument', '保存要带 base_revision');
  if (params.fields !== undefined && !isObject(params.fields)) fail('invalid_argument', 'fields 要是对象');
  if (params.sections !== undefined) {
    if (!isObject(params.sections) || Object.values(params.sections).some((v) => typeof v !== 'string')) fail('invalid_argument', 'sections 要是「标题 → 正文」');
  }
  if (params.summary !== undefined && (typeof params.summary !== 'string' || params.summary.length > SUMMARY_MAX)) fail('invalid_argument', 'summary 太长');
  const body = { base_revision: params.base_revision };
  for (const k of ['fields', 'sections', 'content', 'summary']) if (params[k] !== undefined) body[k] = params[k];
  return body;
}

function commandOf(params, ctx) {
  const command = params.command;
  // 只能发本包的命令（上传包的命令都带包名前缀，静态检查保证）
  if (typeof command !== 'string' || !new RegExp(`^/${ctx.pack}-[a-z0-9-]{1,60}$`).test(command)) fail('forbidden', '只能发本插件的命令');
  const args = params.args === undefined ? {} : params.args;
  if (!isObject(args) || Object.values(args).some((v) => !['string', 'number', 'boolean'].includes(typeof v))) fail('invalid_argument', 'args 只能是简单的键值');
  if (params.text !== undefined && (typeof params.text !== 'string' || params.text.length > TEXT_MAX)) fail('invalid_argument', 'text 太长');
  return { command, args, text: params.text };
}

/**
 * 校验并执行一次调用。ctx：{pack, version, label, editor: {id, title}, artifact: {type, key, title}, types: string[], readonly}；
 * deps：{readArtifact(type, key), saveArtifact(type, key, body), sendCommand({command, args, text}), theme()}。
 * 接口出错原样带回（code、message、status、details），比如写入范围锁的 423。
 */
export async function handle(call, ctx, deps) {
  try {
    switch (call.method) {
      case 'context.get':
        return {
          ui_api: UI_API, pack: { name: ctx.pack, version: ctx.version, label: ctx.label }, editor: ctx.editor,
          artifact: ctx.artifact, readonly: !!ctx.readonly,
        };
      case 'artifact.read': {
        const { type, key } = targetOf(call.params, ctx);
        return await deps.readArtifact(type, key);
      }
      case 'artifact.save': {
        const { type, key } = targetOf(call.params, ctx);
        return await deps.saveArtifact(type, key, saveBody(call.params));
      }
      case 'chat.command':
        return await deps.sendCommand(commandOf(call.params, ctx));
      case 'theme.get':
        return deps.theme();
      default:
        return fail('method_unknown', '不认识的方法');
    }
  } catch (e) {
    if (e instanceof BridgeError) throw e;
    throw new BridgeError(e?.code || 'internal_error', e?.message || '平台处理失败', { status: e?.status, details: e?.details ?? null });
  }
}

export function response(id, result) {
  return { onto_ui: UI_API, id, ok: true, result: result === undefined ? null : result };
}

export function errorResponse(id, error) {
  const out = { code: error?.code || 'internal_error', message: error?.message || '' };
  if (error?.status) out.status = error.status;
  if (error?.details) out.details = error.details;
  return { onto_ui: UI_API, id, ok: false, error: out };
}

export function event(name, data) {
  return { onto_ui: UI_API, event: name, data };
}

// 从计算后的样式里取主题变量（宿主调用；单测里传假的 getter）
export function readTheme(get) {
  const vars = {};
  for (const name of THEME_VARS) {
    const v = String(get(name) || '').trim();
    if (v) vars[name] = v;
  }
  return { mode: 'light', vars };
}
