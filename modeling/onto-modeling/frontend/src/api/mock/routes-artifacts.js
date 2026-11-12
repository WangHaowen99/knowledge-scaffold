// 3.17 插件产物（AR-01～AR-09），F2 只有图示包的流程图 diagram.flowchart；另导出阶段 flowchart（设计稿 flowchart 页背后那段对话）。
// 演示参数：?mock=fc:conflict 第一次保存返回 409（模拟助手刚改过这张图）；?mock=fc:compressed 「超量处理流程」是压缩存储（自动回退 drawio 高级编辑）。
import { checkFlowchart, normalizeModel, summarize } from '@packs/diagram/ui/flowchart/flowchartModel.js';
import { isLocked } from '../../state/readonly.js';
import * as F from './fixtures.js';
import { FLOWCHARTS, entryRef } from './scenario.js';

const TYPE = 'diagram.flowchart';
const STATUS = { draft: { value: 'draft', label: '草稿', tone: 'gray' }, confirmed: { value: 'confirmed', label: '已确认', tone: 'blue' } };

export const FLOWCHART_TYPE = {
  type: TYPE,
  title: '流程图',
  pack: 'diagram',
  pack_version: '0.5.0',
  format: 'drawio',
  path: 'ext/diagram/flowcharts/{key}.drawio',
  key: 'filename',
  schema: {
    version: '1',
    fields: [
      { name: 'title', type: 'text', label: '流程名称', required: true },
      { name: 'operation', type: 'ref', label: '关联操作', required: false },
      { name: 'state', type: 'enum', label: '状态', required: true, enum: [{ value: 'draft', label: '草稿', tone: 'gray' }, { value: 'confirmed', label: '已确认', tone: 'blue' }] },
      { name: 'summary', type: 'text', label: '规模', required: false },
    ],
  },
  sections: [],
  refs: [{ field: 'operation', kind: 'operation' }],
  status: { field: 'state', map: { draft: '草稿', confirmed: '已确认' } },
  view: {
    list: { columns: ['title', 'operation', 'summary', 'state'], filters: ['state'] },
    // 流程图编辑器是图示包自带的（ADR-0055）：官方包随平台编译；对话里的卡片选平台的流程图卡组件
    detail: {
      layout: 'custom', side: [],
      editor: { id: 'flowchart', title: '流程图编辑器', pack: 'diagram', version: '0.5.0', mode: 'bundled', ui_api: '', entry: 'ui/flowchart/index.jsx' },
    },
    card: { component: 'flowchart', title: 'title', chips: ['state'] },
  },
  studio: true,
};

const node = (id, type, label, x, y, extra = {}) => ({ id, type, label, description: '', x, y, refs: [], pending: false, pending_note: null, ...extra });
const edge = (id, type, from, to, label = '', condition = '') => ({ id, type, from, to, label, condition });

// 超量处理流程：设计稿 FC_DEV 的版式（8 个节点、1 个判断，另有 2 个数据对象）
function overageFlow() {
  return {
    storage: 'standard',
    process: {
      name: '超量处理流程',
      description: '对照门店申请与适用标准逐项比较，严于标准按门店执行，超出标准生成审批提示并挂到补货台账，最后输出超量清单。',
      roles: ['补货专员', '区域督导'],
      inputs: ['补货单（草稿）', '门店特殊需求'],
      exceptions: ['标准缺失：转区域审批'],
      handoffs: ['审批提示交补货台账跟踪'],
    },
    nodes: [
      node('n1', 'start', '受理申请', 150, 60, { description: '店长提交补货申请，带门店、品类和申请量。' }),
      node('n2', 'step', '读取申请量', 150, 170, { description: '从补货单草稿里读出每个商品的申请量。', refs: [entryRef('replenish_order')] }),
      node('n3', 'data', '补货单（草稿）', 400, 170, { refs: [entryRef('replenish_order')] }),
      node('n4', 'step', '取标准上限', 150, 300, {
        description: '按商品品类的适用标准取各检查项目的标准上限，作为和申请量比较的基准。',
        refs: [entryRef('overage_and_hint'), entryRef('display_qty_std'), entryRef('safety_stock_std')],
        pending: true,
        pending_note: '适用标准、区域统一配额还是店长提供',
      }),
      node('n5', 'data', '陈列量标准', 400, 300, { refs: [entryRef('display_qty_std')] }),
      node('n6', 'decision', '严于标准？', 150, 450, { description: '门店申请量和标准上限逐项比较。' }),
      node('n7', 'step', '按门店执行', 400, 450, { description: '申请量不超过标准上限，按门店申请下单。', refs: [entryRef('r09_store_stricter')] }),
      node('n8', 'step', '生成审批提示', 150, 590, { description: '超出标准的项生成审批提示，交区域督导审批。', refs: [entryRef('approval_hint'), entryRef('r11_temp_increase')] }),
      node('n9', 'step', '挂到补货台账', 400, 590, { description: '审批提示挂到补货台账，审批通过前不得下单。', refs: [entryRef('replenish_ledger')] }),
      node('n10', 'end', '输出超量清单', 650, 520, { refs: [entryRef('overage_list')] }),
    ],
    edges: [
      edge('e1', 'seq', 'n1', 'n2'),
      edge('e2', 'data', 'n3', 'n2', '读取'),
      edge('e3', 'seq', 'n2', 'n4'),
      edge('e4', 'data', 'n5', 'n4', '读取'),
      edge('e5', 'seq', 'n4', 'n6'),
      edge('e6', 'cond', 'n6', 'n7', '是', '门店申请量严于标准上限（申请量不超过品类标准上限）'),
      edge('e7', 'cond', 'n6', 'n8', '否', '门店申请量超出标准上限，需要区域审批'),
      edge('e8', 'seq', 'n8', 'n9'),
      edge('e9', 'seq', 'n7', 'n10'),
      edge('e10', 'seq', 'n9', 'n10'),
    ],
    drawio_xml: null,
    operation: entryRef('overage_and_hint'),
  };
}

// 门店补货主流程：设计稿 fcCanvas 默认图的版式（10 个节点、2 个判断，另有 1 个数据对象）
function replenishMainFlow() {
  return {
    storage: 'standard',
    process: {
      name: '门店补货主流程',
      description: '受理门店补货申请，按品类测算安全库存、核定陈列量，超量的生成审批提示，最后下发补货单。',
      roles: ['店长', '补货专员', '区域督导'],
      inputs: ['补货申请', '05-门店清单', '06-商品品类表'],
      exceptions: ['不在受理范围：退回门店补充'],
      handoffs: ['补货单交门店收货'],
    },
    nodes: [
      node('m1', 'start', '受理申请', 150, 60),
      node('m2', 'step', '录入门店与品类', 150, 165, { refs: [entryRef('store'), entryRef('goods_category')] }),
      node('m3', 'decision', '在受理范围？', 150, 300, { refs: [entryRef('r01_intake_scope')] }),
      node('m4', 'step', '测算安全库存', 400, 300, { refs: [entryRef('safety_stock_calc')] }),
      node('m5', 'step', '核定陈列量', 630, 300, { refs: [entryRef('display_qty_check')] }),
      node('m6', 'decision', '有超量？', 630, 450),
      node('m7', 'step', '生成审批提示', 400, 590, { refs: [entryRef('overage_and_hint')] }),
      node('m8', 'end', '下发补货单', 680, 590, { refs: [entryRef('render_order')] }),
      node('m9', 'step', '退回门店补充', 150, 450),
      node('m10', 'end', '结束受理', 150, 590),
      node('m11', 'data', '商品品类表', 400, 165, { refs: [entryRef('goods_category')] }),
    ],
    edges: [
      edge('f1', 'seq', 'm1', 'm2'),
      edge('f2', 'seq', 'm2', 'm3'),
      edge('f3', 'cond', 'm3', 'm4', '是'),
      edge('f4', 'exc', 'm3', 'm9', '超出范围'),
      edge('f5', 'data', 'm11', 'm4', '读取'),
      edge('f6', 'seq', 'm4', 'm5'),
      edge('f7', 'seq', 'm5', 'm6'),
      edge('f8', 'cond', 'm6', 'm7', '有超量'),
      edge('f9', 'cond', 'm6', 'm8', '无超量'),
      edge('f10', 'seq', 'm7', 'm8'),
      edge('f11', 'seq', 'm9', 'm10'),
    ],
    drawio_xml: null,
    operation: entryRef('replenish_main'),
  };
}

const MODELS = { 'overage-flow': overageFlow, 'replenish-main-flow': replenishMainFlow };
const SOURCES = { 'overage-flow': '补货规则第 9 章 · 助手画出', 'replenish-main-flow': '01 场景说明 · 02 补货规则' };

// 压缩存储的 drawio：<diagram> 里是 deflate + base64，不是明文 mxGraphModel；mock 只要形状对，内容是占位
const compressedXml = (name) => `<mxfile host="onto-modeling"><diagram name="${name}">${btoa('onto-modeling mock compressed diagram')}</diagram></mxfile>`;

function flowState(db, pid, sc) {
  if (!db.flowcharts) db.flowcharts = new Map();
  if (!db.flowcharts.has(pid)) {
    const map = new Map();
    if (pid === F.MAIN_PID) {
      FLOWCHARTS.forEach((fc, i) => {
        let content = MODELS[fc.key]();
        if (fc.key === 'overage-flow' && sc.fc === 'compressed') content = { ...content, storage: 'drawio_compressed', drawio_xml: compressedXml(fc.title) };
        map.set(fc.key, {
          key: fc.key,
          state: fc.state,
          content,
          revision: `r${fc.key.length}${i}a`,
          rev: 1,
          updated_at: new Date(Date.now() - (i ? 2 * 24 * 3600 * 1000 : 3 * 60 * 1000)).toISOString(),
          updated_by: i ? 'web' : 'assistant',
          source_label: SOURCES[fc.key],
          history: [{ commit: i ? 'b81f30a2' : 'c3e9d710', version_label: i ? 'v0.9' : 'v0.12', at: new Date(Date.now() - (i ? 2 * 24 * 3600 * 1000 : 3 * 60 * 1000)).toISOString(), source: i ? 'studio' : 'assistant', summary: i ? '你在 Studio 里调整了布局' : '助手按补货规则第 9 章画出' }],
        });
      });
    }
    db.flowcharts.set(pid, map);
  }
  return db.flowcharts.get(pid);
}

function row(rec, lock) {
  const m = rec.content;
  return {
    key: rec.key,
    title: m.process.name || rec.key,
    fields: {
      operation: m.operation?.name || null,
      summary: summarize(m).text,
      state: rec.state,
      source_label: rec.source_label || null,
      updated_by: rec.updated_by,
    },
    status: STATUS[rec.state] || null,
    stale: false,
    locked: isLocked(lock, { type: TYPE, key: rec.key }),
    updated_at: rec.updated_at,
    revision: rec.revision,
  };
}

function detail(rec, lock, pid) {
  const m = rec.content;
  const refs = [];
  const seen = new Set();
  if (m.operation) { refs.push({ ...m.operation, field: 'operation' }); seen.add(m.operation.id); }
  m.nodes.forEach((n) => (n.refs || []).forEach((r) => { if (!seen.has(r.id)) { seen.add(r.id); refs.push({ ...r, field: `nodes/${n.id}` }); } }));
  const locked = isLocked(lock, { type: TYPE, key: rec.key });
  return {
    ...row(rec, lock),
    type: TYPE,
    format: 'drawio',
    format_version: '1',
    sections: {},
    table: null,
    content: structuredClone(m),
    refs,
    stale_reason: null,
    lock: locked ? lock : null,
    path: `ext/diagram/flowcharts/${rec.key}.drawio`,
    project_id: pid,
  };
}

const bump = (label) => {
  const m = /^v(\d+)\.(\d+)$/.exec(label || '');
  return m ? `v${m[1]}.${Number(m[2]) + 1}` : 'v0.1';
};

export function register(ctx) {
  const { on, json, err, db, sc, cardOf, scopeOf, emitProj, uid, hub, convKey, convsOf, emitConv, artifactTypeDefs = [] } = ctx;
  // 别的分组登记过的产物类型（artifactTypeDefs）不归这里管：返回 undefined，交给后面登记的路由（index.js 按顺序试）；
  // 谁都没登记的类型照常 404
  const unknownType = (t) => {
    if (t === TYPE) return null;
    return artifactTypeDefs.some((d) => d.type === t) ? undefined : err(404, 'artifact_type_unknown', '没有这种产物类型', { type: t });
  };
  const enabled = () => true;

  on('GET', '/projects/([^/]+)/artifact-types', () => json(200, { items: [...(enabled() ? [FLOWCHART_TYPE] : []), ...artifactTypeDefs] }));

  on('GET', '/projects/([^/]+)/artifacts/([^/]+)/summary', ({ m }) => {
    const t = decodeURIComponent(m[2]);
    const bad = unknownType(t);
    if (bad !== null) return bad;
    const list = [...flowState(db, m[1], sc).values()];
    const by = Object.values(STATUS).map((s) => ({ value: s.value, label: s.label, count: list.filter((r) => r.state === s.value).length }));
    return json(200, { total: list.length, by, stale: 0, trend: null, delta_vs_previous_pct: null });
  });

  on('GET', '/projects/([^/]+)/artifacts/([^/]+)', ({ m, query }) => {
    const t = decodeURIComponent(m[2]);
    const bad = unknownType(t);
    if (bad !== null) return bad;
    const lock = scopeOf(m[1]);
    let list = [...flowState(db, m[1], sc).values()].map((r) => row(r, lock));
    const st = query.get('filter[state]');
    if (st) list = list.filter((r) => r.fields.state === st);
    const q = (query.get('q') || '').trim();
    if (q) list = list.filter((r) => `${r.title}${r.fields.operation || ''}`.includes(q));
    const all = [...flowState(db, m[1], sc).values()];
    const facets = { state: Object.values(STATUS).map((s) => ({ value: s.value, label: s.label, count: all.filter((r) => r.state === s.value).length })) };
    const limit = Number(query.get('limit') || 50);
    return json(200, { items: list.slice(0, limit), next_cursor: null, total: list.length, facets });
  });

  on('GET', '/projects/([^/]+)/artifacts/([^/]+)/([^/]+)/history', ({ m }) => {
    const bad = unknownType(decodeURIComponent(m[2]));
    if (bad !== null) return bad;
    const rec = flowState(db, m[1], sc).get(decodeURIComponent(m[3]));
    if (!rec) return err(404, 'artifact_not_found', '流程图不存在');
    return json(200, { items: [...rec.history].reverse(), next_cursor: null });
  });

  on('GET', '/projects/([^/]+)/artifacts/([^/]+)/([^/]+)', ({ m }) => {
    const bad = unknownType(decodeURIComponent(m[2]));
    if (bad !== null) return bad;
    const rec = flowState(db, m[1], sc).get(decodeURIComponent(m[3]));
    if (!rec) return err(404, 'artifact_not_found', '流程图不存在');
    return json(200, detail(rec, scopeOf(m[1]), m[1]));
  });

  // 保存一次 = 一个版本：项目版本号 +1，推 workspace.changed（带这张图），对话里同一张图的卡片跟着更新规模
  function commitChange(pid, rec, summary) {
    const card = cardOf(pid);
    const version = bump(card?.version_label);
    if (card) card.version_label = version;
    const commit = uid().padStart(8, '0').slice(-8);
    rec.rev += 1;
    rec.revision = `r${rec.rev}${uid()}`;
    rec.updated_at = new Date().toISOString();
    rec.updated_by = 'web';
    rec.history.push({ commit, version_label: version, at: rec.updated_at, source: 'web', summary });
    const ref = { type: TYPE, key: rec.key, title: rec.content.process.name };
    emitProj(pid, 'workspace.changed', { commit, version_label: version, source: 'web', changeset_id: null, by: 'zhangsan', entries: [], artifacts: [ref], paths: [`ext/diagram/flowcharts/${rec.key}.drawio`] });
    if (card) emitProj(pid, 'project.updated', { fields: ['version_label'], meta: F.projectMeta(card) });
    // 对话里引用这张图的流程图卡：规模跟着变
    const sum = summarize(rec.content).text;
    convsOf(pid).forEach((c) => {
      hub.history(convKey(pid, c.id)).forEach((ev) => {
        const d = ev.data;
        if (ev.type !== 'card' || d?.card_type !== 'flowchart' || d.artifact?.key !== rec.key || d.summary === sum) return;
        emitConv(pid, c.id, 'card.updated', { ...d, summary: sum, subtitle: (d.subtitle || '').replace(/\d+ 个节点、\d+ 个判断/, sum) });
      });
    });
    return { commit, version };
  }

  on('PUT', '/projects/([^/]+)/artifacts/([^/]+)/([^/]+)', async ({ m, body }) => {
    const pid = m[1];
    const bad = unknownType(decodeURIComponent(m[2]));
    if (bad !== null) return bad;
    const rec = flowState(db, pid, sc).get(decodeURIComponent(m[3]));
    if (!rec) return err(404, 'artifact_not_found', '流程图不存在');
    const lock = scopeOf(pid);
    if (isLocked(lock, { type: TYPE, key: rec.key })) return err(423, 'write_scope_locked', '这张图在助手的写入范围里', { lock });
    // 演示冲突：第一次保存前，助手刚改过这张图
    if (sc.fc === 'conflict' && !rec.conflicted) {
      rec.conflicted = true;
      const expected = rec.revision;
      const pend = rec.content.nodes.find((n) => n.pending);
      if (pend) pend.description += '（助手补充：先按适用标准取值）';
      commitChange(pid, rec, '助手补充了「取标准上限」的说明');
      rec.updated_by = 'assistant';
      return err(409, 'revision_conflict', '这张图已被修改', { expected, actual: rec.revision, changed_by: 'assistant', changed_at: rec.updated_at, version_label: cardOf(pid)?.version_label || null });
    }
    if (body.base_revision && body.base_revision !== rec.revision) {
      return err(409, 'revision_conflict', '这张图已被修改', { expected: body.base_revision, actual: rec.revision, changed_by: rec.updated_by, changed_at: rec.updated_at, version_label: cardOf(pid)?.version_label || null });
    }
    const content = body.content || {};
    let next = normalizeModel({ ...content, storage: rec.content.storage, drawio_xml: rec.content.storage === 'drawio_compressed' ? content.drawio_xml || rec.content.drawio_xml : null });
    // drawio 高级编辑交来的是 XML：服务端（图示包）解析成模型；mock 在浏览器里用 DOMParser 解析明文 XML
    if (content.drawio_xml && rec.content.storage !== 'drawio_compressed') {
      try {
        const { drawioToModel } = await import('@packs/diagram/ui/flowchart/drawioConvert.js');
        next = normalizeModel(drawioToModel(content.drawio_xml, next));
      } catch { /* 解析不了就按模型保存 */ }
    }
    rec.content = next;
    const { commit, version } = commitChange(pid, rec, body.summary || '在流程图弹窗里修改');
    return json(200, { artifact: detail(rec, scopeOf(pid), pid), commit, version_label: version });
  });

  on('POST', '/projects/([^/]+)/artifacts/([^/]+)', ({ m, body }) => {
    const pid = m[1];
    const bad = unknownType(decodeURIComponent(m[2]));
    if (bad !== null) return bad;
    const map = flowState(db, pid, sc);
    const title = body.fields?.title || body.content?.process?.name || '未命名流程';
    const key = body.key || `flow-${uid()}`;
    if (map.has(key)) return err(409, 'artifact_exists', '同名流程图已存在', { key });
    const content = normalizeModel(body.content || {
      process: { name: title },
      nodes: [node('n1', 'start', '开始', 150, 60), node('n2', 'end', '结束', 150, 200)],
      edges: [edge('e1', 'seq', 'n1', 'n2')],
    });
    content.process.name = content.process.name || title;
    const rec = { key, state: 'draft', content, revision: `r1${uid()}`, rev: 1, updated_at: new Date().toISOString(), updated_by: 'web', source_label: '你在 Studio 新建', history: [] };
    map.set(key, rec);
    const { commit, version } = commitChange(pid, rec, `新建流程图「${title}」`);
    return json(201, { artifact: detail(rec, scopeOf(pid), pid), commit, version_label: version });
  });

  on('DELETE', '/projects/([^/]+)/artifacts/([^/]+)/([^/]+)', ({ m, body }) => {
    const pid = m[1];
    const bad = unknownType(decodeURIComponent(m[2]));
    if (bad !== null) return bad;
    const map = flowState(db, pid, sc);
    const rec = map.get(decodeURIComponent(m[3]));
    if (!rec) return err(404, 'artifact_not_found', '流程图不存在');
    if (isLocked(scopeOf(pid), { type: TYPE, key: rec.key })) return err(423, 'write_scope_locked', '这张图在助手的写入范围里', { lock: scopeOf(pid) });
    if (body.base_revision && body.base_revision !== rec.revision) return err(409, 'revision_conflict', '这张图已被修改', { expected: body.base_revision, actual: rec.revision });
    map.delete(rec.key);
    const { commit, version } = commitChange(pid, rec, `删除流程图「${rec.content.process.name}」`);
    return json(200, { commit, version_label: version });
  });

  on('POST', '/projects/([^/]+)/artifacts/([^/]+)/([^/]+)/validate', ({ m, body }) => {
    const bad = unknownType(decodeURIComponent(m[2]));
    if (bad !== null) return bad;
    const rec = flowState(db, m[1], sc).get(decodeURIComponent(m[3]));
    if (!rec) return err(404, 'artifact_not_found', '流程图不存在');
    return json(200, checkFlowchart(body.content || rec.content));
  });

  // AR-10：mock 不存文件原文，按模型生成（服务端是把模型合进原文）
  on('POST', '/projects/([^/]+)/artifacts/([^/]+)/([^/]+)/drawio', async ({ m, body }) => {
    const bad = unknownType(decodeURIComponent(m[2]));
    if (bad !== null) return bad;
    const rec = flowState(db, m[1], sc).get(decodeURIComponent(m[3]));
    if (!rec) return err(404, 'artifact_not_found', '流程图不存在');
    const { modelToDrawio } = await import('@packs/diagram/ui/flowchart/drawioConvert.js');
    return json(200, { xml: modelToDrawio(normalizeModel(body.content || rec.content), rec.content.process?.name), revision: rec.revision });
  });
}

// ---------- 阶段 flowchart：设计稿 flowchart 页背后的对话 ----------
function mmdd(d) {
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function flowchartStageCard() {
  return {
    card_id: 'card-fc-overage',
    card_type: 'flowchart',
    eyebrow: null,
    title: '流程图 · 超量处理流程',
    subtitle: '按补货规则第 9 章画出 · 8 个节点、1 个判断',
    status: { key: 'draft', label: '草稿' },
    plugin: { name: 'diagram', label: '图示' },
    open: { kind: 'dialog', target: 'flowchart', ref: { type: TYPE, key: 'overage-flow', title: '超量处理流程' } },
    artifact: { type: TYPE, key: 'overage-flow', title: '超量处理流程' },
    state: 'draft',
    summary: '8 个节点、1 个判断',
    locked: false,
    thumbnail: F.flowchartCard().thumbnail,
  };
}

function flowchartHistory() {
  const t = new Date(Date.now() - 24 * 3600 * 1000);
  t.setHours(15, 18, 0, 0);
  const ts = t.toISOString();
  const t2 = new Date(t.getTime() + 2 * 60 * 1000).toISOString();
  const cid = F.conversations()[0].id;
  let seq = 1700;
  const e = (type, data, at = t2) => ({ id: String(++seq), type, ts: at, project_id: F.MAIN_PID, conversation_id: cid, data });
  const sug = (id, label, icon, text) => ({ id, label, icon, primary: false, disabled: false, reason: '', priority: 3, action: { type: 'send', text } });
  return [
    e('notice', { kind: 'info', text: `${mmdd(t)} 15:18 · 会话「超量与审批提示」`, version_label: null, changeset_id: null, align: 'center' }, ts),
    e('message.user', { message_id: 'm-u-fc1', text: '把超量处理的过程画成流程图，我要先看一下判断走向对不对', attachments: [], references: [], origin: null, delivery: 'immediate' }, ts),
    e('message.assistant', { message_id: 'm-a-fc2', text_md: '画好了，按补货规则第 9 章整理成 8 个节点、1 个判断。「取标准上限」这一步的口径还没定，先用虚线框标出来。', anchors: [], entry_links: [], continued: false }),
    e('card', flowchartStageCard()),
    e('suggestions.updated', {
      mode: 'normal',
      primary: null,
      secondary: [sug('sg-fc-ok', '流程没问题', 'check', '流程没问题'), sug('sg-fc-done', '我改完了，你看看', 'edit', '我改完了，你看看这张流程图')],
      all_commands: true,
    }),
  ];
}

export const stages = {
  flowchart: { conversations: F.conversations, history: flowchartHistory },
};
