// 个人设置（me）：分区参数、改密规则逐条判定、偏好合并、模型配置表单到 A-10 请求体、mock 的 A-06～A-11
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { applyModelClear, applyModelSave, initialMe, register as registerMe } from '../src/api/mock/routes-me.js';
import {
  DEFAULT_PREFS, KIND_PREF, NOTI_GROUPS, RULE_CHIPS, adminText, avatarText, canClear, changePasswordRules, clearedToast, contextText,
  engineCards, failedRules, fmtMoney, fmtTokensShort, isLocalMode, lastLoginText, modelDirty, modelForm, modelProblems, modelRequest,
  normalizePrefs, parseSection, passwordError, passwordFormView, permissionNote, placeholders, profileChips, sourceChip, sourceOptions,
  systemNotifyAllowed, usageView, withPref,
} from '../src/pages/me/meModel.js';

// ---------- 分区 ----------
test('分区参数：四个分区原样，别名认得，其余落到账号', () => {
  assert.deepEqual(['acct', 'look', 'noti', 'model'].map(parseSection), ['acct', 'look', 'noti', 'model']);
  assert.equal(parseSection(' MODEL '), 'model');
  assert.equal(parseSection('password'), 'acct');
  assert.equal(parseSection('theme'), 'look');
  assert.equal(parseSection('notifications'), 'noti');
  assert.equal(parseSection(undefined), 'acct');
  assert.equal(parseSection('sys'), 'acct');
});

// ---------- 账号 ----------
test('账号卡：本地模式、角色与认证来源 chip、头像字、用量与上次登录', () => {
  assert.equal(isLocalMode({ auth_source: 'local' }, null), true);
  assert.equal(isLocalMode(F.USERS.zhangsan, { auth: { enabled: false } }), true);
  assert.equal(isLocalMode(F.USERS.zhangsan, { auth: { enabled: true } }), false);
  assert.deepEqual(profileChips(F.USERS.modeler01, false).map((c) => c.text), ['建模者', 'auth-server']);
  assert.deepEqual(profileChips(F.USERS.local, true).map((c) => c.text), ['管理员', '本地模式']);
  assert.equal(avatarText(F.USERS.zhangsan), '张');
  assert.equal(avatarText({ username: 'ops01' }), 'o');

  const u = usageView({ input_tokens: 2790000, output_tokens: 410000, cost: { amount: 126, currency: 'CNY' } });
  assert.equal(u.text, '3.2M');
  assert.equal(u.title, '本月 tokens：输入 2,790,000 · 输出 410,000；费用 ¥126');
  assert.equal(usageView({ input_tokens: 800, output_tokens: 0, cost: { amount: null, currency: 'USD' } }).title.endsWith('费用 —'), true);
  assert.equal(usageView(null).text, '—');
  assert.deepEqual([fmtTokensShort(null), fmtTokensShort(950), fmtTokensShort(32000), fmtTokensShort(200000)], ['—', '950', '32k', '200k']);
  assert.deepEqual([fmtMoney({ amount: 1.5, currency: 'USD' }), fmtMoney({ amount: 126, currency: 'CNY' }), fmtMoney(null)], ['$1.50', '¥126', '—']);

  assert.match(lastLoginText({ at: '2026-10-31T00:52:00Z', ip: '192.0.2.24' }), /^上次登录 2026-10-3\d \d\d:52 · 192\.0\.2\.24$/);
  assert.equal(lastLoginText(null), ''); // B2 第 5 条：重启后为 null
  assert.equal(lastLoginText({ at: 'bad', ip: null }), '');
});

// ---------- 改密规则 ----------
test('改密规则逐条判定：键与 weak_password 的 failed_rules 同名', () => {
  const ok = changePasswordRules('zhangsan', 'Oldpass#2025', 'Mango#2026');
  assert.deepEqual(Object.keys(ok).sort(), ['classes', 'common', 'contains_username', 'edge_space', 'len', 'max', 'same_as_old']);
  assert.ok(Object.values(ok).every(Boolean));
  assert.deepEqual(failedRules('zhangsan', 'x', 'Ab1#'), ['len']);
  assert.deepEqual(failedRules('zhangsan', 'x', `Ab1#${'a'.repeat(130)}`), ['max']);
  assert.deepEqual(failedRules('zhangsan', 'x', 'abcdefgh1'), ['classes']);
  assert.deepEqual(failedRules('zhangsan', 'x', 'ZhangSan#2026'), ['contains_username']);
  assert.deepEqual(failedRules('u', 'x', 'Passw0rd!'), ['common']);
  assert.deepEqual(failedRules('zhangsan', 'x', ' Mango#2026'), ['edge_space']);
  assert.deepEqual(failedRules('zhangsan', 'Mango#2026', 'Mango#2026'), ['same_as_old']);
  // 旧密码还没填时不判「与旧密码相同」
  assert.deepEqual(failedRules('zhangsan', '', 'Mango#2026'), []);
});

test('改密表单：chip 实时打勾、其余规则单独说、两次不一致、能不能提交', () => {
  const idle = passwordFormView({}, 'zhangsan');
  assert.deepEqual(idle.chips.map((c) => c.state), ['idle', 'idle', 'idle', 'idle']);
  assert.deepEqual(idle.chips.map((c) => c.text), RULE_CHIPS.map(([, t]) => t));
  assert.equal(idle.canSubmit, false);

  const weak = passwordFormView({ old: 'Oldpass#2025', next: 'zhangsan  ', confirm: 'zhangsan  ' }, 'zhangsan');
  assert.deepEqual(weak.chips.map((c) => [c.key, c.state]), [['len', 'ok'], ['classes', 'fail'], ['contains_username', 'fail'], ['common', 'ok']]);
  assert.deepEqual(weak.extra, ['首尾不能有空格']);
  assert.equal(weak.canSubmit, false);

  const mis = passwordFormView({ old: 'Oldpass#2025', next: 'Mango#20261', confirm: 'Mango#2026' }, 'zhangsan');
  assert.equal(mis.mismatch, true);
  assert.deepEqual(mis.chips.map((c) => c.state), ['ok', 'ok', 'ok', 'ok']);
  assert.equal(mis.canSubmit, false);

  const good = passwordFormView({ old: 'Oldpass#2025', next: 'Mango#2026', confirm: 'Mango#2026' }, 'zhangsan');
  assert.equal(good.canSubmit, true);
  // 服务端说是常见密码（本地表里没有）：chip 变红、不能再提交
  const srv = passwordFormView({ old: 'Oldpass#2025', next: 'Mango#2026', confirm: 'Mango#2026' }, 'zhangsan', ['common']);
  assert.equal(srv.chips.find((c) => c.key === 'common').state, 'fail');
  assert.equal(srv.canSubmit, false);
  assert.deepEqual(passwordFormView({ old: 'a', next: 'Mango#2026', confirm: '' }, 'zhangsan').mismatch, false);
});

test('改密错误落到字段：旧密码不对、弱密码、本地模式、没配管理令牌，其余交给通用提示', () => {
  assert.deepEqual(passwordError({ code: 'old_password_incorrect' }), { field: 'old', text: '当前密码不正确' });
  assert.deepEqual(passwordError({ code: 'weak_password', details: { failed_rules: ['common'] } }).failed, ['common']);
  assert.equal(passwordError({ code: 'forbidden', details: { reason: 'local_mode' } }).field, null);
  assert.match(passwordError({ code: 'user_admin_unavailable' }).text, /管理令牌/);
  assert.equal(passwordError({ code: 'rate_limited' }), null);
  assert.equal(passwordError({ code: 'forbidden', details: null }), null);
});

// ---------- 偏好 ----------
test('偏好合并：缺字段按默认补、不认识的主题回浅色、多出来的字段保留；改一项不动其余', () => {
  assert.deepEqual(normalizePrefs(null), DEFAULT_PREFS);
  const p = normalizePrefs({ theme: 'sepia', notifications: { system: false, l4_new_suspension: true, l2_error: 'yes', future_kind: true }, density: 'compact' });
  assert.equal(p.theme, 'light');
  assert.equal(p.density, 'compact');
  assert.equal(p.notifications.system, false);
  assert.equal(p.notifications.l4_new_suspension, true);
  assert.equal(p.notifications.l2_error, true); // 不是布尔按默认
  assert.equal(p.notifications.future_kind, true);
  assert.equal(p.notifications.l2_step_done, true);

  const dark = withPref(DEFAULT_PREFS, 'theme', 'dark');
  assert.equal(dark.theme, 'dark');
  assert.deepEqual(dark.notifications, DEFAULT_PREFS.notifications);
  const off = withPref(dark, 'notifications.l2_error', false);
  assert.equal(off.theme, 'dark');
  assert.equal(off.notifications.l2_error, false);
  assert.equal(off.notifications.l2_needs_input, true);
  assert.equal(DEFAULT_PREFS.notifications.l2_error, true); // 不改原对象
  // 设计稿默认：只有「新增挂起问题」关着
  assert.deepEqual(Object.entries(DEFAULT_PREFS.notifications).filter(([, v]) => !v).map(([k]) => k), ['l4_new_suspension']);
  assert.deepEqual(NOTI_GROUPS.flatMap((g) => g.items.map(([k]) => k)), Object.keys(DEFAULT_PREFS.notifications).filter((k) => k !== 'system'));
});

test('系统通知：要浏览器授权、总开关打开，按通知类型对应的开关；权限说明', () => {
  const prefs = withPref(DEFAULT_PREFS, 'notifications.l4_new_suspension', false);
  assert.equal(systemNotifyAllowed(prefs, 'autobuild_finished', 'granted'), true);
  assert.equal(systemNotifyAllowed(prefs, 'autobuild_suspension', 'granted'), false);
  assert.equal(systemNotifyAllowed(prefs, 'publish_done', 'granted'), true); // 表里没有的类型只看总开关
  assert.equal(systemNotifyAllowed(prefs, 'autobuild_finished', 'default'), false);
  assert.equal(systemNotifyAllowed(withPref(prefs, 'notifications.system', false), 'publish_done', 'granted'), false);
  assert.ok(Object.values(KIND_PREF).every((k) => k in DEFAULT_PREFS.notifications));

  assert.equal(permissionNote('granted', true), null);
  assert.equal(permissionNote('default', false), null);
  assert.equal(permissionNote('default', true).action, '允许通知');
  assert.equal(permissionNote('denied', false).tone, 'amber');
  assert.equal(permissionNote('unsupported', true).tone, 'gray');
});

// ---------- 我的模型配置 ----------
test('引擎卡：按 C-06 排序、只用 label、带接口类型；没装的说原因；C-06 拿不到时用兜底名字', () => {
  const settings = F.modelSettings();
  const cards = engineCards(settings, F.engines({ betaInstalled: false }));
  assert.deepEqual(cards.map((c) => [c.id, c.title, c.sub, c.icon]), [
    ['claude', '稳定引擎 · Anthropic 兼容接口', '功能最完整', 'cap'],
    ['jiuwen', '内测引擎 · OpenAI 兼容接口', '当前部署未安装', 'node'],
  ]);
  assert.ok(cards.every((c) => !`${c.title}${c.sub}`.includes(c.id)));
  assert.deepEqual(engineCards(settings, null).map((c) => c.title), ['稳定引擎 · Anthropic 兼容接口', '内测引擎 · OpenAI 兼容接口']);
  assert.equal(engineCards(settings, F.engines()).at(1).sub, '内测中');
  // A-09 没有的引擎不出卡
  assert.deepEqual(engineCards({ engines: { jiuwen: settings.engines.jiuwen } }, F.engines()).map((c) => c.id), ['jiuwen']);
});

test('来源三选一、管理员摘要、占位、上下文参数', () => {
  const cfg = F.modelSettings().engines.claude;
  assert.deepEqual(sourceOptions(cfg).map((o) => o.key), ['deployment', 'admin', 'personal']);
  assert.equal(adminText(cfg), '管理员配置：gateway.example.com · model-standard');
  assert.equal(adminText({ admin_summary: { gateway_host: 'gateway.example.com', model: null } }), '管理员配置：gateway.example.com · 网关默认模型');
  assert.match(adminText({ admin_summary: null }), /部署默认/);
  assert.deepEqual(sourceChip('personal'), { text: '个人 Key', tone: 'blue', icon: 'key' });
  assert.equal(sourceChip('admin').text, '管理员配置');
  assert.equal(sourceChip('deployment').text, '部署默认');
  const ph = placeholders(cfg);
  assert.equal(ph.api_key, '粘贴你的 API Key');
  assert.equal(ph.model, '留空沿用管理员配置的模型 · model-standard');
  assert.equal(placeholders({ ...cfg, api_key_set: true }).api_key, 'sk-•••• •••• ••••');
  assert.equal(placeholders({ ...cfg, admin_summary: null }).base_url, '留空沿用部署默认的网关地址');
  assert.equal(contextText(cfg.context), '上下文 200k · 输出上限 32k · 用到 80% 时压缩');
  assert.equal(contextText(null), '');
});

test('模型配置表单 → A-10 请求体：Key 留空不传，网关、模型只传改过的，切回管理员只传 source', () => {
  const admin = F.modelSettings().engines.claude;
  const f0 = modelForm(admin);
  assert.deepEqual(f0, { source: 'admin', api_key: '', base_url: '', model: '' });
  assert.equal(modelDirty(admin, f0), false);

  // 第一次用个人 Key：没填 Key 不让存；网关不是 http(s) 也不让存
  const p1 = { ...f0, source: 'personal' };
  assert.equal(modelDirty(admin, p1), true);
  assert.deepEqual(Object.keys(modelProblems(admin, p1)), ['api_key']);
  assert.deepEqual(Object.keys(modelProblems(admin, { ...p1, api_key: 'example-key', base_url: 'gateway.example.com' })), ['base_url']);
  assert.deepEqual(modelProblems(admin, { ...p1, api_key: 'example-key', base_url: 'https://gateway.example.com/v1' }), {});
  assert.deepEqual(modelRequest(admin, { ...p1, api_key: ' example-key ' }), { source: 'personal', api_key: 'example-key' });
  assert.deepEqual(modelRequest(admin, { ...p1, api_key: 'example-key', base_url: 'https://gateway.example.com/v1 ', model: 'model-large-256k' }), {
    source: 'personal', api_key: 'example-key', base_url: 'https://gateway.example.com/v1', model: 'model-large-256k',
  });

  // 已经有个人 Key：留空保持不变；只改模型就只传模型；把网关清空传空串
  const mine = { ...admin, source: 'personal', api_key_set: true, base_url: 'https://gateway.example.com/v1', model: 'model-x' };
  const f1 = modelForm(mine);
  assert.equal(modelDirty(mine, f1), false);
  assert.deepEqual(modelProblems(mine, f1), {});
  assert.deepEqual(modelRequest(mine, { ...f1, model: 'model-y' }), { source: 'personal', model: 'model-y' });
  assert.deepEqual(modelRequest(mine, { ...f1, base_url: '' }), { source: 'personal', base_url: '' });
  assert.equal(modelDirty(mine, { ...f1, api_key: '  ' }), false);
  // 切回管理员 / 部署默认：只传 source（服务端清掉个人配置）
  assert.deepEqual(modelRequest(mine, { ...f1, source: 'admin', api_key: 'example-ignored' }), { source: 'admin' });
  assert.equal(modelDirty(mine, { ...f1, source: 'deployment' }), true);
  assert.deepEqual(modelProblems(admin, { ...f0, source: 'deployment', base_url: 'bad' }), {});

  assert.equal(canClear(admin), false);
  assert.equal(canClear(mine), true);
  assert.equal(canClear({ ...admin, model: 'model-x' }), true);
  assert.equal(clearedToast({ source: 'admin' }), '已清除个人配置，改用管理员配置');
  assert.equal(clearedToast({ source: 'deployment' }), '已清除个人配置，改用部署默认配置');
});

// ---------- mock ----------
function harness({ sc = {}, authEnabled = true, user = F.USERS.zhangsan } = {}) {
  const routes = [];
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db: { cards: F.projectCards() },
    sc,
    F,
    authEnabled,
  };
  registerMe(ctx);
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, user });
      if (res !== undefined) return { status: res.status, body: res.status === 204 ? null : await res.json() };
    }
    throw new Error(`${method} ${path} 没有路由`);
  };
  return { call, ctx };
}

test('mock A-07：项目数不算归档，上次登录用保留网段；本地模式没有上次登录', async () => {
  const me = (await harness().call('GET', '/api/v1/me')).body;
  assert.equal(me.project_count, 5);
  assert.match(me.last_login.ip, /^192\.0\.2\./);
  assert.equal(usageView(me.usage_this_month).text, '3.2M');
  assert.equal((await harness({ authEnabled: false, user: F.USERS.local }).call('GET', '/api/v1/me')).body.last_login, null);
});

test('mock A-06：本地模式 403、旧密码不对、弱密码带 failed_rules、频繁、没配管理令牌、成功 204', async () => {
  const body = { old_password: 'Oldpass#2025', new_password: 'Mango#2026' };
  const local = await harness({ authEnabled: false, user: F.USERS.local }).call('PUT', '/api/v1/me/password', body);
  assert.deepEqual([local.status, local.body.code, local.body.details.reason], [403, 'forbidden', 'local_mode']);
  const { call } = harness();
  assert.equal((await call('PUT', '/api/v1/me/password', { ...body, old_password: 'wrong' })).body.code, 'old_password_incorrect');
  const weak = await call('PUT', '/api/v1/me/password', { old_password: 'Mango#2026', new_password: 'Mango#2026' });
  assert.deepEqual([weak.status, weak.body.code, weak.body.details.failed_rules], [400, 'weak_password', ['same_as_old']]);
  assert.deepEqual((await call('PUT', '/api/v1/me/password', { ...body, new_password: 'zhangsan#A1' })).body.details.failed_rules, ['contains_username']);
  assert.equal((await call('PUT', '/api/v1/me/password', { old_password: '', new_password: '' })).body.code, 'invalid_argument');
  assert.equal((await call('PUT', '/api/v1/me/password', body)).status, 204);
  assert.equal((await harness({ sc: { me: 'rate' } }).call('PUT', '/api/v1/me/password', body)).status, 429);
  assert.equal((await harness({ sc: { me: 'noadmin' } }).call('PUT', '/api/v1/me/password', body)).body.code, 'user_admin_unavailable');
});

test('mock A-08：读默认、整体替换并补齐；me:noprefs 两个方向都 404', async () => {
  const { call } = harness();
  assert.deepEqual((await call('GET', '/api/v1/me/preferences')).body, DEFAULT_PREFS);
  const saved = (await call('PUT', '/api/v1/me/preferences', { theme: 'dark', notifications: { system: false } })).body;
  assert.equal(saved.theme, 'dark');
  assert.equal(saved.notifications.system, false);
  assert.equal(saved.notifications.l2_step_done, true);
  assert.deepEqual((await call('GET', '/api/v1/me/preferences')).body, saved);
  assert.equal((await call('PUT', '/api/v1/me/preferences', { theme: 'blue' })).body.code, 'invalid_argument');
  const none = harness({ sc: { me: 'noprefs' } });
  assert.equal((await none.call('GET', '/api/v1/me/preferences')).status, 404);
  assert.equal((await none.call('PUT', '/api/v1/me/preferences', DEFAULT_PREFS)).status, 404);
});

test('mock A-09～A-11：存个人 Key → personal、已设置；改模型沿用 Key；切回管理员等于清除；DELETE 回到管理员', async () => {
  const { call, ctx } = harness();
  const base = '/api/v1/me/model-settings';
  assert.equal((await call('GET', base)).body.personal_allowed, true);
  assert.equal((await call('PUT', `${base}/claude`, { source: 'personal' })).body.details.fields.api_key, 'required');
  assert.equal((await call('PUT', `${base}/claude`, { source: 'personal', api_key: 'example-key', base_url: 'gateway.example.com' })).body.details.fields.base_url, 'invalid_url');
  assert.equal((await call('PUT', `${base}/nope`, { source: 'admin' })).status, 404);

  const saved = (await call('PUT', `${base}/claude`, { source: 'personal', api_key: 'example-key', model: 'model-large-256k' })).body;
  assert.deepEqual([saved.source, saved.api_key_set, saved.model, saved.base_url], ['personal', true, 'model-large-256k', '']);
  assert.ok(!JSON.stringify(saved).includes('example-key')); // Key 不回显
  // 项目设置 P-12 读的是同一份
  assert.equal(ctx.db.me.modelSettings.engines.claude.source, 'personal');
  const kept = (await call('PUT', `${base}/claude`, { source: 'personal', base_url: 'https://gateway.example.com/v1' })).body;
  assert.deepEqual([kept.api_key_set, kept.model, kept.base_url], [true, 'model-large-256k', 'https://gateway.example.com/v1']);

  const back = (await call('PUT', `${base}/claude`, { source: 'deployment' })).body;
  assert.deepEqual([back.source, back.api_key_set, back.model, back.base_url], ['deployment', false, '', '']);
  await call('PUT', `${base}/jiuwen`, { source: 'personal', api_key: 'example-key' });
  const cleared = (await call('DELETE', `${base}/jiuwen`)).body;
  assert.deepEqual([cleared.source, cleared.api_key_set], ['admin', false]);
});

test('mock me:nokey：个人 Key 关闭时存个人配置 403；me:personal 稳定引擎已经在用个人 Key', async () => {
  const { call } = harness({ sc: { me: 'nokey+personal' } });
  const s = (await call('GET', '/api/v1/me/model-settings')).body;
  assert.equal(s.personal_allowed, false);
  assert.equal(s.engines.claude.source, 'personal');
  assert.equal(s.engines.jiuwen.source, 'admin');
  assert.equal((await call('PUT', '/api/v1/me/model-settings/jiuwen', { source: 'personal', api_key: 'example-key' })).status, 403);
  // 纯函数：清除时管理员没配就回部署默认
  assert.equal(applyModelClear({ source: 'personal', admin_summary: null }).source, 'deployment');
  assert.equal(applyModelSave({ source: 'admin', api_key_set: false, base_url: '', model: '' }, { source: 'personal', api_key: 'k' }).api_key_set, true);
  assert.equal(initialMe({ F, flags: new Set() }).modelSettings.personal_allowed, true);
});

test('整个 mock 服务端：存了个人 Key，项目设置的模型来源跟着变成「我的」', async () => {
  globalThis.window = { location: { search: '' }, localStorage: { getItem: () => null } };
  const { createMockServer } = await import('../src/api/mock/index.js');
  const s = createMockServer();
  delete globalThis.window;
  const call = async (method, path, body) => {
    const res = await s.fetch(`/api/v1${path}`, { method, headers: { Authorization: 'Bearer mock.zhangsan.1' }, body: body ? JSON.stringify(body) : undefined });
    return { status: res.status, body: res.status === 204 ? null : await res.json() };
  };
  const pid = F.MAIN_PID;
  assert.equal((await call('GET', `/projects/${pid}/engine`)).body.model_source, 'admin');
  assert.equal((await call('PUT', '/me/model-settings/claude', { source: 'personal', api_key: 'example-key' })).status, 200);
  assert.equal((await call('GET', `/projects/${pid}/engine`)).body.model_source, 'personal');
  assert.equal((await call('DELETE', '/me/model-settings/claude')).body.source, 'admin');
  assert.equal((await call('GET', `/projects/${pid}/engine`)).body.model_source, 'admin');
});
