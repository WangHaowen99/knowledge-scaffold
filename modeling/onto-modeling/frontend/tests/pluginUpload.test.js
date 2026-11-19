// 上传与审核（plugin-upload，PL-13～PL-18）：页面纯逻辑与 mock
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { failedChecks, passedChecks, reportMarkdown, seedUploads, summaryOf } from '../src/api/mock/pack-uploads-data.js';
import { finishCheck, register } from '../src/api/mock/routes-pack-uploads.js';
import {
  PACK_MAX_BYTES, canReject, checkView, contributesText, defaultPublishForm, effectText, eligibleProjects,
  fmtClock, installBlock, isPublishable, kindText, orderUploads, packLimit, pickUpload, publishBlock, publishBody, publishToast, reasonProblem,
  rejectDraft, rolloutEstimate, rolloutProblem, segUploads, summaryText, timelineRows,
  uploadErrorText, uploadMeta, uploadStatus, uploadTitle, validatePackFile,
} from '../src/pages/plugins/uploadModel.js';

const MB = 1024 * 1024;
const up = (over = {}) => ({
  id: 'up-1', filename: 'industry-retail-1.1.0.zip', size: 412 * 1024, uploaded_at: '2026-11-01T13:36:00.000Z', uploaded_by: 'zhangsan',
  tenant_label: null, state: 'passed', summary: { pass: 13, warn: 1, fail: 0 }, manifest_ok: true, checksums_ok: true,
  name: 'industry-retail', version: '1.1.0', ...over,
});

// ---------- 上传 ----------

test('文件类型与大小：只收 .zip、不能是空文件、不超过上限', () => {
  assert.equal(validatePackFile({ name: 'industry-retail-1.1.0.zip', size: 412 * 1024 }), null);
  assert.equal(validatePackFile({ name: 'PACK.ZIP', size: 10 }), null);
  assert.equal(validatePackFile({ name: 'notes.txt', size: 10 }), '只支持 .zip 离线包，「notes.txt」不是 zip');
  assert.equal(validatePackFile({ name: 'a.zip', size: 0 }), '「a.zip」是空文件');
  assert.equal(validatePackFile({ name: 'big.zip', size: 230 * MB }), '「big.zip」有 230 MB，超过 200 MB 上限');
  assert.equal(validatePackFile({ name: 'big.zip', size: 230 * MB }, 300 * MB), null);
  assert.equal(validatePackFile({ name: 'edge.zip', size: PACK_MAX_BYTES }), null);
  assert.equal(validatePackFile(null), '没有选中文件');
  assert.equal(packLimit({ limits: { pack_upload_bytes: 50 * MB } }), 50 * MB);
  assert.equal(packLimit(null), PACK_MAX_BYTES);
});

test('上传时间：今天只写时刻，今年写月-日，更早写日期', () => {
  const now = new Date(2026, 10, 1, 22, 0).getTime();
  assert.equal(fmtClock(new Date(2026, 10, 1, 21, 36).toISOString(), now), '21:36');
  assert.equal(fmtClock(new Date(2026, 9, 30, 9, 5).toISOString(), now), '10-30 09:05');
  assert.equal(fmtClock(new Date(2025, 0, 2, 9, 5).toISOString(), now), '2025-01-02');
  assert.equal(fmtClock(null, now), '');
  assert.equal(fmtClock('不是时间', now), '');
});

test('检查结果汇总：「9 通过 · 1 警告」，没有的项不写', () => {
  assert.equal(summaryText({ pass: 9, warn: 1, fail: 0 }), '9 通过 · 1 警告');
  assert.equal(summaryText({ pass: 9, warn: 1, fail: 4 }), '9 通过 · 1 警告 · 4 失败');
  assert.equal(summaryText({ pass: 14, warn: 0, fail: 0 }), '14 通过');
  assert.equal(summaryText({ pass: 0, warn: 0, fail: 1 }), '1 失败');
  assert.equal(summaryText(null), '没有检查项');
  assert.deepEqual(checkView({ result: 'fail' }), { cls: 'bad', icon: 'x', label: '失败', tone: 'red' });
  assert.equal(checkView({ result: 'pass' }).cls, 'ok');
  assert.equal(checkView({ result: 'odd' }).label, 'odd');
});

test('上传列表一行：状态标、说明（清单、校验和只在静态检查里逐项列，行上不单标）', () => {
  assert.deepEqual(uploadStatus(up()), { tone: 'blue', text: '检查完成' });
  assert.deepEqual(uploadStatus(up({ state: 'checking' })), { running: true, text: '检查中' });
  assert.deepEqual(uploadStatus(up({ state: 'failed', summary: { pass: 9, warn: 1, fail: 4 } })), { tone: 'red', icon: 'x', text: '4 项失败' });
  assert.equal(uploadStatus(up({ state: 'published' })).text, '已发布');
  assert.deepEqual(uploadStatus(up({ state: 'rejected', summary: { pass: 9, warn: 1, fail: 4 } })), { tone: 'gray', icon: 'undo', text: '已退回' });
  assert.equal(uploadStatus(up({ state: 'held' })).text, '暂不发布');
  const now = new Date(2026, 10, 1, 22, 0).getTime();
  const at = new Date(2026, 10, 1, 21, 52).toISOString();
  assert.equal(uploadMeta(up({ size: 88 * 1024, uploaded_at: at, tenant_label: '示例租户' }), now), '88 KB · 上传于 21:52 · 租户 · 示例租户');
  assert.equal(uploadMeta(up({ uploaded_at: at }), now), '412 KB · 上传于 21:52');
  assert.equal(uploadTitle(up()), 'industry-retail 1.1.0');
  assert.equal(uploadTitle(up({ name: null, version: null })), 'industry-retail-1.1.0.zip');
});

test('列表顺序与默认选中：待审的在前；默认选第一个能发布的，地址参数优先', () => {
  const a = up({ id: 'a', state: 'published', uploaded_at: '2026-11-01T14:00:00Z' });
  const b = up({ id: 'b', state: 'failed', summary: { pass: 9, warn: 1, fail: 4 }, uploaded_at: '2026-11-01T13:00:00Z' });
  const c = up({ id: 'c', state: 'passed', uploaded_at: '2026-11-01T12:00:00Z' });
  const d = up({ id: 'd', state: 'checking', uploaded_at: '2026-11-01T14:30:00Z' });
  assert.deepEqual(orderUploads([a, b, c, d]).map((x) => x.id), ['d', 'b', 'c', 'a']);
  assert.equal(pickUpload([a, b, c, d]), 'c');
  assert.equal(pickUpload([a, b, c, d], 'a'), 'a');
  assert.equal(pickUpload([a, b, c, d], 'nope'), 'c');
  assert.equal(pickUpload([a, b]), 'b');
  assert.equal(pickUpload([a]), 'a');
  assert.equal(pickUpload([]), null);
  // 检查卡上的切换：待审的最多 3 个，选中的已发布那个补在后面
  assert.deepEqual(segUploads([a, b, c, d], 'a').map(([id]) => id), ['d', 'b', 'c', 'a']);
  assert.deepEqual(segUploads([a, b, c, d], 'c', 2).map(([id]) => id), ['d', 'b', 'c']);
  assert.deepEqual(segUploads([b, c], 'c'), [['b', 'industry-retail 1.1.0'], ['c', 'industry-retail 1.1.0']]);
});

test('清单摘要：kind、贡献、生效时机', () => {
  const m = { kind: 'knowledge', contributes: { commands: 1, templates: 3, nodes: ['retail.compliance'], gates: 1, config: 2, pages: [], hooks: 0 } };
  assert.equal(kindText(m), 'knowledge（行业包）');
  assert.equal(kindText({ ...m, category: 'system' }), 'knowledge（系统知识包）');
  assert.equal(kindText({ kind: 'node' }), 'node（能力包）'); // 节点包归进能力包
  assert.equal(kindText(null), '');
  assert.equal(contributesText(m.contributes), '命令 1 · 模板 3 · 节点 1 · 关卡 1 · 可调设置 2');
  assert.equal(contributesText({}), '没有声明贡献');
  assert.equal(effectText(m), '数据现读即生效；新增的步骤从下一条消息开始生效');
  assert.equal(effectText({ kind: 'knowledge', contributes: { templates: 2 } }), '数据现读即生效');
  assert.equal(effectText({ kind: 'capability', contributes: { commands: 2 } }), '下一条消息生效');
  assert.equal(effectText({ kind: 'provider' }), '管理员在插件中心信任后生效'); // ADR-0104
  assert.match(effectText({ kind: 'exporter' }), /安装到平台/);
});

// ---------- 发布 ----------

test('能否发布：检查通过且没有失败项才能发布；其余给说明', () => {
  assert.equal(isPublishable(up()), true);
  assert.equal(isPublishable(up({ state: 'held' })), true);
  assert.equal(isPublishable(up({ state: 'passed', summary: { pass: 1, warn: 0, fail: 1 } })), false);
  assert.equal(isPublishable(null), false);
  assert.equal(publishBlock(up()), null);
  assert.equal(publishBlock(up({ state: 'checking' })), '检查完成后才能发布');
  assert.equal(publishBlock(up({ state: 'failed', summary: { pass: 9, warn: 1, fail: 4 } })), '4 项失败，不能发布');
  assert.equal(publishBlock(up({ state: 'published' })), '已发布');
  assert.equal(publishBlock(up({ state: 'rejected' })), '已退回');
  assert.equal(canReject(up({ state: 'failed' })), true);
  assert.equal(canReject(up({ state: 'published' })), false);
  assert.equal(canReject(up({ state: 'checking' })), false);
});

test('灰度表单校验：通道、方式、百分比 0～100、范围不能为空、不重复', () => {
  const f = defaultPublishForm();
  assert.deepEqual(f, { channel: 'beta', by: 'project', percent: 10, targets: [], autoBreaker: true });
  assert.equal(rolloutProblem(f), null);
  assert.equal(rolloutProblem({ ...f, channel: 'alpha' }), '选一个发布通道');
  assert.equal(rolloutProblem({ ...f, by: 'region' }), '选择按项目还是按租户灰度');
  assert.equal(rolloutProblem({ ...f, percent: 101 }), '灰度百分比要在 0～100 之间');
  assert.equal(rolloutProblem({ ...f, percent: 12.5 }), '灰度百分比要在 0～100 之间');
  assert.equal(rolloutProblem({ ...f, percent: 0 }), '灰度范围是空的：拉一个百分比，或指定项目');
  assert.equal(rolloutProblem({ ...f, by: 'tenant', percent: 0 }), '灰度范围是空的：拉一个百分比，或指定租户');
  assert.equal(rolloutProblem({ ...f, percent: 0, targets: [{ id: 'p1', label: '门店补货单编制' }] }), null);
  assert.equal(rolloutProblem({ ...f, targets: [{ id: 'p1' }, { id: 'p1' }] }), '指定的对象有重复');
  assert.equal(rolloutProblem({ ...f, targets: Array.from({ length: 51 }, (_, i) => ({ id: `p${i}` })) }), '指定的对象最多 50 个');
});

test('发布请求体与说明：rollout、auto_breaker 按契约；项目数按 O-01 估', () => {
  const f = { ...defaultPublishForm(), targets: [{ id: 'retail-restock-3f9a1c2d', label: '门店补货单编制' }] };
  assert.deepEqual(publishBody(f), { channel: 'beta', rollout: { by: 'project', percent: 10, targets: ['retail-restock-3f9a1c2d'] }, auto_breaker: true });
  assert.deepEqual(publishBody({ ...defaultPublishForm(), channel: 'stable', autoBreaker: false }), { channel: 'stable', rollout: { by: 'project', percent: 10 }, auto_breaker: false });
  assert.equal(eligibleProjects({ projects: { total: 38, active: 24, archived: 6 } }), 32);
  assert.equal(eligibleProjects({}), null);
  assert.equal(rolloutEstimate({ by: 'project', percent: 10 }, 38), '约 4 个项目');
  assert.equal(rolloutEstimate({ by: 'project', percent: 10 }, null), '约 10% 的项目');
  assert.equal(rolloutEstimate({ by: 'project', percent: 100 }, 38), '全部项目');
  assert.equal(rolloutEstimate({ by: 'tenant', percent: 20 }, 38), '约 20% 的租户');
  assert.equal(publishToast(up(), defaultPublishForm()), 'industry-retail 1.1.0 已发布到 beta，灰度 10%');
  assert.equal(publishToast(up(), f), 'industry-retail 1.1.0 已发布到 beta，灰度 10%，另指定 1 个项目');
});

test('错误码：pack_version_exists、static_check_failed、pack_upload_invalid 等', () => {
  const e = (code, details = null, serverMessage = '') => ({ code, details, serverMessage, status: 409 });
  assert.equal(uploadErrorText(e('pack_version_exists'), up()), '包仓库里已经有 industry-retail 1.1.0，内容和这次上传的不一样。版本目录不可修改，请改版本号重新打包上传。');
  assert.equal(uploadErrorText(e('static_check_failed', { failed: ['写入范围不碰受保护目录'] })), '静态检查有失败项，不能发布：写入范围不碰受保护目录');
  assert.equal(uploadErrorText(e('pack_upload_state', { state: 'rejected' })), '这个上传已经退回了');
  assert.equal(uploadErrorText(e('pack_upload_state', { state: 'published' })), '这个上传已经发布了');
  assert.equal(uploadErrorText(e('pack_upload_invalid', { reason: 'manifest_missing' })), '包里缺少 manifest.yaml');
  assert.equal(uploadErrorText(e('pack_upload_invalid', { reason: 'other' }, '包坏了')), '包坏了');
  assert.equal(uploadErrorText(e('payload_too_large', { limit_bytes: 200 * MB })), '插件包超过 200 MB 上限');
});

// ---------- 审核记录与退回 ----------

test('审核记录：上传、静态检查（用时）、发布、退回，末尾补当前在等什么', () => {
  const at = (s) => new Date(Date.UTC(2026, 10, 1, 13, 36, s)).toISOString();
  const base = up({ timeline: [{ at: at(0), actor: 'zhangsan', text: '上传', state: 'checking' }, { at: at(4), actor: 'system', text: '静态检查通过', state: 'passed' }] });
  const nameOf = (x) => (x === 'zhangsan' ? '管理员 张工' : x);
  const rows = timelineRows(base, { nameOf });
  assert.equal(rows.length, 3);
  assert.equal(rows[0].title, '上传 industry-retail-1.1.0.zip');
  assert.match(rows[0].meta, /^管理员 张工 · /);
  assert.equal(rows[1].title, '静态检查：13 通过 · 1 警告');
  assert.match(rows[1].meta, / · 用时 4 秒$/);
  assert.deepEqual([rows[2].title, rows[2].dot, rows[2].icon, rows[2].meta], ['待发布', 'wait', 'hand', '']);
  const checking = timelineRows(up({ state: 'checking', timeline: base.timeline.slice(0, 1) }), { nameOf });
  assert.deepEqual([checking[1].title, checking[1].spin, checking[1].meta], ['静态检查中', true, '']);
  const failed = timelineRows(up({ state: 'failed', summary: { pass: 9, warn: 1, fail: 4 }, tenant_label: '示例租户', timeline: [base.timeline[0], { ...base.timeline[1], state: 'failed' }] }), { nameOf });
  assert.match(failed[0].meta, /租户 · 示例租户/);
  assert.equal(failed[1].dot, 'err');
  assert.deepEqual([failed[2].title, failed[2].meta], ['待退回', '']);
  const rejected = timelineRows(up({ state: 'rejected', timeline: [...base.timeline, { at: at(60), actor: 'zhangsan', text: '退回：版本号写错了', state: 'rejected' }] }), { nameOf });
  assert.equal(rejected.length, 3);
  assert.deepEqual([rejected[2].title, rejected[2].note], ['退回', '版本号写错了']);
  const published = timelineRows(up({ state: 'published', timeline: [...base.timeline, { at: at(90), actor: 'zhangsan', text: '发布到 beta', state: 'published' }] }), { nameOf });
  assert.equal(published.at(-1).title, '发布到 beta');
  assert.deepEqual(timelineRows(null), []);
});

test('退回原因：初稿列出失败项；必填、最多 500 字', () => {
  assert.equal(rejectDraft({ checks: failedChecks('demo-trace-ext') }), '静态检查有 4 项失败：「没有新增实体类型」「写入范围不碰受保护目录」「hooks 只追加拒绝型」「页面是平台内置或本包的隔离页面，前端代码只在 ui/ 下」。请修改后重新上传。');
  assert.equal(rejectDraft({ checks: passedChecks('industry-retail', '1.1.0') }), '');
  assert.equal(reasonProblem('  '), '请写明退回原因');
  assert.equal(reasonProblem('x'.repeat(501)), '退回原因最多 500 字');
  assert.equal(reasonProblem('版本号写错了'), null);
});

// ---------- mock 数据 ----------

test('mock 数据：检查项 id 与后端一致；通过的包 13 通过 · 1 警告，租户包 9 通过 · 1 警告 · 4 失败（前端代码判失败）', () => {
  const ids = ['manifest', 'checksums', 'symlinks', 'declared', 'compat', 'names', 'entity_types', 'write_scope', 'node_entry', 'hooks', 'frontend', 'secrets', 'syntax', 'prompt_budget'];
  assert.deepEqual(passedChecks('industry-retail', '1.1.0').map((c) => c.id), ids);
  assert.deepEqual(summaryOf(passedChecks('industry-retail', '1.1.0')), { pass: 13, warn: 1, fail: 0 });
  const bad = failedChecks('demo-trace-ext');
  assert.deepEqual(summaryOf(bad), { pass: 9, warn: 1, fail: 4 });
  assert.equal(bad.find((c) => c.id === 'frontend').result, 'fail');
  const seeds = seedUploads(Date.now());
  assert.deepEqual(seeds.map((u) => [u.filename, u.state, u.tenant_label]), [['industry-retail-1.1.0.zip', 'passed', null], ['demo-trace-ext-0.1.0.zip', 'failed', '示例租户']]);
  assert.equal(seedUploads(Date.now(), { checking: true })[0].state, 'checking');
  assert.deepEqual(seedUploads(Date.now(), { empty: true }), []);
  assert.match(reportMarkdown(seeds[1]), /^# 静态检查报告：demo-trace-ext 0\.1\.0\n\n\| 检查项 \| 结果 \| 说明 \|/);
  const rec = { filename: 'industry-property-1.3.0.zip', size: 40960, tenant_label: null, timeline: [] };
  finishCheck(rec);
  assert.deepEqual([rec.state, rec.name, rec.version, rec.timeline.at(-1).text], ['passed', 'industry-property', '1.3.0', '静态检查通过']);
});

// ---------- mock 路由 ----------

function harness(sc = {}) {
  const routes = [];
  const events = [];
  let n = 0;
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db: {}, sc, F, uid: () => (n += 1).toString(16),
    forbid: (user, roles) => (roles.some((r) => (user?.roles || [user?.role]).includes(r)) ? undefined : new Response(JSON.stringify({ code: 'forbidden' }), { status: 403 })),
    emitUser: (type, data) => events.push({ type, data }),
  };
  register(ctx);
  const admin = { ...F.USERS.zhangsan, roles: ['user', 'admin'] };
  const call = async (method, path, body, { user = admin, form = null } = {}) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, form, user });
      if (res !== undefined) return { status: res.status, body: res.headers.get('Content-Type')?.includes('json') || res.status >= 400 ? await res.json() : await res.text(), headers: res.headers };
    }
    return { status: 404 };
  };
  return { call, ctx, events };
}
const U = '/api/v1/admin/packs/uploads';

test('mock：只有管理员能调；列表、详情、检查报告', async () => {
  const h = harness();
  const modeler = { ...F.USERS.modeler01, roles: ['user'] };
  assert.equal((await h.call('GET', U, null, { user: modeler })).status, 403);
  const list = await h.call('GET', U);
  assert.deepEqual(list.body.items.map((x) => x.state), ['passed', 'failed']);
  assert.equal('checks' in list.body.items[0], false);
  const failedOnly = await h.call('GET', `${U}?state=failed`);
  assert.equal(failedOnly.body.items.length, 1);
  const id = list.body.items[1].id;
  const detail = await h.call('GET', `${U}/${id}`);
  assert.equal(detail.body.checks.length, 14);
  assert.equal(detail.body.timeline.length, 2);
  const report = await h.call('GET', `${U}/${id}/report`);
  assert.equal(report.status, 200);
  assert.match(report.headers.get('Content-Disposition'), /report\.md/);
  assert.match(report.body, /\| 页面是平台内置或本包的隔离页面，前端代码只在 ui\/ 下 \| 失败 \|/);
});

test('mock：上传校验类型、大小、清单；收下后是 checking', async () => {
  const h = harness();
  const form = (name, size) => {
    const fd = new FormData();
    fd.append('file', new File([new Uint8Array(size)], name));
    return fd;
  };
  const bigForm = new FormData();
  const big = new File([new Uint8Array(4)], 'big.zip');
  bigForm.append('file', big);
  const realGet = bigForm.get.bind(bigForm);
  bigForm.get = (k) => (k === 'file' ? Object.defineProperty(realGet(k), 'size', { value: 230 * MB }) : realGet(k));
  assert.equal((await h.call('POST', U, null, { form: bigForm })).status, 413);
  const txt = await h.call('POST', U, null, { form: form('notes.txt', 8) });
  assert.deepEqual([txt.status, txt.body.code, txt.body.details.reason], [400, 'pack_upload_invalid', 'not_zip']);
  const nm = await h.call('POST', U, null, { form: form('demo-nomanifest-0.1.0.zip', 8) });
  assert.equal(nm.body.details.reason, 'manifest_missing');
  const ok = await h.call('POST', U, null, { form: form('industry-property-1.3.0.zip', 8) });
  assert.deepEqual([ok.status, ok.body.state, ok.body.name, ok.body.uploaded_by], [202, 'checking', null, 'zhangsan']);
  const demo = await h.call('POST', U, null, { form: form('demo-x-0.2.0.zip', 8) });
  assert.equal(demo.body.tenant_label, '示例租户');
  const list = await h.call('GET', U);
  assert.equal(list.body.items.length, 4);
});

test('mock：发布写进通道，失败项 422、已发布 409、同名同版本内容不同 409 pack_version_exists', async () => {
  const h = harness();
  const [good, bad] = (await h.call('GET', U)).body.items;
  const body = { channel: 'beta', rollout: { by: 'project', percent: 10 }, auto_breaker: true };
  const fail = await h.call('POST', `${U}/${bad.id}/publish`, body);
  assert.deepEqual([fail.status, fail.body.code, fail.body.details.failed.length], [422, 'static_check_failed', 4]);
  assert.equal((await h.call('POST', `${U}/${good.id}/publish`, { ...body, channel: 'alpha' })).status, 400);
  assert.equal((await h.call('POST', `${U}/${good.id}/publish`, { ...body, rollout: { by: 'project', percent: 120 } })).status, 400);
  const ok = await h.call('POST', `${U}/${good.id}/publish`, body);
  assert.deepEqual(ok.body, { pack: { name: 'industry-retail', version: '1.1.0', label: '零售连锁' }, channel: 'beta', rollout: { by: 'project', percent: 10 } });
  const again = await h.call('POST', `${U}/${good.id}/publish`, body);
  assert.deepEqual([again.status, again.body.code, again.body.details.state], [409, 'pack_upload_state', 'published']);
  const last = (await h.call('GET', `${U}/${good.id}`)).body.timeline.at(-1);
  assert.deepEqual([last.text, last.state, last.actor], ['发布到 beta', 'published', 'zhangsan']);
  // 再传一个同名同版本的包：检查通过后发布，仓库里已有内容不同的 1.1.0 → 409
  const dup = h.ctx.db.packUploads[0];
  const twin = { ...dup, id: 'up-twin', state: 'passed', timeline: [] };
  h.ctx.db.packUploads.push(twin);
  const exists = await h.call('POST', `${U}/up-twin/publish`, body);
  assert.deepEqual([exists.status, exists.body.code], [409, 'pack_version_exists']);
});

test('mock：upload:exists 让 industry-retail 1.1.0 一发布就 409', async () => {
  const h = harness({ upload: 'exists' });
  const [good] = (await h.call('GET', U)).body.items;
  const res = await h.call('POST', `${U}/${good.id}/publish`, { channel: 'stable', rollout: { by: 'project', percent: 100 }, auto_breaker: false });
  assert.deepEqual([res.status, res.body.code], [409, 'pack_version_exists']);
  const checking = harness({ upload: 'checking+empty' });
  assert.equal((await checking.call('GET', U)).body.items.length, 0);
});

test('mock：退回要写原因，退回后不能再发布', async () => {
  const h = harness();
  const [, bad] = (await h.call('GET', U)).body.items;
  const empty = await h.call('POST', `${U}/${bad.id}/reject`, { reason: ' ' });
  assert.deepEqual([empty.status, empty.body.code], [400, 'invalid_argument']);
  const ok = await h.call('POST', `${U}/${bad.id}/reject`, { reason: '带了前端代码' });
  assert.equal(ok.body.state, 'rejected');
  const detail = await h.call('GET', `${U}/${bad.id}`);
  assert.equal(detail.body.timeline.at(-1).text, '退回：带了前端代码');
  assert.equal((await h.call('POST', `${U}/${bad.id}/reject`, { reason: 'x' })).status, 409);
  assert.equal((await h.call('POST', `${U}/${bad.id}/publish`, { channel: 'beta' })).status, 409);
});

// ---------- 普通用户装自己的包（PL-22～PL-26） ----------

test('普通用户：上限 20 MiB，检查通过才能安装，装好后不再出待安装', () => {
  assert.equal(packLimit(null, true), 20 * MB);
  assert.equal(packLimit({ limits: { pack_private_upload_bytes: 5 * MB } }, true), 5 * MB);
  assert.equal(installBlock(up({ state: 'passed' })), null);
  assert.equal(installBlock(up({ state: 'checking' })), '检查完成后才能安装');
  assert.equal(installBlock(up({ state: 'failed', summary: { pass: 3, warn: 0, fail: 2 } })), '2 项失败，不能安装');
  assert.equal(installBlock(up({ state: 'installed' })), '已安装');
  assert.equal(uploadStatus(up({ state: 'installed' })).text, '已安装');
  const titles = (u) => timelineRows({ ...u, timeline: [] }, { mine: true }).map((r) => r.title);
  assert.deepEqual(titles(up({ state: 'passed' })), ['待安装']);
  assert.deepEqual(titles(up({ state: 'installed' })), []);
  assert.deepEqual(titles(up({ state: 'failed', summary: { pass: 3, warn: 0, fail: 2 } })), []);
});

test('管理员看普通用户的上传：写是谁的，能转成公开发布，但不再提示待发布', () => {
  const u = up({ scope: 'private', uploaded_by: 'operator_a', state: 'installed' });
  assert.match(uploadMeta(u), /私有 · operator_a$/);
  assert.doesNotMatch(uploadMeta(u, Date.now(), true), /私有/);
  assert.equal(isPublishable(u), true);
  assert.deepEqual(timelineRows({ ...u, timeline: [] }), []);
});
