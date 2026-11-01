// 知识沉淀页：页面纯逻辑（knowledgeModel）与 mock（K-01～K-08、在对话里复核）
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { lockScope, register, taskSeeds } from '../src/api/mock/routes-knowledge.js';
import {
  checkRows, conflictChip, conflictDetail, conflictLine, deleteImpact, emptyForm, entryLocked, footText, formBody, formFromDetail,
  inlineParts, isEmptyProject, layerInfo, layerSeg, listQuery, lockCopy, markSegments, overrideForm, overrideParts, quoteMeta,
  reviewCommand, richBlocks, scopeLabel, sourceMeta, usedByLabel, usedByTarget, validateForm,
} from '../src/pages/knowledge/knowledgeModel.js';

const P = `/api/v1/projects/${F.MAIN_PID}`;
const lockOf = (state) => ({
  state, slice: { id: 's5', name: '超量与审批提示' }, entries: [], artifacts: lockScope.artifacts, paths: [], conversation_id: 'c1',
});

function server({ sc = {}, lock = null } = {}) {
  const routes = [];
  const events = [];
  const tasks = [];
  const hooks = [];
  const timers = [];
  const cards = F.projectCards();
  const ctx = {
    on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }),
    json: (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
    err: (status, code, message, details = null) => new Response(JSON.stringify({ code, message, details }), { status }),
    db: {},
    sc,
    uid: (() => { let i = 0; return () => (i += 1).toString(16); })(),
    F,
    cardOf: (id) => cards.find((c) => c.id === id),
    scopeOf: () => lock || { state: 'none', entries: [], artifacts: [], paths: [] },
    emitProj: (pid, type, data) => events.push({ type, data }),
    commandHooks: hooks,
    putTask: (pid, task) => { tasks.push(task); return task; },
    later: (pid, cid, ms, fn) => timers.push(fn),
    convsOf: () => [{ id: 'c1' }],
  };
  register(ctx);
  const call = async (method, path, body) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {} });
      if (res !== undefined) return { status: res.status, body: await res.json() };
    }
    assert.fail(`${method} ${path} 没有路由`);
    return null;
  };
  return { call, events, tasks, hooks, timers, cards };
}

// ---------- 纯逻辑 ----------
test('范围、来源、筛选参数、分层条与列表尾注', () => {
  assert.equal(scopeLabel({ kind: 'global' }), '全局');
  assert.equal(scopeLabel({ kind: 'entries', entries: [{ kind: 'object', id: 'object:a', name: '陈列量标准' }] }), '对象：陈列量标准');
  assert.equal(scopeLabel({ kind: 'entries', entries: [{ kind: 'rule', name: 'R-09' }, { kind: 'object', name: '门店' }] }), '规则：R-09 等 2 个');
  const at = new Date(2026, 9, 28, 14, 20).toISOString();
  assert.deepEqual(sourceMeta({ kind: 'conversation', label: '对话', at }), { icon: 'msg', text: '对话 10-28' });
  assert.deepEqual(sourceMeta({ kind: 'material', label: '02-门店补货规则.docx §11.4' }), { icon: 'filetext', text: '02-门店补货规则.docx §11.4' });
  assert.equal(sourceMeta({ kind: 'pack', label: '零售连锁行业包' }).icon, 'pkg');
  assert.equal(sourceMeta({ kind: 'manual', label: '手动新增', at }).icon, 'edit');
  assert.deepEqual(listQuery({ layer: 'all', type: '', q: '  ' }), { limit: 20 });
  assert.deepEqual(listQuery({ layer: 'industry', type: 'term', q: ' 陈列 ', cursor: '20', limit: 20 }), { limit: 20, layer: 'industry', type: 'term', q: '陈列', cursor: '20' });
  assert.deepEqual(layerSeg({ all: 36, project: 12, industry: 24 }).map((x) => x[1]), ['全部 36', '项目 12', '行业包 24']);
  const info = layerInfo({ layers: [{ layer: 'industry', label: '零售连锁', count: 24, packs: ['industry-retail'] }, { layer: 'project', count: 12, overridden: 1 }], loading_rule: '规则' });
  assert.deepEqual(info, { industry: { count: 24, label: '零售连锁', pack: 'industry-retail' }, project: { count: 12, note: '同名条目覆盖行业包 · 已覆盖 1 条' }, rule: '规则' });
  assert.equal(footText(20, 36), '显示前 20 条 · 共 36 条，行业包条目只读，可以在项目里写同名条目覆盖');
  assert.equal(footText(12, 12), '共 12 条，行业包条目只读，可以在项目里写同名条目覆盖');
  assert.equal(isEmptyProject({ layers: [{ layer: 'project', count: 0 }] }), true);
  assert.equal(isEmptyProject({ layers: [{ layer: 'project', count: 3 }] }), false);
});

test('写入范围只读：锁里的知识条目「编辑」「删除」停用，两态说明不同', () => {
  const e = { id: 'kn-p03', title: '临时加量必须区域审批通过后才能下单' };
  assert.equal(entryLocked({ state: 'none', artifacts: lockScope.artifacts }, e), false);
  assert.equal(entryLocked(lockOf('assistant_running'), e), true);
  assert.equal(entryLocked(lockOf('pending_confirm'), { id: 'kn-p01' }), false);
  assert.equal(entryLocked(lockOf('pending_confirm'), { id: 'kn-p99', locked: true }), true);
  const run = lockCopy(lockOf('assistant_running'));
  assert.match(run.text.running[0], /助手正在改「超量与审批提示」这一片/);
  assert.match(run.pop, /确认生效或撤回前不能编辑、删除/);
  const pend = lockCopy(lockOf('pending_confirm'));
  assert.match(pend.text.pending[0], /修改还没确认/);
  assert.equal(lockCopy({ state: 'none' }), null);
  assert.match(lockCopy({ ...lockOf('autobuild_slice') }).pop, /这一片合入前/);
});

test('表单：校验、请求体、编辑回填、行业包条目覆盖', () => {
  assert.deepEqual(validateForm(emptyForm()), { title: '写一个条目名称', rule: '规则不能为空' });
  assert.deepEqual(validateForm({ ...emptyForm(), title: 'x'.repeat(61), rule: 'r' }), { title: '名称不超过 60 个字' });
  assert.deepEqual(validateForm({ ...emptyForm(), title: 'a', rule: 'r', scope: { kind: 'entries', entries: [] } }), { scope: '选一个适用范围' });
  const body = formBody({ ...emptyForm(), title: ' 陈列上限 ', rule: ' 先看到货周期 ', background: '' }, { base_revision: 'r1' });
  assert.deepEqual(body, { title: '陈列上限', type: 'domain_rule', scope: { kind: 'global' }, rule_md: '先看到货周期', confidence: 'medium', base_revision: 'r1' });
  const f = formFromDetail({ title: 't', type: 'term', scope: { kind: 'global' }, background_md: 'b', rule_md: 'r', confidence: 'high' });
  assert.deepEqual(f, { title: 't', type: 'term', scope: { kind: 'global' }, background: 'b', rule: 'r', confidence: 'high' });
  const o = overrideForm({ title: '陈列上限按品类取', type: 'domain_rule', scope: { kind: 'global' } });
  assert.equal(o.title, '陈列上限按品类取');
  assert.match(o.background, /覆盖行业包条目「陈列上限按品类取」/);
  assert.equal(o.rule, '');
});

test('冲突检查、删除影响、来源引用、提名检查项', () => {
  assert.deepEqual(conflictChip(null), { text: '保存时自动查重', tone: 'line', n: 0 });
  assert.equal(conflictChip({ checked_at: 'x', conflicts: [{}] }).text, '保存时自动查重 · 最近一次：1 处冲突');
  assert.equal(conflictChip({ checked_at: 'x', conflicts: [] }).tone, 'line');
  const c = { kind: 'scope_conflict', effective: 'project', project_entry: { id: 'kn-p01', title: '陈列上限按到货周期区分，不是按品类' }, industry_entry: { id: 'kn-i04', title: '陈列上限按品类取' } };
  assert.equal(conflictLine(c), '「陈列上限按到货周期区分」与行业包「陈列上限按品类取」口径不同');
  assert.equal(conflictDetail(c), '项目「陈列上限按到货周期区分」↔ 行业包「陈列上限按品类取」：项目优先，已生效');
  assert.deepEqual(reviewCommand(c), { command: '/knowledge-review', args: { project_entry: 'kn-p01', industry_entry: 'kn-i04' }, text: '复核知识口径冲突「陈列上限按到货周期区分 / 陈列上限按品类取」' });
  assert.equal(deleteImpact({ used_by: [1, 2, 3], overrides: { entry_title: '陈列上限取值' } }), '引用它的 3 个产物下次刷新时不再带上这条知识；被覆盖的行业包条目「陈列上限取值」会重新生效。');
  assert.equal(deleteImpact({ used_by: [] }), '目前没有产物引用这条知识。');
  assert.equal(usedByLabel({ kind: 'object', name: '陈列量标准' }), '对象 陈列量标准');
  assert.equal(usedByLabel({ type: 'trace.scenario', key: 's-03', title: '3 门店更严' }), '场景 3 门店更严');
  assert.deepEqual(usedByTarget({ type: 'trace.scenario', key: 's-03' }), { page: 'scenarios', query: { key: 's-03' } });
  assert.deepEqual(usedByTarget({ kind: 'object', id: 'object:x' }), { page: 'studio-table', query: { entry: 'object:x' } });
  assert.equal(quoteMeta({ conversation_id: 'c1', at: new Date(2026, 9, 28, 14, 20).toISOString(), by: '张工' }, { c1: '陈列量核定返工' }), '对话「陈列量核定返工」 · 10-28 14:20 · 张工');
  assert.deepEqual(overrideParts({ pack: 'industry-retail', entry_title: '陈列上限取值', note: '行业条目写的是「按品类取」' }, '零售连锁'), { target: '零售连锁行业包 / 陈列上限取值', note: '（行业条目写的是「按品类取」）' });
  const rows = checkRows([{ item: 'customer_name', found: 1, action: '已替换' }, { item: 'amount', found: 0, action: '' }]);
  assert.deepEqual(rows.map((r) => [r.label, r.result[0], r.action]), [['单位名称', '发现 1 处', '已替换'], ['金额', '未发现', '—']]);
  assert.deepEqual(markSegments('abcdef', [{ start: 2, end: 4 }]), [{ v: 'ab', mark: false }, { v: 'cd', mark: true }, { v: 'ef', mark: false }]);
});

test('正文：去掉开头的小标题，认段落、列表、[[条目]] 和粗体', () => {
  const blocks = richBlocks('## 规则\n取上限时先看 [[补货单]] 的到货周期\n\n- 缺失时**追问**\n- 不默认每日');
  assert.deepEqual(blocks.map((b) => b.t), ['p', 'li', 'li']);
  assert.deepEqual(blocks[0].parts, [{ t: 'text', v: '取上限时先看 ' }, { t: 'link', v: '补货单' }, { t: 'text', v: ' 的到货周期' }]);
  assert.deepEqual(inlineParts('缺失时**追问**'), [{ t: 'text', v: '缺失时' }, { t: 'b', v: '追问' }]);
});

// ---------- mock ----------
test('K-01：两层、筛选、分页、计数与加载规则；?mock=knowledge:empty 只清空项目层', async () => {
  const { call } = server();
  const all = await call('GET', `${P}/knowledge?limit=7`);
  assert.equal(all.status, 200);
  assert.deepEqual(all.body.counts, { all: 36, project: 12, industry: 24 });
  assert.equal(all.body.items.length, 7);
  assert.equal(all.body.next_cursor, '7');
  assert.equal(all.body.items[0].title, '陈列上限按到货周期区分，不是按品类');
  assert.equal(all.body.items[0].detail, undefined);
  assert.deepEqual(all.body.layers.map((l) => [l.layer, l.count, l.overridden]), [['industry', 24, 1], ['project', 12, 1]]);
  assert.match(all.body.loading_rule, /按「概念」匹配/);
  const ind = await call('GET', `${P}/knowledge?layer=industry&type=term`);
  assert.ok(ind.body.items.every((e) => e.layer === 'industry' && e.type === 'term' && e.readonly));
  const q = await call('GET', `${P}/knowledge?q=${encodeURIComponent('陈列上限')}`);
  assert.deepEqual(q.body.counts, { all: 2, project: 1, industry: 1 });
  const empty = server({ sc: { knowledge: 'empty' } });
  const e = await empty.call('GET', `${P}/knowledge`);
  assert.deepEqual(e.body.counts, { all: 24, project: 0, industry: 24 });
  assert.equal(isEmptyProject(e.body), true);
  const chk = await empty.call('GET', `${P}/knowledge/conflicts`);
  assert.equal(chk.body.checked_at, null);
  assert.equal(chk.body.total_entries, 24);
});

test('K-02 详情 / K-06 冲突检查', async () => {
  const { call } = server();
  const d = await call('GET', `${P}/knowledge/kn-p01`);
  assert.equal(d.body.overrides.entry_title, '陈列上限取值');
  assert.equal(d.body.source_quote.by, '张工');
  assert.equal(d.body.used_by.length, 3);
  const plain = await call('GET', `${P}/knowledge/kn-i05`);
  assert.equal(plain.body.readonly, true);
  assert.ok(plain.body.rule_md);
  const chk = await call('GET', `${P}/knowledge/conflicts`);
  assert.equal(chk.body.total_entries, 36);
  assert.equal(chk.body.conflicts[0].project_entry.id, 'kn-p01');
  const again = await call('POST', `${P}/knowledge/conflicts`);
  assert.equal(again.body.conflicts.length, 1);
  assert.equal(again.body.duplicates, 0);
});

test('K-03：对话里沉淀（只带全局范围）仍然可用；同名同范围 409；和行业包同名时覆盖并发起复核', async () => {
  const { call, events, tasks, timers } = server();
  const chat = await call('POST', `${P}/knowledge`, { title: '牛奶按箱补货', type: 'convention', scope: { kind: 'global' }, rule_md: '## 规则\n牛奶按箱补货', source: { conversation_id: 'c1', message_id: 'm1' } });
  assert.equal(chat.status, 201);
  assert.equal(chat.body.entry.source.kind, 'conversation');
  assert.equal(chat.body.version_label, 'v0.13');
  assert.deepEqual(chat.body.conflicts, []);
  assert.equal(chat.body.review_task, null);
  assert.ok(events.some((e) => e.type === 'knowledge.updated' && e.data.change === 'created'));
  const dup = await call('POST', `${P}/knowledge`, { title: '牛奶按箱补货', type: 'term', scope: { kind: 'global' }, rule_md: 'x' });
  assert.equal(dup.status, 409);
  assert.equal(dup.body.code, 'knowledge_duplicate');
  const bad = await call('POST', `${P}/knowledge`, { title: '', type: 'x', scope: null, rule_md: '' });
  assert.equal(bad.status, 400);
  assert.deepEqual(Object.keys(bad.body.details.fields).sort(), ['rule_md', 'scope', 'title', 'type']);
  const ov = await call('POST', `${P}/knowledge`, { title: '陈列上限按品类取', type: 'domain_rule', scope: { kind: 'global' }, rule_md: '按到货周期取' });
  assert.equal(ov.body.entry.overrides_industry, true);
  assert.equal(ov.body.entry.source.kind, 'manual');
  assert.equal(ov.body.conflicts[0].kind, 'scope_conflict');
  assert.equal(ov.body.review_task.origin.page, 'knowledge');
  timers.forEach((fn) => fn());
  assert.equal(tasks.at(-1).state, 'done');
  const chk = await call('GET', `${P}/knowledge/conflicts`);
  assert.equal(chk.body.conflicts.length, 2);
});

test('K-04 / K-05：行业层只读、版本冲突、写入范围锁 423、删除恢复被覆盖的行业条目', async () => {
  const { call } = server();
  const d = (await call('GET', `${P}/knowledge/kn-p01`)).body;
  const body = { title: d.title, type: d.type, scope: d.scope, rule_md: '新规则', base_revision: d.revision };
  const ind = await call('PUT', `${P}/knowledge/kn-i04`, body);
  assert.equal(ind.status, 403);
  assert.equal(ind.body.code, 'artifact_readonly');
  const stale = await call('PUT', `${P}/knowledge/kn-p01`, { ...body, base_revision: 'old' });
  assert.equal(stale.status, 409);
  const ok = await call('PUT', `${P}/knowledge/kn-p01`, body);
  assert.equal(ok.status, 200);
  assert.notEqual(ok.body.entry.revision, d.revision);
  assert.equal((await call('GET', `${P}/knowledge/kn-p01`)).body.rule_md, '新规则');
  const del = await call('DELETE', `${P}/knowledge/kn-p01`, { base_revision: ok.body.entry.revision });
  assert.equal(del.body.restored_industry_entry, '陈列上限取值');
  assert.equal(del.body.affected_artifacts, 3);
  assert.equal((await call('GET', `${P}/knowledge/conflicts`)).body.conflicts.length, 0);
  assert.equal((await call('GET', `${P}/knowledge/kn-p01`)).status, 404);

  const locked = server({ lock: lockOf('pending_confirm') });
  const d3 = (await locked.call('GET', `${P}/knowledge/kn-p03`)).body;
  const put = await locked.call('PUT', `${P}/knowledge/kn-p03`, { title: d3.title, type: d3.type, scope: d3.scope, rule_md: 'x', base_revision: d3.revision });
  assert.equal(put.status, 423);
  assert.equal(put.body.code, 'write_scope_locked');
  assert.equal((await locked.call('DELETE', `${P}/knowledge/kn-p03`, {})).status, 423);
  assert.equal((await locked.call('DELETE', `${P}/knowledge/kn-p02`, {})).status, 200);
});

test('K-07 / K-08 提名回流；「在对话里复核」命令跑完改冲突说明', async () => {
  const { call, hooks, events } = server();
  const pv = await call('POST', `${P}/knowledge/kn-p01/nomination-preview`, { industry_pack: 'industry-retail' });
  assert.equal(pv.body.pack.label, '零售连锁');
  assert.equal(pv.body.checks.length, 4);
  assert.equal(pv.body.sanitized_text.slice(pv.body.highlights[0].start, pv.body.highlights[0].end), '示例门店');
  assert.equal((await call('POST', `${P}/knowledge/kn-i01/nomination-preview`, {})).status, 400);
  const no = await call('POST', `${P}/knowledge/kn-p01/nominations`, { industry_pack: 'industry-retail', sanitized_text: pv.body.sanitized_text });
  assert.equal(no.status, 400);
  const yes = await call('POST', `${P}/knowledge/kn-p01/nominations`, { industry_pack: 'industry-retail', sanitized_text: pv.body.sanitized_text, confirm_general: true });
  assert.equal(yes.status, 201);
  assert.equal(yes.body.state, 'pending_review');
  const hook = hooks.find((h) => h.match({ command: '/knowledge-review' }));
  assert.equal(hook.title({ text: '复核知识口径冲突「a / b」' }), '助手正在复核知识口径冲突「a / b」');
  hook.finish(F.MAIN_PID, { args: { project_entry: 'kn-p01' } });
  assert.match((await call('GET', `${P}/knowledge/conflicts`)).body.conflicts[0].note, /复核过/);
  assert.ok(events.some((e) => e.type === 'knowledge.updated' && e.data.ids[0] === 'kn-p01'));
  assert.equal(taskSeeds.knowledge().origin.page, 'knowledge');
});
