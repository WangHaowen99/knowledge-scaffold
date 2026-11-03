// mock · 3.17 插件产物（trace.scenario 场景、trace.mock 示例数据）：场景走查页。
// 同一路径的 AR 路由先由 routes-artifacts.js 试（它只管 diagram.flowchart，别的类型返回 undefined 交到这里）。
// 页面命令（S-20，commandHooks）：批量验证、补齐过期场景、交互走查、按场景补生成，按步推进，跑完改场景状态并发 artifacts.updated。
// 演示参数：
//   ?mock=task:scenarios          一打开就在「批量推演 23 个场景」（已推演 9 / 23，静止不动）
//   ?mock=scope:running|pending   写入范围锁两态：场景目录 ext/trace/scenarios/** 在锁里，23 个场景全部只读
//   ?mock=scope:pending,trace:batch  这把锁来自批量推演（node=trace.trace）：只读条写「批量推演跑完了，结果还没确认」
// 批量验证走完整的两态：开跑时 23 个场景进写入范围锁（助手运行中），跑完结果放进一轮变更集（修改待确认，对话里出「这一轮」卡），
// 这一轮结束就合入、解锁（F3）；开跑时项目已经有别的锁（如 scope 参数）就不动锁，结果直接回到列表。
import { isLocked } from '../../state/readonly.js';
import { SLICES } from './scenario.js';
import * as D from './scenarios-data.js';

const SCENARIO = 'trace.scenario';
const MOCK = 'trace.mock';
const ORIGIN = 'scenarios';

const RESULT = {
  pass: { value: 'pass', label: '通过', tone: 'teal' },
  fail: { value: 'fail', label: '失败', tone: 'red' },
  stale: { value: 'stale', label: '过期', tone: 'dash' },
  not_run: { value: 'not_run', label: '未推演', tone: 'gray' },
};

export const SCENARIO_TYPE = {
  type: SCENARIO, title: '场景', pack: 'trace', pack_version: '0.9.1', format: 'md-frontmatter',
  path: 'ext/trace/scenarios/{key}.md', key: 'filename',
  schema: {
    version: '1',
    fields: [
      { name: 'title', type: 'text', label: '场景', required: true },
      { name: 'slice', type: 'enum', label: '分段', required: true, enum: SLICES.map((x) => ({ value: x.id, label: x.name })) },
      { name: 'operation', type: 'ref', label: '推演的操作', required: false },
      { name: 'source', type: 'anchor', label: '来源', required: true },
      { name: 'source_label', type: 'text', label: '来源名称', required: false },
      { name: 'input_kind', type: 'enum', label: '输入', required: true, enum: [{ value: 'custom', label: '自定义输入', tone: 'indigo' }, { value: 'mock', label: '来自示例数据', tone: 'line' }] },
      { name: 'last_run_at', type: 'date', label: '上次推演', required: false },
      { name: 'result', type: 'enum', label: '状态', required: true, enum: Object.values(RESULT) },
      { name: 'expected', type: 'text', label: '预期', required: false },
      { name: 'actual', type: 'text', label: '实得', required: false },
      { name: 'fail_reason', type: 'text', label: '没通过的原因', required: false },
      { name: 'stale_source', type: 'text', label: '过期来源', required: false },
    ],
  },
  sections: ['输入', '预期结论', '推演记录'],
  refs: [{ field: 'operation', kind: 'operation' }],
  status: { field: 'result', map: { pass: '通过', fail: '失败', stale: '过期', not_run: '未推演' } },
  view: {
    list: { columns: ['title', 'slice', 'source', 'input_kind', 'last_run_at', 'result'], filters: ['result', 'slice', 'input_kind'] },
    detail: { layout: 'sections', side: ['result', 'slice', 'operation', 'source', 'input_kind', 'last_run_at'] },
    card: { title: 'title', chips: ['result'] },
  },
  studio: false,
};

export const MOCK_TYPE = {
  type: MOCK, title: '示例数据', pack: 'trace', pack_version: '0.9.1', format: 'csv',
  path: 'ext/trace/mock/{key}.csv', key: 'filename',
  schema: {
    version: '1',
    fields: [
      { name: 'object', type: 'ref', label: '示例数据', required: true },
      { name: 'scenarios', type: 'text', label: '用于场景', required: false },
      { name: 'rows', type: 'number', label: '行数', required: true },
      { name: 'mode', type: 'enum', label: '生成方式', required: true, enum: [{ value: 'by_scenario', label: '按场景生成' }, { value: 'manual', label: '手工维护' }] },
    ],
  },
  sections: [],
  refs: [{ field: 'object', kind: 'object' }],
  status: null,
  view: { list: { columns: ['object', 'scenarios', 'rows', 'mode'], filters: ['mode'] }, detail: { layout: 'table', side: [] }, card: { title: 'object', chips: [] } },
  studio: false,
};

// 「超量与审批提示」这一段的场景：并进写入范围锁（场景目录本来就在锁的 paths 里，这里给出具体的产物）
export const lockScope = {
  artifacts: D.scenarios().filter((x) => x.slice === 's5').map((x) => ({ type: SCENARIO, key: x.key, title: x.title })),
};

export const taskSeeds = {
  scenarios: () => ({ origin: { page: ORIGIN, label: '场景走查' }, title: '助手正在批量推演 23 个场景', percent: 39, step_label: '已推演 9 / 23' }),
};

const bump = (label) => {
  const m = /^v(\d+)\.(\d+)$/.exec(label || '');
  return m ? `v${m[1]}.${Number(m[2]) + 1}` : 'v0.1';
};
const scenarioPath = (key) => `ext/trace/scenarios/${key}.md`;

export function register(ctx) {
  const { on, json, err, db, sc, uid, F, cardOf, emitProj, emitConv, scopeOf, setScope, commandHooks = [], artifactTypeDefs = [] } = ctx;
  artifactTypeDefs.push(SCENARIO_TYPE, MOCK_TYPE);
  D.SC_RESOLUTIONS.forEach((r) => { if (F.ANCHOR_RESOLUTIONS && !F.ANCHOR_RESOLUTIONS[r.anchor]) F.ANCHOR_RESOLUTIONS[r.anchor] = r; });
  // ?mock=trace:batch：开场的写入范围锁换成批量推演那一把（节点 trace.trace）
  if (sc.trace === 'batch' && db.scope?.has(F.MAIN_PID)) {
    db.scope.set(F.MAIN_PID, { ...db.scope.get(F.MAIN_PID), node: { id: 'trace.trace', version: 'trace@0.10.0' }, reason_text: '助手正在批量推演 23 个场景' });
  }

  const stateOf = (pid) => {
    if (!db.trace) db.trace = new Map();
    if (!db.trace.has(pid)) {
      const main = pid === F.MAIN_PID;
      const now = new Date().toISOString();
      db.trace.set(pid, {
        scenarios: main ? D.scenarios().map((x, i) => ({ ...x, rev: 1, revision: `rs${i}a1`, updated_at: x.last_run_at || now, history: [] })) : [],
        mocks: main ? D.mockSets().map((x, i) => ({ ...x, revision: `rm${i}b2`, updated_at: now })) : [],
        trend: D.PASS_TREND.slice(),
      });
    }
    return db.trace.get(pid);
  };
  const ours = (t) => t === SCENARIO || t === MOCK;
  const lockedOf = (lock, key) => isLocked(lock, { type: SCENARIO, key }) || isLocked(lock, { path: scenarioPath(key) });

  function row(x, lock) {
    return {
      key: x.key, title: x.title,
      fields: {
        title: x.title, slice: x.slice, operation: x.operation, source: x.source, source_label: x.source_label, input_kind: x.input_kind,
        last_run_at: x.last_run_at, result: x.result, expected: x.expected || null, actual: x.actual || null, fail_reason: x.fail_reason || null,
        stale_source: x.stale_source || null,
      },
      status: RESULT[x.result] || null, stale: x.result === 'stale', locked: lockedOf(lock, x.key),
      updated_at: x.updated_at, revision: x.revision,
    };
  }
  function mockRow(x) {
    return {
      key: x.key, title: x.object.name,
      fields: { object: x.object, scenarios: x.scenarios, rows: x.rows, mode: x.mode },
      status: null, stale: false, locked: false, updated_at: x.updated_at, revision: x.revision,
    };
  }
  const sections = (x) => ({
    输入: x.input_text,
    预期结论: x.expect_text,
    推演记录: x.result === 'fail'
      ? `预期 ${x.expected}，实得 ${x.actual}：${x.fail_reason}。`
      : x.last_run_at ? `最近一次推演${RESULT[x.result]?.label === '通过' ? '通过' : `结果：${RESULT[x.result]?.label}`}。` : '还没推演过。',
  });

  // 保存一次 = 一个版本：项目版本号 +1，推 workspace.changed 和 artifacts.updated
  function commit(pid, type, keys, change, summary) {
    const card = cardOf(pid);
    const version = bump(card?.version_label);
    if (card) card.version_label = version;
    const sha = uid().padStart(8, '0').slice(-8);
    const st = stateOf(pid);
    st.scenarios.filter((x) => keys.includes(x.key)).forEach((x) => x.history.push({ commit: sha, version_label: version, at: new Date().toISOString(), source: 'web', summary }));
    emitProj(pid, 'workspace.changed', { commit: sha, version_label: version, source: 'web', changeset_id: null, by: 'zhangsan', entries: [], artifacts: keys.map((key) => ({ type, key, title: key })), paths: [] });
    if (card) emitProj(pid, 'project.updated', { fields: ['version_label'], meta: F.projectMeta(card) });
    emitProj(pid, 'artifacts.updated', { type, change, keys });
    return { commit: sha, version_label: version };
  }

  // ---------- AR-09 汇总（先于 AR-03 登记：/artifacts/{type}/summary 也匹配 /artifacts/{type}/{key}） ----------
  on('GET', '/projects/([^/]+)/artifacts/([^/]+)/summary', ({ m }) => {
    const t = decodeURIComponent(m[2]);
    if (!ours(t)) return undefined;
    const st = stateOf(m[1]);
    if (t === MOCK) {
      return json(200, { total: st.mocks.length, by: [{ value: 'by_scenario', label: '按场景生成', count: st.mocks.filter((x) => x.mode === 'by_scenario').length }], stale: 0, trend: null, delta_vs_previous_pct: null });
    }
    const list = st.scenarios;
    const by = Object.values(RESULT).map((r) => ({ value: r.value, label: r.label, count: list.filter((x) => x.result === r.value).length }));
    const pct = list.length ? Math.round((list.filter((x) => x.result === 'pass').length / list.length) * 100) : 0;
    const trend = [...st.trend.map(([version_label, value_pct]) => ({ version_label, value_pct })), { version_label: cardOf(m[1])?.version_label || 'v0.12', value_pct: pct }];
    const prev = st.trend.at(-1)?.[1];
    return json(200, { total: list.length, by, stale: list.filter((x) => x.result === 'stale').length, trend: list.length ? trend : null, delta_vs_previous_pct: prev == null || !list.length ? null : pct - prev });
  });

  // ---------- AR-02 列表 ----------
  on('GET', '/projects/([^/]+)/artifacts/([^/]+)', ({ m, query }) => {
    const t = decodeURIComponent(m[2]);
    if (!ours(t)) return undefined;
    const st = stateOf(m[1]);
    const limit = Number(query.get('limit') || 50);
    if (t === MOCK) {
      const items = st.mocks.map(mockRow);
      return json(200, { items: items.slice(0, limit), next_cursor: null, total: items.length, facets: { mode: [{ value: 'by_scenario', label: '按场景生成', count: items.length }] }, skipped: st.mocks.length ? D.SKIPPED : [] });
    }
    const lock = scopeOf(m[1]);
    const all = st.scenarios.map((x) => row(x, lock));
    let list = all;
    const result = query.get('filter[result]');
    const slice = query.get('filter[slice]');
    const input = query.get('filter[input_kind]');
    const stale = query.get('stale');
    const q = (query.get('q') || '').trim();
    if (result) list = list.filter((r) => r.fields.result === result);
    if (slice) list = list.filter((r) => r.fields.slice === slice);
    if (input) list = list.filter((r) => r.fields.input_kind === input);
    if (stale === 'true') list = list.filter((r) => r.stale);
    if (stale === 'false') list = list.filter((r) => !r.stale);
    if (q) list = list.filter((r) => r.title.includes(q));
    const facet = (field, values) => values.map(({ value, label }) => ({ value, label, count: all.filter((r) => r.fields[field] === value).length }));
    return json(200, {
      items: list.slice(0, limit), next_cursor: null, total: list.length,
      facets: {
        result: facet('result', Object.values(RESULT)),
        slice: facet('slice', SCENARIO_TYPE.schema.fields[1].enum),
        input_kind: facet('input_kind', SCENARIO_TYPE.schema.fields[5].enum),
      },
    });
  });

  // ---------- AR-04 历史 ----------
  on('GET', '/projects/([^/]+)/artifacts/([^/]+)/([^/]+)/history', ({ m }) => {
    const t = decodeURIComponent(m[2]);
    if (!ours(t)) return undefined;
    const x = stateOf(m[1]).scenarios.find((y) => y.key === decodeURIComponent(m[3]));
    if (!x) return err(404, 'artifact_not_found', '场景不存在');
    const born = { commit: `c${x.key.replace(/\D/g, '')}0a17`, version_label: 'v0.9', at: x.last_run_at || x.updated_at, source: 'assistant', summary: `新增场景「${x.title}」` };
    return json(200, { items: [...x.history].reverse().concat(born), next_cursor: null });
  });

  // ---------- AR-03 详情 ----------
  on('GET', '/projects/([^/]+)/artifacts/([^/]+)/([^/]+)', ({ m }) => {
    const t = decodeURIComponent(m[2]);
    if (!ours(t)) return undefined;
    const st = stateOf(m[1]);
    const key = decodeURIComponent(m[3]);
    if (t === MOCK) {
      const x = st.mocks.find((y) => y.key === key);
      if (!x) return err(404, 'artifact_not_found', '示例数据不存在');
      return json(200, {
        ...mockRow(x), type: MOCK, format: 'csv', format_version: '1', sections: {}, content: null, refs: [{ ...x.object, field: 'object' }],
        table: { columns: x.columns, rows: x.preview, total_rows: x.rows }, stale_reason: null, lock: null, path: `ext/trace/mock/${key}.csv`,
      });
    }
    const x = st.scenarios.find((y) => y.key === key);
    if (!x) return err(404, 'artifact_not_found', '场景不存在');
    const lock = scopeOf(m[1]);
    const r = row(x, lock);
    return json(200, {
      ...r, type: SCENARIO, format: 'md-frontmatter', format_version: '1', sections: sections(x), table: null, content: null,
      refs: [{ ...x.operation, field: 'operation' }], stale_reason: x.stale_reason || null, lock: r.locked ? lock : null, path: scenarioPath(key),
    });
  });

  // ---------- AR-05 保存（改标题、输入、预期结论） ----------
  on('PUT', '/projects/([^/]+)/artifacts/([^/]+)/([^/]+)', ({ m, body }) => {
    const pid = m[1];
    const t = decodeURIComponent(m[2]);
    if (t !== SCENARIO) return ours(t) ? err(403, 'artifact_readonly', '示例数据由场景生成，不在这里改', { reason: 'generated' }) : undefined;
    const x = stateOf(pid).scenarios.find((y) => y.key === decodeURIComponent(m[3]));
    if (!x) return err(404, 'artifact_not_found', '场景不存在');
    if (lockedOf(scopeOf(pid), x.key)) return err(423, 'write_scope_locked', '这个场景在助手的写入范围里', scopeOf(pid));
    if (body.base_revision && body.base_revision !== x.revision) {
      return err(409, 'revision_conflict', '这个场景已被修改', { target: `${SCENARIO}/${x.key}`, expected: body.base_revision, actual: x.revision, changed_by: 'assistant', changed_at: x.updated_at, version_label: cardOf(pid)?.version_label || null });
    }
    const title = String(body.fields?.title ?? x.title).trim();
    if (!title) return err(400, 'artifact_schema_invalid', '场景名称不能为空', { findings: [{ code: 'E-SC-TITLE', level: 'error', message: '场景名称不能为空', field: 'title' }] });
    const changedInput = body.sections?.['输入'] != null && body.sections['输入'] !== x.input_text;
    Object.assign(x, {
      title,
      input_text: body.sections?.['输入'] ?? x.input_text,
      expect_text: body.sections?.['预期结论'] ?? x.expect_text,
      rev: x.rev + 1, revision: `rs${uid()}`, updated_at: new Date().toISOString(),
    });
    // 改了输入，旧的推演结果就过期了
    if (changedInput && x.result !== 'stale') Object.assign(x, { result: 'stale', stale_source: '手动修改', stale_reason: '输入改过了，要重新推演' });
    const out = commit(pid, SCENARIO, [x.key], 'updated', body.summary || `修改场景「${title}」`);
    const lock = scopeOf(pid);
    return json(200, { artifact: { ...row(x, lock), type: SCENARIO, sections: sections(x), stale_reason: x.stale_reason || null, lock: null, path: scenarioPath(x.key) }, ...out });
  });

  // ---------- AR-07 删除 ----------
  on('DELETE', '/projects/([^/]+)/artifacts/([^/]+)/([^/]+)', ({ m, body }) => {
    const pid = m[1];
    const t = decodeURIComponent(m[2]);
    if (t !== SCENARIO) return ours(t) ? err(403, 'artifact_readonly', '示例数据由场景生成，不在这里删', { reason: 'generated' }) : undefined;
    const st = stateOf(pid);
    const x = st.scenarios.find((y) => y.key === decodeURIComponent(m[3]));
    if (!x) return err(404, 'artifact_not_found', '场景不存在');
    if (lockedOf(scopeOf(pid), x.key)) return err(423, 'write_scope_locked', '这个场景在助手的写入范围里', scopeOf(pid));
    if (body?.base_revision && body.base_revision !== x.revision) return err(409, 'revision_conflict', '这个场景已被修改', { target: `${SCENARIO}/${x.key}`, expected: body.base_revision, actual: x.revision });
    st.scenarios = st.scenarios.filter((y) => y.key !== x.key);
    const out = commit(pid, SCENARIO, [x.key], 'deleted', `删除场景「${x.title}」`);
    return json(200, out);
  });

  // ---------- 页面命令（S-20）：跑完结果回到列表 ----------
  const fromPage = (b, command) => b.command === command && b.origin?.page === ORIGIN;
  const ran = (pid, keys, apply) => {
    const st = stateOf(pid);
    const now = new Date().toISOString();
    st.scenarios.filter((x) => keys.includes(x.key)).forEach((x) => { apply(x); x.last_run_at = now; x.updated_at = now; });
  };
  const keysOf = (pid, pick) => stateOf(pid).scenarios.filter(pick).map((x) => x.key);

  // 批量验证：开跑时推演范围进写入范围锁（助手运行中），跑完这一轮就合入成版本、解锁（F3），对话里出「这一轮」卡，
  // 撤回这一轮回到跑之前的结果（CS-05 见下）。开跑时项目已经有别的锁就不动锁，结果直接回到列表。
  const batchLock = (pid, state, csId, cid) => ({
    state, changeset_id: csId, conversation_id: cid, run_id: null, slice: null, node: { id: 'trace.trace', version: 'trace@0.10.0' },
    entries: [], artifacts: stateOf(pid).scenarios.map((x) => ({ type: SCENARIO, key: x.key, title: x.title })), paths: [],
    reason_text: `助手正在批量推演 ${stateOf(pid).scenarios.length} 个场景`, since: new Date().toISOString(),
  });
  const roundCard = (csId, sum, state, version) => ({
    card_id: `card-trace-${csId}`, card_type: 'round_changes', eyebrow: '这一轮', title: `批量推演 ${sum.total} 个场景`, subtitle: '',
    status: state === 'reverted' ? { key: 'skipped', label: '已撤回' } : { key: 'confirmed', label: version },
    plugin: { name: 'trace', label: '场景走查' }, open: null, changeset_id: csId, option_key: null, checks: { errors: 0, warnings: 0, pending: 0 }, hints: [],
    groups: [{ kind: 'scenario', label: '场景', items: [{ change: 'test', target: null, text: `推演 ${sum.total} 个：通过 ${sum.pass} · 失败 ${sum.fail} · 过期 ${sum.stale}`, comparable: false }] }],
    state, merged_version_label: version,
  });
  const summaryOf = (pid) => {
    const list = stateOf(pid).scenarios;
    const n = (r) => list.filter((x) => x.result === r).length;
    return { total: list.length, pass: n('pass'), fail: n('fail'), stale: n('stale') };
  };
  commandHooks.push({
    match: (b) => fromPage(b, '/ontology-trace-batch') && !b.args?.keys?.length,
    title: (b) => `助手正在${b.text || '批量推演全部场景'}`,
    steps: ['已推演 3 / 23', '已推演 9 / 23', '已推演 16 / 23', '已推演 23 / 23'],
    start: (pid, b, task) => {
      const st = stateOf(pid);
      st.batch = null;
      if (!setScope || scopeOf(pid).state !== 'none') return;
      const csId = `cs-trace-${uid()}`;
      st.batch = { csId, cid: task.conversation_id, before: st.scenarios.map((x) => ({ ...x })) };
      setScope(pid, batchLock(pid, 'assistant_running', csId, task.conversation_id));
    },
    doneText: (b) => `「${b.text || '批量推演'}」跑完了，结果已写进场景走查。`,
    finish: (pid) => {
      // 过期的场景 3、6 补上了新口径的输入，推演通过；场景 4 还没有示例数据，仍过期；失败的那个照旧
      const keys = keysOf(pid, (x) => x.key !== 's-04');
      ran(pid, keys, (x) => { if (x.result === 'stale') Object.assign(x, { result: 'pass', stale_source: null, stale_reason: null }); });
      const b = stateOf(pid).batch;
      if (b) {
        // 一轮结束就合入成版本（F3）：趋势线把跑之前那一版的通过率记进历史，「比上一版」跟它比
        const st = stateOf(pid);
        const sum = summaryOf(pid);
        db.changesets?.set(b.csId, { owner: 'trace', state: 'merged', pid, cid: b.cid });
        const before = b.before || [];
        if (before.length) st.trend = [...st.trend, [cardOf(pid)?.version_label || 'v0.12', Math.round((before.filter((x) => x.result === 'pass').length / before.length) * 100)]].slice(-6);
        const out = commit(pid, SCENARIO, st.scenarios.map((x) => x.key), 'updated', `批量推演 ${sum.total} 个场景`);
        b.version = out.version_label;
        setScope(pid, noLock(pid));
        emitConv?.(pid, b.cid, 'card', roundCard(b.csId, sum, 'merged', out.version_label));
        emitProj(pid, 'changeset.updated', { changeset_id: b.csId, state: 'merged', source: 'assistant', conversation_id: b.cid, version_label: out.version_label });
      }
      emitProj(pid, 'artifacts.updated', { type: SCENARIO, change: 'updated', keys });
    },
  });

  // ---------- 批量推演那一轮的变更集（owner: 'trace'；index.js 的同路径处理者让过来） ----------
  const mine = (id) => {
    const cs = db.changesets?.get(id);
    return cs?.owner === 'trace' ? cs : null;
  };
  const noLock = (pid) => ({ ...scopeOf(pid), state: 'none', changeset_id: null, entries: [], artifacts: [], paths: [], node: null, reason_text: '', since: null });
  on('GET', '/projects/([^/]+)/changesets/([^/]+)/diff', ({ m }) => {
    if (!mine(m[2])) return undefined;
    const before = new Map((stateOf(m[1]).batch?.before || []).map((x) => [x.key, x]));
    const items = stateOf(m[1]).scenarios.filter((x) => before.get(x.key)?.result !== x.result).map((x) => ({
      target: { type: SCENARIO, key: x.key, title: x.title }, change: 'mod',
      sections: [{ title: '', lines: [{ kind: 'header', text: x.title }, { kind: 'del', text: `- 状态：${RESULT[before.get(x.key).result].label}` }, { kind: 'add', text: `+ 状态：${RESULT[x.result].label}` }] }],
    }));
    return json(200, { items });
  }); // CS-03
  on('GET', '/projects/([^/]+)/changesets/([^/]+)', ({ m }) => {
    const cs = mine(m[2]);
    if (!cs) return undefined;
    const now = new Date().toISOString();
    return json(200, {
      id: m[2], source: 'assistant', build_mode: 'L2', purpose: '批量推演', state: cs.state, base: cardOf(m[1])?.head || 'a71e03c9',
      conversation_id: cs.cid, run_id: null, slice_id: null, created_at: now, updated_at: now, merged: null, entity_count: stateOf(m[1]).scenarios.length,
      groups: [], checks: { errors: 0, warnings: 0, findings: [] }, next_version_label: bump(cardOf(m[1])?.version_label),
    });
  }); // CS-02
  on('POST', '/projects/([^/]+)/changesets/([^/]+)/(merge|discard)', ({ m }) => {
    const pid = m[1];
    const cs = mine(m[2]);
    if (!cs) return undefined;
    // CS-04：一轮结束已经合入，界面不再调；CS-05 撤回这一轮：回到跑之前的结果，另存一个版本
    if (m[3] === 'merge' || cs.state !== 'merged') return err(409, 'changeset_closed', '这一轮的修改已经生效或撤回', { state: cs.state });
    const st = stateOf(pid);
    const keys = st.scenarios.map((x) => x.key);
    cs.state = 'reverted';
    if (st.batch?.before) st.scenarios = st.batch.before;
    const sum = summaryOf(pid);
    const version = st.batch?.version || null;
    st.batch = null;
    const out = commit(pid, SCENARIO, keys, 'updated', '撤回批量推演');
    emitConv?.(pid, cs.cid, 'card.updated', roundCard(m[2], sum, 'reverted', version));
    emitConv?.(pid, cs.cid, 'notice', { kind: 'discarded', text: `已撤回这一轮 · 版本 ${out.version_label}`, version_label: out.version_label, changeset_id: m[2], align: 'center' });
    emitProj(pid, 'changeset.updated', { changeset_id: m[2], state: 'merged', source: 'assistant', conversation_id: cs.cid, version_label: version });
    emitProj(pid, 'artifacts.updated', { type: SCENARIO, change: 'updated', keys });
    return json(200, { state: 'reverted', commit: out.commit, version_label: out.version_label });
  }); // CS-04 / CS-05
  commandHooks.push({
    // 补齐过期：后端同一条命令 /ontology-trace-batch，只带 keys（contract-changes B4c 映射表）
    match: (b) => fromPage(b, '/ontology-trace-batch') && !!b.args?.keys?.length,
    title: (b) => `助手正在${b.text || '补齐过期场景'}`,
    steps: ['已补齐 1 / 3', '已补齐 2 / 3', '已补齐 3 / 3'],
    finish: (pid, b) => {
      const keys = b.args?.keys?.length ? b.args.keys : keysOf(pid, (x) => x.result === 'stale');
      ran(pid, keys, (x) => Object.assign(x, { result: 'pass', stale_source: null, stale_reason: null }));
      emitProj(pid, 'artifacts.updated', { type: SCENARIO, change: 'fresh', keys });
    },
  });
  commandHooks.push({
    match: (b) => fromPage(b, '/ontology-trace-interactive'),
    title: (b) => `助手正在${b.text || '交互走查'}`,
    steps: ['走查到第 1 步', '走查到第 2 步', '走查到第 3 步', '走查完成'],
    finish: (pid, b) => {
      const key = b.args?.scenario;
      if (!key) return;
      ran(pid, [key], (x) => { if (x.result === 'stale') Object.assign(x, { result: 'pass', stale_source: null, stale_reason: null }); });
      emitProj(pid, 'artifacts.updated', { type: SCENARIO, change: 'updated', keys: [key] });
    },
  });
  commandHooks.push({
    match: (b) => fromPage(b, '/ontology-mock'),
    title: (b) => `助手正在${b.text || '按场景补生成示例数据'}`,
    steps: ['已生成 2 / 6 组', '已生成 4 / 6 组', '已生成 6 / 6 组'],
    finish: (pid) => {
      const st = stateOf(pid);
      const now = new Date().toISOString();
      st.mocks.forEach((x) => { x.updated_at = now; });
      emitProj(pid, 'artifacts.updated', { type: MOCK, change: 'updated', keys: st.mocks.map((x) => x.key) });
    },
  });
}
