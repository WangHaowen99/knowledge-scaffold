// 系统设置（sys）：页面纯逻辑（上下文参数校验、推荐表合并与匹配、导入、配置项来源与生效时机、配额单独设置、
// 单价表与服务令牌表单），以及 mock 的 SY-01～SY-18、X-06（角色把关、校验失败的行列、后端现状 sys:p1）。
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { register } from '../src/api/mock/routes-system.js';
import * as S from '../src/api/mock/system-data.js';
import {
  SERVICE_TOKEN_RE, contextSummary, customPresets, defaultPlaceholder, defaultSourceText, engineBody, engineForm, engineProblems, engineSavedText,
  expiryText, filterPresets, filterRegistry, fmtInt, importSummary, installChip, itemPrecedenceNote, limitsBody, limitsDirty, limitsForm, limitsProblems,
  locatePresetError, matchPreset, mcpForm, mcpItemFromForm, mcpItemsAfterRemove, mcpItemsAfterSave, mcpProblems, mcpSaveError, mcpStatus, mcpTarget,
  mcpTestText, mergePresets, normalizeModelName, overrideBody, overrideProblems, parseIntText, parsePresetFile, permissionModes, poolRows,
  precedenceText, presetErrorText, presetForm, presetFormProblems, presetRowActions, presetsMeta, presetsNote, pricingBody, pricingForm,
  pricingProblems, projectScopeText, quotaBody, quotaDirty, quotaForm, quotaProblems, quotaValueText, registryValue, removePreset, sandboxLine,
  serviceTokenBody, serviceTokenProblems, subjectText, sysSection, upsertPreset, validatePresets,
} from '../src/pages/sys/sysModel.js';

// ---------- 分区与数字 ----------
test('分区参数：七个分区直达，项目设置「添加服务器」的 mcp 也认，认不出落到模型接入', () => {
  ['model', 'ctx', 'sess', 'box', 'mcp', 'reg', 'quota'].forEach((k) => assert.equal(sysSection(k), k));
  assert.equal(sysSection('MCP'), 'mcp');
  assert.equal(sysSection('context'), 'ctx');
  assert.equal(sysSection('sandbox'), 'box');
  assert.equal(sysSection('l4'), 'quota');
  assert.equal(sysSection(undefined), 'model');
  assert.equal(sysSection('nope'), 'model');
});

test('整数输入：千分位、空格、百分号都认；空是 null，不是整数是 NaN', () => {
  assert.equal(parseIntText('2,000,000'), 2000000);
  assert.equal(parseIntText(' 80% '), 80);
  assert.equal(parseIntText(''), null);
  assert.ok(Number.isNaN(parseIntText('12万')));
  assert.ok(Number.isNaN(parseIntText('1.5')));
  assert.equal(fmtInt(131072), '131,072');
  assert.equal(fmtInt(null), '—');
});

// ---------- 模型接入 ----------
const stable = () => S.modelSettings().engines.claude;
const beta = () => S.modelSettings().engines.jiuwen;

test('引擎卡右上角：已安装带运行时版本；没装写「当前部署未安装」', () => {
  assert.deepEqual(installChip(stable()), { text: '已安装 · 运行时 2.1.30', tone: 'teal', icon: 'check' });
  assert.equal(installChip(S.modelSettings({ betaInstalled: false }).engines.jiuwen).text, '当前部署未安装');
});

test('上下文参数校验：窗口、最大输出的范围，最大输出小于窗口，压缩阈值 50–95', () => {
  const f = engineForm(stable());
  assert.deepEqual(engineProblems(f), {});
  assert.equal(f.compact, '80%');
  assert.equal(engineProblems({ ...f, window: '12' }).window, '整数 1,000–10,000,000');
  assert.equal(engineProblems({ ...f, window: 'abc' }).window, '整数 1,000–10,000,000');
  assert.equal(engineProblems({ ...f, max_output: '300000' }).max_output, '要小于上下文窗口');
  assert.equal(engineProblems({ ...f, compact: '99%' }).compact, '50%–95% 之间');
  assert.equal(engineProblems({ ...f, compact: '49' }).compact, '50%–95% 之间');
  assert.deepEqual(engineProblems({ ...f, compact: '95' }), {});
  assert.equal(engineProblems({ ...f, base_url: 'gateway.example.com' }).base_url, '要以 http:// 或 https:// 开头');
  assert.deepEqual(engineProblems({ ...f, window: '', max_output: '' }), {});
});

test('SY-02 请求体：只带改过的字段；Key 留空不改；清空上下文传 null；没改动返回 null', () => {
  const e = stable();
  const f = engineForm(e);
  assert.equal(engineBody(e, f), null);
  assert.deepEqual(engineBody(e, { ...f, api_key: '  ' }), null);
  assert.deepEqual(engineBody(e, { ...f, api_key: 'new-secret' }), { api_key: 'new-secret' });
  assert.deepEqual(engineBody(e, { ...f, base_url: ' https://gw2.example.com ' }), { base_url: 'https://gw2.example.com' });
  assert.deepEqual(engineBody(e, { ...f, window: '128,000' }), { context: { window: 128000 } });
  assert.deepEqual(engineBody(e, { ...f, window: '', max_output: '' }), { context: { window: null, max_output: null } });
  assert.deepEqual(engineBody(e, { ...f, compact: '70%' }), { context: { compact_at_pct: 70 } });
  assert.deepEqual(engineBody(e, { ...f, model: 'model-standard' }), { model: 'model-standard' });
});

test('保存提示：压缩阈值被服务端推算覆盖时说清楚没按填的值保存', () => {
  assert.equal(engineSavedText('稳定引擎', { base_url: 'x' }, { context: { compact_at_pct: 80 } }), '已保存，稳定引擎的新会话生效');
  assert.match(engineSavedText('稳定引擎', { context: { compact_at_pct: 70 } }, { context: { compact_at_pct: 80 } }), /由服务端按窗口与最大输出推算，现为 80%/);
  assert.equal(engineSavedText('稳定引擎', { context: { compact_at_pct: 80 } }, { context: { compact_at_pct: 80 } }), '已保存，稳定引擎的新会话生效');
});

test('留空时的默认值与来源（B2 第 8 条：env / default / 推算）', () => {
  const d = stable().context_defaults;
  assert.equal(defaultSourceText(d.window), '内置默认');
  assert.equal(defaultSourceText({ value: 64000, source: 'env' }), '.env');
  assert.equal(defaultSourceText({ value: null, source: null }), '引擎默认');
  assert.equal(defaultPlaceholder(d.window), '留空按 200000（内置默认）');
  assert.equal(defaultPlaceholder({ value: null }), '留空交给引擎默认');
});

test('内测引擎底部那行：与推荐表一致时写「来自推荐表」，留空时写默认值的来源', () => {
  const e = beta();
  const f = engineForm(e);
  assert.equal(contextSummary(e, f, { window: 131072, max_output: 8192 }).text, '上下文窗口 131072 · 最大输出 8192（来自推荐表，可覆盖）');
  assert.equal(contextSummary(e, f, { window: 131072, max_output: 16384 }).origin, '已填写');
  const empty = contextSummary(e, { ...f, window: '', max_output: '' }, null);
  assert.equal(empty.window, 200000);
  assert.equal(empty.origin, '内置默认');
});

// ---------- 上下文推荐表 ----------
const ROWS = () => mergePresets(S.BUILTIN_PRESETS, S.customPresetsSeed());

test('模型名规范化：大小写、provider 前缀、点和下划线、网关日期后缀', () => {
  assert.equal(normalizeModelName('Vendor/Model_Large.256K-20260601'), 'model-large-256k');
  assert.equal(normalizeModelName('model-standard-202606'), 'model-standard');
  assert.equal(normalizeModelName(''), '');
});

test('推荐表合并：自定义同名行覆盖内置（带内置值），其余自定义行追加；来源与操作对上', () => {
  const rows = ROWS();
  assert.equal(rows.length, 6);
  const std = rows.find((r) => r.model === 'model-standard');
  assert.equal(std.origin, 'override');
  assert.deepEqual(std.builtin, { window: 131072, max_output: 16384 });
  assert.equal(std.max_output, 8192);
  assert.equal(rows.at(-1).model, 'model-coder-256k');
  assert.equal(rows.at(-1).origin, 'custom');
  assert.deepEqual(presetRowActions(rows[0]), ['override']);
  assert.deepEqual(presetRowActions(std), ['edit', 'restore']);
  assert.deepEqual(presetRowActions(rows.at(-1)), ['edit', 'delete']);
  // 自定义行用别名写也能覆盖内置
  const viaAlias = mergePresets(S.BUILTIN_PRESETS, [{ model: 'large-256k', window: 256000, max_output: 16000 }]);
  assert.equal(viaAlias[0].origin, 'override');
  assert.equal(viaAlias[0].model, 'model-large-256k');
});

test('按模型查推荐（SY-08）：模型名、别名、带日期后缀和前缀都能命中；查不到为 null', () => {
  const rows = ROWS();
  assert.equal(matchPreset(rows, 'model-standard').max_output, 8192);
  assert.equal(matchPreset(rows, 'large-256k').model, 'model-large-256k');
  assert.equal(matchPreset(rows, 'gw/MODEL_STANDARD-20261020').model, 'model-standard');
  assert.equal(matchPreset(rows, 'model-unknown'), null);
  assert.equal(matchPreset(rows, ''), null);
});

test('自定义表的整份替换：当前自定义行、恢复内置 = 删掉覆盖行、编辑保持原位', () => {
  const rows = ROWS();
  const list = customPresets(rows);
  assert.deepEqual(list.map((p) => p.model), ['model-standard', 'model-coder-256k']);
  assert.ok(!('aliases' in list[0]), '别名、来源不传，服务端沿用同名条目');
  assert.deepEqual(removePreset(list, 'MODEL_standard').map((p) => p.model), ['model-coder-256k']);
  const edited = upsertPreset(list, { model: 'model-coder-256k', window: 256000, max_output: 16000 }, 'model-coder-256k');
  assert.equal(edited[1].max_output, 16000);
  const renamed = upsertPreset(list, { model: 'model-coder-200k', window: 200000, max_output: 16000 }, 'model-coder-256k');
  assert.deepEqual(renamed.map((p) => p.model), ['model-standard', 'model-coder-200k']);
  assert.equal(upsertPreset(list, { model: 'model-new', window: 64000, max_output: 8000 }).length, 3);
});

test('推荐表校验：第一处问题带行号与列，重名也算', () => {
  assert.equal(validatePresets([{ model: 'a', window: 64000, max_output: 8000 }]), null);
  assert.deepEqual(validatePresets([{ model: 'a', window: 64000, max_output: 8000 }, { model: 'b', window: '十万', max_output: 8000 }]), { row: 2, column: 'window', message: '不是数字' });
  assert.deepEqual(validatePresets([{ model: '', window: 64000, max_output: 8000 }]), { row: 1, column: 'model', message: '不能为空' });
  assert.deepEqual(validatePresets([{ model: 'a', window: 64000, max_output: 64000 }]), { row: 1, column: 'max_output', message: '要小于上下文窗口' });
  assert.deepEqual(validatePresets([{ model: 'model.a', window: 64000, max_output: 8000 }, { model: 'MODEL-A', window: 64000, max_output: 8000 }]), { row: 2, column: 'model', message: '与第 1 行重名' });
});

test('校验失败定位（details.error={row, column, message}）：字段名、列号、已带位置的原文', () => {
  assert.equal(presetErrorText({ row: 4, column: 'window', message: '不是数字' }), '第 4 行「上下文窗口」不是数字');
  assert.equal(presetErrorText({ row: 3, column: 2, message: '不是整数' }), '第 3 行第 2 列不是整数');
  assert.equal(presetErrorText({ row: 4, column: null, message: 'presets 第 4 条：max_context_tokens 必须是整数' }), 'presets 第 4 条：max_context_tokens 必须是整数');
  assert.equal(presetErrorText({ row: 2, column: null, message: '缺字段' }), '第 2 行：缺字段');
  assert.equal(presetErrorText({ row: null, column: null, message: '不是合法的 JSON' }), '不是合法的 JSON');
  assert.equal(presetErrorText(null), '');
  const list = [{ model: 'a' }, { model: 'b' }];
  assert.deepEqual(locatePresetError({ row: 2, column: 'window', message: 'x' }, list), { row: 2, model: 'b', field: 'window' });
  assert.equal(locatePresetError({ row: 9, column: 'window' }, list), null);
});

test('导入自定义表：CSV（中英文表头）、JSON（数组或 presets），坏行定位到行列', () => {
  const csv = parsePresetFile('a.csv', '\uFEFF模型,厂商,上下文窗口,最大输出,说明\nmodel-a,厂商 A,"200,000",16000,实测\nmodel-b,厂商 B,64000,8000,\n');
  assert.equal(csv.error, null);
  assert.deepEqual(csv.presets[0], { model: 'model-a', vendor: '厂商 A', window: 200000, max_output: 16000, note: '实测' });
  const bad = parsePresetFile('a.csv', 'model,window,max_output\nmodel-a,64000,8000\nmodel-b,12万,8000\n');
  assert.deepEqual(bad.error, { row: 2, column: 'window', message: '不是数字' });
  const json = parsePresetFile('a.json', JSON.stringify({ presets: [{ id: 'model-a', max_context_tokens: 64000, max_output_tokens: 8000, aliases: ['a1'] }] }));
  assert.equal(json.error, null);
  assert.deepEqual(json.presets[0].aliases, ['a1']);
  assert.equal(parsePresetFile('a.json', '{oops').error.message, '不是合法的 JSON');
  assert.equal(parsePresetFile('a.json', '{"x":1}').error.message, 'JSON 里要有 presets 数组');
  assert.equal(parsePresetFile('a.csv', '厂商,窗口\nA,1').error.message, '表头里没有「模型」列');
  assert.equal(parsePresetFile('a.csv', 'model').error.message, 'CSV 至少要有表头和一行数据');
  const sum = importSummary(csv.presets.concat([{ model: 'model-standard', window: 131072, max_output: 8000 }]), ROWS());
  assert.deepEqual(sum, { total: 3, overrides: 1, added: 2, replacing: 2 });
});

test('推荐表提示条：正常 / 有误（沿用或未生效）/ 停用（留有备份）；右侧的核对日期与加载时间', () => {
  const ok = { rows: ROWS(), builtin_verified_at: '2026-06-30', custom: { status: 'ok', overrides: 1, added: 1, loaded_at: '2026-10-20T06:02:00Z' } };
  assert.equal(presetsNote(ok).text, '当前使用：内置表 + 自定义表（1 行覆盖、1 行新增）。');
  assert.match(presetsMeta(ok), /^内置表核对于 2026-06-30 · 自定义表 \d\d-\d\d \d\d:\d\d 加载$/);
  const bad = { custom: { status: 'invalid', error: S.CUSTOM_ERROR, loaded_at: null } };
  assert.deepEqual(presetsNote(bad), { tone: 'red', lead: '自定义表有误，当前未生效：', text: '第 4 行「上下文窗口」不是数字。停用后改用内置表；已保存的模型配置与上下文参数不受影响。' });
  assert.match(presetsNote({ custom: { ...bad.custom, loaded_at: '2026-10-20T06:02:00Z' } }).lead, /当前沿用 .* 加载的自定义表/);
  const off = presetsNote({ builtin_verified_at: '2026-06-30', custom: { status: 'disabled', backup_available: true } });
  assert.equal(off.tone, 'gray');
  assert.match(off.text, /当前只用内置表（核对日期 2026-06-30）。上次停用的自定义表留有备份/);
  assert.equal(filterPresets(ROWS(), '厂商 f').length, 1);
  assert.equal(filterPresets(ROWS(), 'large-256K').length, 1);
});

test('推荐表编辑卡：覆盖内置时模型名锁定、带内置值；新增时和自定义行重名不行，和内置行同名算覆盖', () => {
  const rows = ROWS();
  const ov = presetForm(rows[0], 'override');
  assert.deepEqual(ov.builtin, { window: 256000, max_output: 32000 });
  assert.equal(ov.note, '');
  assert.deepEqual(presetFormProblems(ov, rows), {});
  assert.deepEqual(presetForm(null, 'new').mode, 'new');
  assert.equal(presetFormProblems({ ...presetForm(null, 'new'), model: 'Model-Coder-256K', window: '64000', max_output: '8000' }, rows).model, '与表里的 model-coder-256k 重名');
  assert.deepEqual(presetFormProblems({ ...presetForm(null, 'new'), model: 'model-long-200k', window: '64000', max_output: '8000' }, rows), {});
  assert.deepEqual(presetFormProblems({ ...presetForm(null, 'new'), model: '', window: '12', max_output: '' }, rows), { model: '不能为空', window: '要在 1,000–10,000,000 之间', max_output: '不是数字' });
  assert.deepEqual(presetFormProblems({ ...presetForm(rows.at(-1), 'edit'), max_output: '300000' }, rows), { max_output: '要小于上下文窗口' });
});

// ---------- 会话与权限、沙箱 ----------
test('会话池只读：来源是环境变量时给变量名，运维改过时说明覆盖了环境变量；逐操作确认的超时按实际值', () => {
  const rows = poolRows({ max_sessions: 12, idle_ttl_s: 600, permission_timeout_s: 300, source: 'env' });
  assert.deepEqual(rows.map((r) => [r.value, r.sub]), [['12', 'ONTO_WEB_MAX_SESSIONS'], ['600 秒', 'ONTO_WEB_SESSION_IDLE_TTL'], ['300 秒', 'ONTO_WEB_PERMISSION_TIMEOUT']]);
  assert.equal(poolRows({ max_sessions: 16, idle_ttl_s: 0, source: 'data_dir' })[0].sub, '运维面已调整（覆盖环境变量）');
  assert.equal(poolRows({ idle_ttl_s: 0 })[1].value, '不回收');
  assert.match(permissionModes(300)[1][2], /300 秒不回复/);
  assert.equal(permissionModes()[0][0], 'bypassPermissions');
});

test('沙箱那一行：连上时列隔离参数，没连上时给未连接原因（B2 第 11 条 detail）', () => {
  assert.equal(sandboxLine(S.sandbox()), '经 sandbox-gateway:8110 · 非 root、只读根文件系统、内存 256MB、最多 64 个进程');
  assert.match(sandboxLine(S.sandbox({ connected: false })), /^连不上 sandbox-gateway:8110/);
  assert.equal(sandboxLine({ connected: false, gateway: 'gw:1' }), '连不上 gw:1');
  assert.ok(S.sandbox({ p1: true }).packages.every((p) => p.purpose === null));
});

// ---------- 全局 MCP ----------
test('MCP 列表：地址 / 命令、状态（本页测过的优先，后端恒 unknown 时显示未测试）', () => {
  const [std, mermaid, kb] = S.mcpServers();
  assert.equal(mcpTarget(mermaid), 'npx mermaid-mcp --stdio');
  assert.equal(mcpTarget(std), 'https://mcp.example.com/std');
  assert.deepEqual(mcpStatus(std, {}), ['已连接', 'teal', 'check']);
  assert.deepEqual(mcpStatus(kb, {}), ['连接失败 · 401', 'red', 'x']);
  const p1 = S.mcpServers({ p1: true });
  assert.deepEqual(mcpStatus(p1[0], {}), ['未测试', 'gray', null]);
  assert.deepEqual(mcpStatus(p1[0], { [p1[0].name]: { ok: true } }), ['已连接', 'teal', 'check']);
  assert.deepEqual(mcpStatus(p1[2], { [p1[2].name]: { ok: false, code: '401' } }), ['连接失败 · 401', 'red', 'x']);
  assert.equal(mcpTestText('kb', { ok: true, tools: [{}, {}], latency_ms: 86 }), 'kb：连接正常，2 个工具，延迟 86ms');
  assert.equal(mcpTestText('kb', { ok: true, tools: [], latency_ms: 12 }), 'kb：连接正常，延迟 12ms');
  assert.equal(mcpTestText('kb', { ok: false, error: '401 未授权' }), 'kb：401 未授权');
});

test('MCP 表单：名称、地址校验，Bearer 只存引用；stdio 命令拆成 command + args；整份替换不带运行时字段', () => {
  const items = S.mcpServers();
  assert.deepEqual(mcpProblems(mcpForm(null), items, null), { name: '起个名字', target: '填地址' });
  assert.equal(mcpProblems({ ...mcpForm(null), name: 'kb-search', target: 'https://x.example.com' }, items, null).name, '已经有同名的服务器');
  assert.deepEqual(mcpProblems({ ...mcpForm(items[2]) }, items, 'kb-search'), {});
  assert.equal(mcpProblems({ ...mcpForm(null), name: 'a', target: 'ftp://x' }, items, null).target, '要以 http:// 或 https:// 开头');
  assert.equal(mcpProblems({ ...mcpForm(null), name: 'a', target: 'https://x.example.com', auth: 'bearer', secret_ref: 'abc' }, items, null).secret_ref, '填密钥引用，如 cred://mcp/<名称>');
  const stdio = mcpItemFromForm({ name: 'doc', transport: 'stdio', target: '/opt/mcp/doc --stdio  -v', auth: 'none', secret_ref: '' });
  assert.deepEqual([stdio.command, stdio.args, stdio.url, stdio.auth], ['/opt/mcp/doc', ['--stdio', '-v'], null, { kind: 'none' }]);
  const bearer = mcpItemFromForm({ name: 'kb', transport: 'http', target: 'https://mcp.example.com/kb', auth: 'bearer', secret_ref: 'cred://mcp/kb' });
  assert.deepEqual(bearer.auth, { kind: 'bearer', secret_ref: 'cred://mcp/kb' });
  const saved = mcpItemsAfterSave(items, bearer, null);
  assert.equal(saved.length, 4);
  assert.ok(saved.every((x) => !('status' in x) && !('tools' in x) && !('latency_ms' in x)));
  assert.equal(mcpItemsAfterSave(items, { ...bearer, name: 'kb-2' }, 'kb-search')[2].name, 'kb-2');
  assert.deepEqual(mcpItemsAfterRemove(items, 'mermaid-render').map((x) => x.name), ['replenish-std-lookup', 'kb-search']);
  assert.match(mcpSaveError({ code: 'invalid_argument', details: { fields: { 'auth.kind': 'unsupported' } } }), /还不支持为 MCP 服务器配置鉴权/);
  assert.equal(mcpSaveError({ code: 'invalid_argument', details: { fields: { name: 'x' } } }), null);
});

// ---------- 配置注册表 ----------
test('配置注册表：按生效时机与关键字筛（含说明）；来源优先级的显示；每项自己的优先级不同时给短说明', () => {
  const items = S.registryItems({ stableGateway: 'https://gateway.example.com/anthropic', permissionMode: 'bypassPermissions', presetsText: '内置', maxRuns: 4, snapshotMb: 20 });
  assert.equal(filterRegistry(items, { effect: 'all' }).length, 10);
  assert.deepEqual(filterRegistry(items, { effect: 'restart' }).map((x) => x.key), ['ONTO_WEB_AUTH_URL']);
  assert.deepEqual(filterRegistry(items, { q: '权限模式' }).map((x) => x.key), ['ONTO_WEB_PERMISSION_MODE']);
  assert.deepEqual(filterRegistry(items, { q: 'TRACE' }).map((x) => x.key), ['trace.max_cases_per_slice']);
  const top = ['env', 'data_dir', 'pack', 'default'];
  assert.equal(precedenceText(top), '环境变量 > 数据目录 > 包 > 默认值');
  assert.deepEqual(itemPrecedenceNote(items[0], top), { text: '设置页优先', title: '这一项的优先级：数据目录 > 环境变量 > 包 > 默认值' });
  assert.equal(itemPrecedenceNote(items[1], top), null);
  assert.equal(itemPrecedenceNote({ precedence: ['pack', 'env'] }, top).text, '包优先');
  assert.equal(itemPrecedenceNote({}, top), null);
  assert.deepEqual(registryValue({ value: 'cred://data_dir/ANTHROPIC_API_KEY', secret: true }), { text: 'cred://data_dir/ANTHROPIC_API_KEY', mono: true, muted: false });
  assert.deepEqual(registryValue({ value: '' }), { text: '未设置', mono: false, muted: true });
  assert.equal(registryValue({ value: '12' }).mono, false);
  assert.equal(items.find((x) => x.source === 'pack').pack, 'trace@0.9.1');
});

// ---------- 配额与 L4 限制 ----------
test('默认配额：表单带千分位，校验整数与范围，请求体不带只读的同时在线引擎', () => {
  const q = S.quotas();
  const f = quotaForm(q);
  assert.equal(f.daily_tokens, '2,000,000');
  assert.deepEqual(quotaProblems(f), {});
  assert.equal(quotaDirty(q, f), false);
  assert.equal(quotaDirty(q, { ...f, concurrent_sessions: '3' }), true);
  assert.equal(quotaProblems({ ...f, concurrent_sessions: '0' }).concurrent_sessions, '整数 1–64');
  assert.equal(quotaProblems({ ...f, daily_tokens: '两百万' }).daily_tokens, '填整数');
  const body = quotaBody({ ...f, monthly_tokens: '40,000,000' });
  assert.deepEqual(body.per_project, { monthly_tokens: 40000000, snapshot_git_mb: 20 });
  assert.ok(!('online_engines' in body.per_project));
});

test('单独设置：对象、项、值、原因都要；同一对象同一项不能重复；显示的说法', () => {
  const existing = S.quotas().overrides;
  const base = { kind: 'user', id: 'wanggong', item: 'daily_tokens', value: '3,000,000', reason: '培训周' };
  assert.deepEqual(overrideProblems(base, existing), {});
  assert.deepEqual(overrideBody(base), { subject: { kind: 'user', id: 'wanggong' }, item: 'daily_tokens', value: 3000000, reason: '培训周' });
  assert.equal(overrideProblems({ ...base, id: 'zhangsan' }, existing).item, '这个对象已经有这一项的单独设置，先删掉再加');
  assert.equal(overrideProblems({ ...base, kind: 'project', id: 'bike-fleet-7b20e4a1', item: 'daily_tokens' }, existing).item, '选一项');
  assert.equal(overrideProblems({ ...base, id: '' }, existing).id, '选一个用户');
  assert.equal(overrideProblems({ ...base, kind: 'project', id: '', item: 'monthly_tokens' }, existing).id, '填项目 ID');
  assert.equal(overrideProblems({ ...base, id: '张 三' }, existing).id, '用户名只含字母、数字、- _ .');
  assert.equal(overrideProblems({ ...base, value: '-1' }, existing).value, '填非负整数');
  assert.equal(overrideProblems({ ...base, reason: ' ' }, existing).reason, '写一下原因，方便以后核对');
  assert.equal(subjectText(existing[0].subject), '用户 zhangsan');
  assert.equal(subjectText(existing[1].subject), '项目 共享单车故障诊断');
  assert.equal(quotaValueText('monthly_tokens', 60000000), '60,000,000');
  assert.equal(quotaValueText('snapshot_git_mb', 40), '40 MB');
  assert.equal(quotaValueText('collections_per_source_per_hour', 5), '每小时 5 次');
});

test('L4 平台限制：时长按小时填、按分钟存；校验 0–16 与正数；夜间时段沿用原值', () => {
  const l = S.autobuildLimits();
  const f = limitsForm(l);
  assert.deepEqual([f.hours, f.max_tokens, f.cost, f.currency], ['4', '10,000,000', '300', 'CNY']);
  assert.equal(limitsDirty(l, f), false);
  assert.deepEqual(limitsProblems(f), {});
  assert.equal(limitsProblems({ ...f, max_concurrent_runs: '17' }).max_concurrent_runs, '整数 0–16');
  assert.equal(limitsProblems({ ...f, hours: '0' }).hours, '0–72 小时');
  assert.equal(limitsProblems({ ...f, cost: '' }).cost, '填大于 0 的金额');
  const body = limitsBody({ ...f, hours: '1.5', night: false }, l);
  assert.equal(body.max_duration_min, 90);
  assert.deepEqual(body.night_priority, { enabled: false, start: '20:00', end: '08:00' });
  assert.ok(!('running' in body));
  // 与项目设置（AB-01 platform_limits）同一份上限
  assert.equal(l.max_tokens, 10000000);
});

// ---------- 模型单价（SY-18）、服务令牌（X-06） ----------
test('单价表：模型必填、同一引擎下不重名、单价非负、缓存读取可留空', () => {
  const f = pricingForm(S.pricing());
  assert.deepEqual(pricingProblems(f), {});
  assert.equal(f.items[2].cache_read_per_mtok, '');
  const dup = { ...f, items: [...f.items, { ...f.items[0], model: 'Model_Standard' }] };
  assert.equal(pricingProblems(dup).rows[3].model, '同一引擎下重名');
  const neg = { ...f, items: [{ ...f.items[0], input_per_mtok: '-1', model: '' }] };
  assert.deepEqual(pricingProblems(neg).rows[0], { model: '填模型名', input_per_mtok: '填非负数' });
  assert.equal(pricingProblems({ ...f, currency: 'EUR' }).currency, '选币种');
  const body = pricingBody(f);
  assert.deepEqual(body.items[2], { engine: 'jiuwen', model: 'model-standard', input_per_mtok: 4, output_per_mtok: 12, cache_read_per_mtok: null });
});

test('服务令牌表单：用途、权限范围必填；限定项目时至少选一个；有效期与项目范围的说法', () => {
  const f = { name: '评测服务', scopes: ['receipts.write', 'releases.read'], limit: 'all', project_ids: [], ttl: '90' };
  assert.deepEqual(serviceTokenProblems(f), {});
  assert.deepEqual(serviceTokenBody(f), { name: '评测服务', scopes: ['releases.read', 'receipts.write'], expires_in_s: 90 * 86400 });
  assert.deepEqual(serviceTokenBody({ ...f, limit: 'projects', project_ids: ['p1'], ttl: '0' }).project_ids, ['p1']);
  assert.equal(serviceTokenBody({ ...f, ttl: '0' }).expires_in_s, null);
  assert.equal(serviceTokenProblems({ ...f, limit: 'projects' }).project_ids, '至少选一个项目，或改成全部项目');
  assert.equal(serviceTokenProblems({ ...f, scopes: [] }).scopes, '至少选一个');
  assert.equal(serviceTokenProblems({ ...f, scopes: ['ontology.read'] }).scopes, '权限范围不认识');
  assert.equal(serviceTokenProblems({ ...f, name: '' }).name, '写一个用途，方便以后认出它');
  const now = Date.parse('2026-11-01T12:00:00Z');
  assert.equal(expiryText(null, now), '永不过期');
  assert.equal(expiryText('2026-11-22T13:00:00Z', now), '剩 21 天');
  assert.equal(expiryText('2026-11-01T18:00:00Z', now), '剩 6 小时');
  assert.equal(expiryText('2026-10-01T00:00:00Z', now), '已过期');
  const projects = [{ id: 'a', name: '甲' }, { id: 'b', name: '乙' }, { id: 'c', name: '丙' }];
  assert.equal(projectScopeText(null, projects), '全部项目');
  assert.equal(projectScopeText(['a', 'x'], projects), '甲、x');
  assert.equal(projectScopeText(['a', 'b', 'c'], projects), '甲、乙 等 3 个');
  assert.ok(SERVICE_TOKEN_RE.test(S.placeholderServiceToken(1)));
});

// ---------- mock ----------
function harness(sc = {}, { me = null } = {}) {
  const routes = [];
  let n = 0;
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    wait: () => Promise.resolve(),
    db: { cards: F.projectCards(), ...(me ? { me } : {}) },
    sc,
    uid: () => (n += 1).toString(16),
    F,
    forbid: (user, roles) => (roles.some((r) => (user?.roles || [user?.role]).includes(r)) ? undefined
      : new Response(JSON.stringify({ code: 'forbidden', message: '没有权限', details: { required_roles: roles } }), { status: 403 })),
  };
  register(ctx);
  const admin = { ...F.USERS.zhangsan, roles: ['user', 'admin'] };
  const call = async (method, path, body, user = admin) => {
    const u = new URL(path, 'http://mock.example.invalid');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, user });
      if (res !== undefined) return { status: res.status, body: res.status === 204 ? null : await res.json() };
    }
    throw new Error(`${method} ${path} 没有路由`);
  };
  return { call, ctx };
}
const A = '/api/v1/admin';
const OPS = { ...F.USERS.ops01, roles: ['user', 'ops'] };
const USER = { ...F.USERS.modeler01, roles: ['user'] };

test('mock 角色把关：系统设置只给 admin，配置注册表 ops 也能读', async () => {
  const { call } = harness();
  assert.equal((await call('GET', `${A}/settings/model`, null, USER)).status, 403);
  assert.equal((await call('GET', `${A}/settings/model`, null, OPS)).status, 403);
  assert.equal((await call('PUT', `${A}/quotas`, {}, OPS)).status, 403);
  assert.equal((await call('GET', `${A}/config-registry`, null, OPS)).status, 200);
  assert.equal((await call('GET', `${A}/config-registry`, null, USER)).status, 403);
  assert.equal((await call('GET', `${A}/service-tokens`, null, OPS)).status, 403);
});

test('mock SY-01/02：内测引擎没装；保存只改传了的字段，Key 只回掩码，压缩阈值只校验不保存', async () => {
  const { call } = harness({ engine: 'missing' });
  const v = (await call('GET', `${A}/settings/model`)).body;
  assert.equal(v.engines.jiuwen.installed, false);
  assert.equal(v.engines.claude.configured, true);
  assert.equal((await call('PUT', `${A}/settings/model/claude`, { context: { compact_at_pct: 99 } })).status, 400);
  const r = await call('PUT', `${A}/settings/model/claude`, { api_key: 'new-secret', context: { compact_at_pct: 60 } });
  assert.equal(r.status, 200);
  assert.equal(r.body.context.compact_at_pct, 80);
  assert.ok(!JSON.stringify(r.body).includes('new-secret'));
  const w = await call('PUT', `${A}/settings/model/claude`, { context: { window: 128000 } });
  assert.equal(w.body.context.window, 128000);
  assert.equal(w.body.context.compact_at_pct, S.deriveCompact(128000, 32000));
  assert.equal(w.body.base_url, 'https://gateway.example.com/anthropic');
  assert.equal((await call('PUT', `${A}/settings/default-engine`, { engine: 'jiuwen' })).status, 409);
});

test('mock SY-04：个人 Key 策略与个人设置共用一份（db.me.modelSettings.personal_allowed）', async () => {
  const me = { modelSettings: { personal_allowed: false } };
  const { call } = harness({}, { me });
  assert.equal((await call('GET', `${A}/settings/model`)).body.personal_key_allowed, false);
  await call('PUT', `${A}/settings/personal-key-policy`, { allow: true });
  assert.equal(me.modelSettings.personal_allowed, true);
});

test('mock SY-05～SY-08：整份替换带 version，校验失败给 details.error 行列，停用后只剩内置表', async () => {
  const { call } = harness();
  const v = (await call('GET', `${A}/settings/context-presets`)).body;
  assert.equal(v.custom.status, 'ok');
  assert.equal(v.builtin_verified_at, S.BUILTIN_VERIFIED_AT);
  assert.deepEqual([v.custom.overrides, v.custom.added], [1, 1]);
  assert.ok(Array.isArray(v.rows[0].aliases) && Array.isArray(v.rows[0].sources));
  const stale = await call('PUT', `${A}/settings/context-presets/custom`, { version: 'old', presets: [] });
  assert.equal(stale.status, 409);
  const bad = await call('PUT', `${A}/settings/context-presets/custom`, { version: v.custom.version, presets: [{ model: 'a', window: 64000, max_output: 8000 }, { model: 'b', window: 64000, max_output: 64000 }] });
  assert.equal(bad.status, 400);
  assert.deepEqual(bad.body.details.error, { row: 2, column: 'max_output', message: '要小于上下文窗口' });
  const ok = await call('PUT', `${A}/settings/context-presets/custom`, { version: v.custom.version, presets: [...customPresets(v.rows), { model: 'model-x-64k', window: 64000, max_output: 8000 }] });
  assert.equal(ok.status, 200);
  assert.notEqual(ok.body.custom.version, v.custom.version);
  assert.equal(ok.body.custom.added, 2);
  const m = (await call('GET', `${A}/settings/context-presets/match?model=MODEL_X_64K`)).body;
  assert.deepEqual(m.preset, { window: 64000, max_output: 8000, origin: 'custom' });
  assert.equal((await call('GET', `${A}/settings/context-presets/match?model=nope`)).body.preset, null);
  const off = (await call('POST', `${A}/settings/context-presets/custom/disable`)).body;
  assert.equal(off.custom.status, 'disabled');
  assert.equal(off.custom.backup_available, true);
  assert.ok(off.rows.every((r) => r.origin === 'builtin'));
});

test('mock sys:ctxbad：自定义表有误，error 带行列，只用内置表', async () => {
  const { call } = harness({ sys: 'ctxbad' });
  const v = (await call('GET', `${A}/settings/context-presets`)).body;
  assert.equal(v.custom.status, 'invalid');
  assert.deepEqual(v.custom.error, S.CUSTOM_ERROR);
  assert.ok(v.rows.every((r) => r.origin === 'builtin'));
});

test('mock SY-12/13：整份替换保留测过的状态；sys:p1 下状态恒 unknown、工具为空、Bearer 被拒', async () => {
  const { call } = harness();
  const list = (await call('GET', `${A}/settings/mcp-servers`)).body.items;
  const saved = await call('PUT', `${A}/settings/mcp-servers`, { items: mcpItemsAfterSave(list, mcpItemFromForm({ name: 'doc', transport: 'stdio', target: '/opt/mcp/doc --stdio', auth: 'none', secret_ref: '' }), null) });
  assert.equal(saved.status, 200);
  assert.equal(saved.body.items[0].status, 'connected');
  assert.equal(saved.body.items[3].status, 'unknown');
  assert.equal((await call('POST', `${A}/settings/mcp-servers/test`, { server: list[2] })).body.ok, false);
  const p1 = harness({ sys: 'p1' });
  const items = (await p1.call('GET', `${A}/settings/mcp-servers`)).body.items;
  assert.ok(items.every((s) => s.status === 'unknown' && !s.tools.length && s.auth.kind === 'none'));
  const t = (await p1.call('POST', `${A}/settings/mcp-servers/test`, { server: items[0] })).body;
  assert.deepEqual([t.ok, t.tools], [true, []]);
  const bearer = mcpItemFromForm({ name: 'kb2', transport: 'http', target: 'https://mcp.example.com/kb', auth: 'bearer', secret_ref: 'cred://mcp/kb2' });
  const rej = await p1.call('PUT', `${A}/settings/mcp-servers`, { items: [bearer] });
  assert.equal(rej.status, 400);
  assert.ok(mcpSaveError({ code: rej.body.code, details: rej.body.details }));
});

test('mock SY-09～SY-11、SY-14：权限模式枚举；沙箱 boxdown / p1；注册表跟着设置走', async () => {
  const { call } = harness({ sys: 'boxdown' });
  assert.equal((await call('PUT', `${A}/settings/session`, { permission_mode: 'yolo' })).status, 400);
  assert.equal((await call('PUT', `${A}/settings/session`, { permission_mode: 'plan' })).body.permission_mode, 'plan');
  const box = (await call('GET', `${A}/settings/sandbox`)).body;
  assert.deepEqual([box.connected, box.packages.length], [false, 0]);
  assert.ok(box.detail);
  const reg = (await call('GET', `${A}/config-registry`)).body;
  assert.deepEqual(reg.precedence, ['env', 'data_dir', 'pack', 'default']);
  assert.equal(reg.items.find((x) => x.key === 'ONTO_WEB_PERMISSION_MODE').value, 'plan');
  assert.ok(reg.items.every((x) => Array.isArray(x.precedence) && typeof x.description === 'string'));
  assert.equal((await call('GET', `${A}/config-registry?effect=restart`)).body.items.length, 1);
  assert.equal((await call('GET', `${A}/config-registry?effect=bad`)).status, 400);
});

test('mock SY-15～SY-17：保存配额与平台限制；单独设置的增删与重复；sys:p1 时 404', async () => {
  const { call } = harness();
  const q = (await call('GET', `${A}/quotas`)).body;
  const put = await call('PUT', `${A}/quotas`, quotaBody({ ...quotaForm(q), concurrent_sessions: '3' }));
  assert.equal(put.body.per_user.concurrent_sessions, 3);
  assert.equal(put.body.per_project.online_engines, 1);
  const add = await call('POST', `${A}/quotas/overrides`, { subject: { kind: 'project', id: 'parcel-sort-6c1f2a90' }, item: 'monthly_tokens', value: 80000000, reason: '夜间批量回归' });
  assert.equal(add.status, 201);
  assert.equal(add.body.subject.label, '快递分拣异常处理');
  assert.equal((await call('POST', `${A}/quotas/overrides`, { subject: { kind: 'project', id: 'parcel-sort-6c1f2a90' }, item: 'monthly_tokens', value: 1, reason: 'x' })).status, 409);
  assert.equal((await call('POST', `${A}/quotas/overrides`, { subject: { kind: 'user', id: 'nobody' }, item: 'daily_tokens', value: 1, reason: 'x' })).status, 404);
  assert.equal((await call('POST', `${A}/quotas/overrides`, { subject: { kind: 'user', id: 'wanggong' }, item: 'monthly_tokens', value: 1, reason: 'x' })).status, 400);
  assert.equal((await call('DELETE', `${A}/quotas/overrides/${add.body.id}`)).status, 204);
  assert.equal((await call('GET', `${A}/quotas`)).body.overrides.length, 2);
  const l = (await call('GET', `${A}/autobuild-limits`)).body;
  assert.equal((await call('PUT', `${A}/autobuild-limits`, { ...limitsBody(limitsForm(l), l), max_concurrent_runs: 17 })).status, 400);
  const lp = await call('PUT', `${A}/autobuild-limits`, limitsBody({ ...limitsForm(l), max_concurrent_runs: '6' }, l));
  assert.deepEqual([lp.body.max_concurrent_runs, lp.body.running], [6, 2]);
  const p1 = harness({ sys: 'p1' });
  for (const path of ['/quotas', '/autobuild-limits', '/settings/pricing', '/service-tokens']) {
    assert.equal((await p1.call('GET', `${A}${path}`)).status, 404, path);
  }
});

test('mock SY-18：单价表保存与校验', async () => {
  const { call } = harness();
  const p = (await call('GET', `${A}/settings/pricing`)).body;
  assert.equal((await call('PUT', `${A}/settings/pricing`, { ...p, currency: 'EUR' })).status, 400);
  const ok = await call('PUT', `${A}/settings/pricing`, pricingBody({ ...pricingForm(p), currency: 'USD' }));
  assert.equal(ok.body.currency, 'USD');
  assert.equal(ok.body.items[2].cache_read_per_mtok, null);
});

test('mock X-06：明文只在创建响应里出现一次（占位串），列表只有摘要；限定项目要存在；吊销', async () => {
  const { call } = harness();
  const bad = await call('POST', `${A}/service-tokens`, { name: '评测', scopes: ['releases.read'], project_ids: ['nope'] });
  assert.equal(bad.status, 400);
  const r = await call('POST', `${A}/service-tokens`, serviceTokenBody({ name: '评测服务', scopes: ['releases.read'], limit: 'projects', project_ids: [F.MAIN_PID], ttl: '90' }));
  assert.equal(r.status, 201);
  assert.ok(SERVICE_TOKEN_RE.test(r.body.token));
  assert.match(r.body.token, /placeholder/);
  assert.deepEqual(r.body.summary.project_ids, [F.MAIN_PID]);
  const list = (await call('GET', `${A}/service-tokens`)).body.items;
  assert.equal(list[0].id, r.body.summary.id);
  assert.ok(!JSON.stringify(list).includes(r.body.token));
  assert.equal((await call('DELETE', `${A}/service-tokens/${r.body.summary.id}`)).status, 204);
  assert.equal((await call('DELETE', `${A}/service-tokens/${r.body.summary.id}`)).status, 404);
});
