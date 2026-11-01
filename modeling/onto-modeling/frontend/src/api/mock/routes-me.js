// mock：个人设置（A-06～A-11）。页面：me，顶栏头像；项目设置的模型来源（P-12）也读这里的状态（db.me.modelSettings）。
// A-05 退出登录在 index.js（登录那一组）。
// 地址参数 ?mock=me:<状态>，几个状态用 + 连起来（如 me:nokey+noprefs）：
// - me:nokey    管理员关闭了个人 API Key（A-09 personal_allowed=false，A-10 返回 403）
// - me:personal 稳定引擎已经在用个人 Key（对照设计稿 me「我的模型配置」默认态）
// - me:noprefs  偏好接口还没有（A-08 返回 404，同 B2 后端现状）
// - me:rate     改密太频繁（A-06 返回 429）
// - me:noadmin  账号服务没配管理令牌（A-06 返回 503）
// 改密时当前密码填 wrong 返回 old_password_incorrect；本地模式（auth:local）A-06 返回 403（B2 第 3 条）。
import { DEFAULT_PREFS, failedRules, normalizePrefs } from '../../pages/me/meModel.js';

const URL_RE = /^https?:\/\/[^\s/]+/i;

export function initialMe({ F, flags }) {
  const modelSettings = F.modelSettings();
  if (flags.has('nokey')) modelSettings.personal_allowed = false;
  if (flags.has('personal')) {
    Object.assign(modelSettings.engines.claude, { source: 'personal', api_key_set: true, base_url: '', model: '' });
  }
  return { modelSettings, prefs: normalizePrefs(DEFAULT_PREFS) };
}

// A-10：source=admin|deployment 等于清除个人配置（B2 第 7 条）；base_url、model 不传表示沿用已存值
export function applyModelSave(cfg, body) {
  const source = body.source;
  if (source !== 'personal') return { ...cfg, source, api_key_set: false, base_url: '', model: '' };
  const next = { ...cfg, source: 'personal' };
  if (body.api_key) next.api_key_set = true;
  if (body.base_url !== undefined) next.base_url = body.base_url;
  if (body.model !== undefined) next.model = body.model;
  return next;
}
// A-11：回到管理员配置；管理员没配时就是部署默认
export function applyModelClear(cfg) {
  return { ...cfg, source: cfg.admin_summary ? 'admin' : 'deployment', api_key_set: false, base_url: '', model: '' };
}

export function register(ctx) {
  const { on, json, err, db, F, sc, authEnabled } = ctx;
  const flags = new Set(String(sc.me || '').split(/[+\s]+/).filter(Boolean)); // 地址里的 + 会解码成空格
  db.me = db.me || initialMe({ F, flags });

  // A-07
  on('GET', '/me', ({ user }) => json(200, {
    ...user,
    last_login: authEnabled ? { at: new Date(Date.now() - 26 * 3600e3).toISOString(), ip: '192.0.2.24' } : null,
    project_count: db.cards.filter((c) => !c.archived).length,
    usage_this_month: { output_tokens: 410000, input_tokens: 2790000, cost: { amount: 126, currency: 'CNY' } },
  }));

  // A-06
  on('PUT', '/me/password', ({ user, body }) => {
    if (!authEnabled) return err(403, 'forbidden', '本地模式不能修改密码', { reason: 'local_mode' });
    if (flags.has('rate')) return err(429, 'rate_limited', '请求过于频繁', { retry_after_s: 60 });
    if (flags.has('noadmin')) return err(503, 'user_admin_unavailable', '没配置账号服务管理令牌');
    if (!body.old_password || !body.new_password) return err(400, 'invalid_argument', '请填写当前密码和新密码', { fields: { old_password: body.old_password ? undefined : 'required', new_password: body.new_password ? undefined : 'required' } });
    if (body.old_password === 'wrong') return err(400, 'old_password_incorrect', '旧密码不正确');
    const failed = failedRules(user.username, body.old_password, body.new_password);
    if (failed.length) return err(400, 'weak_password', '密码不满足规则', { failed_rules: failed });
    return new Response(null, { status: 204 });
  });

  // A-08（B2 后端还没有这组接口，me:noprefs 模拟 404）
  on('GET', '/me/preferences', () => (flags.has('noprefs') ? err(404, 'not_found', '路径不存在') : json(200, db.me.prefs)));
  on('PUT', '/me/preferences', ({ body }) => {
    if (flags.has('noprefs')) return err(404, 'not_found', '路径不存在');
    if (body.theme && !['light', 'dark'].includes(body.theme)) return err(400, 'invalid_argument', '主题只能是浅色或深色', { fields: { theme: 'invalid' } });
    db.me.prefs = normalizePrefs(body);
    return json(200, db.me.prefs);
  });

  // A-09
  on('GET', '/me/model-settings', () => json(200, db.me.modelSettings));
  // A-10
  on('PUT', '/me/model-settings/([^/]+)', ({ m, body }) => {
    const engine = decodeURIComponent(m[1]);
    const cfg = db.me.modelSettings.engines[engine];
    if (!cfg) return err(404, 'not_found', '没有这个引擎');
    if (!['deployment', 'admin', 'personal'].includes(body.source)) return err(400, 'invalid_argument', '来源不合法', { fields: { source: 'invalid' } });
    if (body.source === 'personal' && !db.me.modelSettings.personal_allowed) return err(403, 'forbidden', '管理员已关闭个人 API Key', { reason: 'personal_key_disabled' });
    if (body.source === 'personal' && !cfg.api_key_set && !body.api_key) return err(400, 'invalid_argument', '第一次使用个人 Key 要填 API Key', { fields: { api_key: 'required' } });
    if (body.base_url && !URL_RE.test(body.base_url)) return err(400, 'invalid_argument', '网关地址要以 http:// 或 https:// 开头', { fields: { base_url: 'invalid_url' } });
    db.me.modelSettings.engines[engine] = applyModelSave(cfg, body);
    return json(200, db.me.modelSettings.engines[engine]);
  });
  // A-11
  on('DELETE', '/me/model-settings/([^/]+)', ({ m }) => {
    const engine = decodeURIComponent(m[1]);
    const cfg = db.me.modelSettings.engines[engine];
    if (!cfg) return err(404, 'not_found', '没有这个引擎');
    db.me.modelSettings.engines[engine] = applyModelClear(cfg);
    return json(200, db.me.modelSettings.engines[engine]);
  });
}
