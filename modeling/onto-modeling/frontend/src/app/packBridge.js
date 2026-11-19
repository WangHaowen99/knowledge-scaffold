// 上传包 iframe 编辑器与平台之间的窄接口（界面接口 1.0，ADR-0055；契约「包自带界面」一节）。
// iframe 是不同源的（sandbox 只给 allow-scripts），拿不到登录态，只能经这里；平台按下面的白名单代办，
// 越权的、认不出的一律拒绝。只做消息校验与分派，不碰 DOM，可以单测。
//
// 握手：iframe 往父窗口发 {onto_ui: "1.0", connect: true}，平台回 {onto_ui, event: "connected"} 并转交一个 MessagePort；
// 之后的请求、回应、推送都走这个端口。每个 iframe 只握手一次；iframe 再次 load（导航走了）就拆掉，
// 端口随原页面一起失效，导航到的新页面接不上（审查第 2 条）。
// 请求：{onto_ui: "1.0", id, method, params}；回应：{onto_ui, id, ok: true, result} 或 {onto_ui, id, ok: false, error}；
// 平台主动推：{onto_ui, event: "artifact.changed" | "readonly.changed" | "upload.changed" | "command.progress", data}。
//
// 1.1（ADR-0103）：插件自己的页面也走这里（页面模式：ctx.page 有值、ctx.artifact 为 null），加 artifact.list、
// upload.list / put / delete（本包清单声明的上传位）、file.download（本包插件目录 ext/<包>/ 下的文件）、
// studio.open（跳到 Studio 的条目）、command.tasks（本页发出的、还在跑的命令）。
import { UI_API } from './packUi.js';

export { UI_API };
export const METHODS = ['context.get', 'artifact.read', 'artifact.save', 'chat.command', 'theme.get',
  'artifact.list', 'upload.list', 'upload.put', 'upload.delete', 'file.download', 'studio.open', 'command.tasks'];
// 主题变量：只给设计系统里的颜色、圆角、字体这几样
export const THEME_VARS = ['--ink', '--sub', '--muted', '--faint', '--line', '--panel', '--panel2', '--bg', '--blue', '--blue3',
  '--teal', '--amber', '--red', '--green', '--radius', '--font'];

const ID_MAX = 64;
const KEY_RE = /^[^/\\\u0000-\u001f]{1,120}$/;
// 发命令的限制和服务端（services/packs/ui.check_command）一致：短的单行参数值，说明不超过 500 字
const TEXT_MAX = 500;
const ARGS_MAX = 10;
const ARG_CHARS = 200;
const ARG_KEY_RE = /^[A-Za-z_][A-Za-z0-9_-]{0,39}$/;
const CONTROL_RE = /[\u0000-\u001f\u007f]/;
export const COMMAND_INTERVAL_MS = 5000;
const SUMMARY_MAX = 200;
// 1.1：上传与下载的限制（服务端另有一道：PL-28 校验知识包、PL-31 只认插件目录）
export const UPLOAD_MAX_BYTES = 50 * 1024 * 1024;
const NAME_MAX = 200;
const PATH_MAX = 512;
const UPLOAD_ID_RE = /^[a-z0-9][a-z0-9-]{0,60}$/;
const ENTRY_KINDS = ['object', 'operation', 'rule', 'metric'];
const FILTER_MAX = 10;
const LIST_LIMIT_MAX = 200;
export const DOWNLOAD_INTERVAL_MS = 2000;
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

// 只认本包登记的产物类型：类型前缀就是包名。页面模式没有当前产物，type、key 要自己给
function targetOf(params, ctx) {
  const type = params.type === undefined ? ctx.artifact?.type : params.type;
  const key = params.key === undefined ? ctx.artifact?.key : params.key;
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
  // 只能发本包清单里声明的命令（PL-20 给的列表），不按「/包名-」前缀猜：包名可能撞上别的包或官方命令
  if (typeof command !== 'string' || !(ctx.commands || []).includes(command)) fail('forbidden', '只能发本插件的命令');
  const args = params.args === undefined ? {} : params.args;
  if (!isObject(args) || Object.keys(args).length > ARGS_MAX) fail('invalid_argument', 'args 只能是少量简单键值');
  for (const [k, v] of Object.entries(args)) {
    const ok = ARG_KEY_RE.test(k) && (typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v))
      || (typeof v === 'string' && v.length <= ARG_CHARS && !CONTROL_RE.test(v)));
    if (!ok) fail('invalid_argument', 'args 的值要是不超过 200 字的单行文字、数字或布尔');
  }
  if (params.text !== undefined && (typeof params.text !== 'string' || params.text.length > TEXT_MAX
    || CONTROL_RE.test(params.text.replace(/\n/g, '')))) fail('invalid_argument', 'text 太长');
  return { command, args, text: params.text };
}

function typeOf(params, ctx) {
  if (typeof params.type !== 'string' || !ctx.types.includes(params.type)) fail('forbidden', '只能读本插件登记的产物');
  return params.type;
}

const shortLine = (v, max) => typeof v === 'string' && v.length > 0 && v.length <= max && !CONTROL_RE.test(v);

function listQuery(params) {
  const query = {};
  if (params.filter !== undefined) {
    if (!isObject(params.filter) || Object.keys(params.filter).length > FILTER_MAX) fail('invalid_argument', 'filter 只能是少量「字段 → 值」');
    for (const [k, v] of Object.entries(params.filter)) {
      if (!ARG_KEY_RE.test(k) || !shortLine(String(v), ARG_CHARS)) fail('invalid_argument', 'filter 的值要是短的单行文字');
      query[`filter[${k}]`] = String(v);
    }
  }
  if (params.q !== undefined) {
    if (!shortLine(params.q, ARG_CHARS)) fail('invalid_argument', 'q 要是短的单行文字');
    query.q = params.q;
  }
  // 分页（契约 1.6）：游标原样回传，每页最多 LIST_LIMIT_MAX 项
  if (params.cursor !== undefined && params.cursor !== null) {
    if (!shortLine(params.cursor, ARG_CHARS)) fail('invalid_argument', 'cursor 不合法');
    query.cursor = params.cursor;
  }
  if (params.limit !== undefined) {
    if (!Number.isInteger(params.limit) || params.limit < 1 || params.limit > LIST_LIMIT_MAX) fail('invalid_argument', `limit 要是 1～${LIST_LIMIT_MAX} 的整数`);
    query.limit = params.limit;
  }
  return query;
}

function slotOf(params, ctx) {
  if (typeof params.slot !== 'string' || !(ctx.uploads || []).some((u) => u.id === params.slot)) fail('forbidden', '只能用本插件声明的上传位');
  return params.slot;
}

function uploadOf(params) {
  if (!shortLine(params.name, NAME_MAX) || /[/\\]/.test(params.name)) fail('invalid_argument', 'name 要是不带路径的文件名');
  const data = params.data;
  if (!(data instanceof ArrayBuffer)) fail('invalid_argument', 'data 要是 ArrayBuffer');
  if (data.byteLength === 0) fail('invalid_argument', '文件是空的');
  if (data.byteLength > UPLOAD_MAX_BYTES) fail('payload_too_large', '文件超过 50 MB');
  return { name: params.name, data };
}

// 插件目录里的相对路径：不能是绝对路径，不能带 . / .. / 隐藏段 / 反斜杠 / 控制字符；空串是插件目录本身
function pathOf(params) {
  const path = params.path === undefined ? '' : params.path;
  if (typeof path !== 'string' || path.length > PATH_MAX || CONTROL_RE.test(path) || path.includes('\\') || path.startsWith('/')) {
    fail('invalid_argument', 'path 不合法');
  }
  const parts = path.split('/').filter(Boolean);
  if (parts.some((p) => p === '.' || p === '..' || p.startsWith('.'))) fail('invalid_argument', 'path 不合法');
  return parts.join('/');
}

function entryOf(params) {
  if (!ENTRY_KINDS.includes(params.kind)) fail('invalid_argument', `kind 只能是 ${ENTRY_KINDS.join('、')}`);
  if (typeof params.key !== 'string' || !KEY_RE.test(params.key)) fail('invalid_argument', 'key 不合法');
  return { kind: params.kind, key: params.key };
}

function throttleDownload(ctx, deps) {
  const state = ctx.state || (ctx.state = {});
  const now = deps.now ? deps.now() : Date.now();
  if (state.lastDownloadAt != null && now - state.lastDownloadAt < DOWNLOAD_INTERVAL_MS) fail('rate_limited', '下载太频繁，稍后再试');
  state.lastDownloadAt = now;
}

const needPage = (ctx) => { if (!ctx.page) fail('method_unknown', '这个方法只在插件页面里能用'); };

// 发命令限频：同一个编辑器（ctx.state）两条之间至少隔 COMMAND_INTERVAL_MS
function throttle(ctx, deps) {
  const state = ctx.state || (ctx.state = {});
  const now = deps.now ? deps.now() : Date.now();
  if (state.lastCommandAt != null && now - state.lastCommandAt < COMMAND_INTERVAL_MS) fail('rate_limited', '发命令太频繁，稍后再试');
  state.lastCommandAt = now;
}

/**
 * 校验并执行一次调用。ctx：{pack, version, label, editor: {id, title}, artifact: {type, key, title} | null,
 * page: {id, title} | null（插件页面）, types: string[], commands: string[]（本包声明的命令）,
 * uploads: [{id, title, kind, max}]（本包的上传位）, readonly, state（限频用，可变）}；
 * deps：{readArtifact(type, key), saveArtifact(type, key, body), sendCommand({command, args, text}), theme(), now?(),
 * 1.1 页面模式另有 listArtifacts(type, query), listUploads(slot), putUpload(slot, name, data), deleteUpload(slot, id),
 * download(path), openStudio(kind, key), commandTasks()}。
 * 接口出错原样带回（code、message、status、details），比如写入范围锁的 423。
 */
export async function handle(call, ctx, deps) {
  try {
    switch (call.method) {
      case 'context.get':
        return {
          ui_api: UI_API, pack: { name: ctx.pack, version: ctx.version, label: ctx.label }, editor: ctx.editor,
          artifact: ctx.artifact || null, page: ctx.page || null, readonly: !!ctx.readonly,
          uploads: (ctx.uploads || []).map((u) => ({ id: u.id, title: u.title, kind: u.kind, max: u.max })),
        };
      case 'artifact.list':
        return await deps.listArtifacts(typeOf(call.params, ctx), listQuery(call.params));
      case 'upload.list':
        needPage(ctx);
        return await deps.listUploads(slotOf(call.params, ctx));
      case 'upload.put': {
        needPage(ctx);
        const slot = slotOf(call.params, ctx);
        const { name, data } = uploadOf(call.params);
        return await deps.putUpload(slot, name, data);
      }
      case 'upload.delete': {
        needPage(ctx);
        const slot = slotOf(call.params, ctx);
        if (typeof call.params.id !== 'string' || !UPLOAD_ID_RE.test(call.params.id)) fail('invalid_argument', 'id 不合法');
        return await deps.deleteUpload(slot, call.params.id);
      }
      case 'file.download': {
        needPage(ctx);
        const path = pathOf(call.params);
        throttleDownload(ctx, deps);
        await deps.download(path);
        return { ok: true };
      }
      case 'studio.open': {
        const { kind, key } = entryOf(call.params);
        await deps.openStudio(kind, key);
        return { ok: true };
      }
      case 'command.tasks':
        needPage(ctx);
        return await deps.commandTasks();
      case 'artifact.read': {
        const { type, key } = targetOf(call.params, ctx);
        return await deps.readArtifact(type, key);
      }
      case 'artifact.save': {
        const { type, key } = targetOf(call.params, ctx);
        return await deps.saveArtifact(type, key, saveBody(call.params));
      }
      case 'chat.command': {
        const body = commandOf(call.params, ctx);
        throttle(ctx, deps);
        return await deps.sendCommand(body);
      }
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

/**
 * iframe 一侧的桥接宿主（PackFrame 用）。target()：iframe 的 contentWindow；respond(call)：校验后的调用 → 结果（抛错即错误回应）；
 * onNavigated()：iframe 第二次 load 时调，宿主据此卸掉 iframe。
 * 窗口消息只认来自 target 的一次握手；请求只走握手时转交的端口。
 */
export function createBridgeHost({ target, respond, onNavigated }) {
  let port = null;
  let used = false;
  let loads = 0;
  let closed = false;

  const close = () => {
    closed = true;
    if (port) { port.onmessage = null; port.close(); }
    port = null;
  };

  const onPortMessage = async (e) => {
    if (closed) return;
    const reply = (msg) => { if (!closed && port) port.postMessage(msg); };
    let call;
    try {
      call = parseRequest(e.data);
    } catch (err) {
      if (err.id) reply(errorResponse(err.id, err));
      return;
    }
    if (!call) return;
    try {
      const result = await respond(call);
      reply(response(call.id, result));
    } catch (err) {
      reply(errorResponse(call.id, err));
    }
  };

  return {
    get connected() { return !!port && !closed; },
    onWindowMessage(e) {
      const win = target();
      if (closed || used || !win || e.source !== win) return;
      if (!isObject(e.data) || !('onto_ui' in e.data) || e.data.connect !== true) return;
      used = true;
      const channel = new MessageChannel();
      port = channel.port1;
      port.onmessage = onPortMessage;
      win.postMessage(event('connected', { ui_api: UI_API }), '*', [channel.port2]);
    },
    onLoad() {
      loads += 1;
      if (loads > 1 && !closed) {
        close();
        onNavigated?.();
      }
    },
    send(name, data) {
      if (!closed && port) port.postMessage(event(name, data));
    },
    close,
  };
}
