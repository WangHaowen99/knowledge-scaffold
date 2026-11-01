// mock · 3.8 知识（K-01～K-08）：知识沉淀页、对话里「沉淀为业务知识」（K-03、K-06 GET 两处共用，改时保持兼容）。
// 演示参数：?mock=knowledge:empty 本项目还没有沉淀的条目（行业包条目照常加载）；
// ?mock=task:knowledge 一打开就有一个「在对话里复核」的进行中任务；
// ?mock=scope:running|pending 时，「超量与审批提示」这一片范围内的 2 条知识（lockScope）只读：编辑、删除返回 423。
// 写入范围锁里的知识条目用 ArtifactRef 表示：{ type: 'core.knowledge', key: <知识条目 id> }。
import { isLocked } from '../../state/readonly.js';
import * as K from './knowledge-data.js';

export const KNOWLEDGE_TYPE = 'core.knowledge';
const TYPES = ['correction', 'convention', 'term', 'domain_rule'];

export const lockScope = {
  artifacts: [
    { type: KNOWLEDGE_TYPE, key: 'kn-p03', title: '临时加量必须区域审批通过后才能下单' },
    { type: KNOWLEDGE_TYPE, key: 'kn-p07', title: '门店申请超出标准时逐条列进超量清单' },
  ],
};

export const taskSeeds = {
  knowledge: () => ({
    origin: { page: 'knowledge', label: '知识沉淀' },
    title: '助手正在复核知识口径冲突「陈列上限按到货周期区分 / 陈列上限按品类取」',
    percent: 40,
    step_label: '对照行业包条目',
  }),
};

const bump = (label) => {
  const m = /^v(\d+)\.(\d+)$/.exec(label || '');
  return m ? `v${m[1]}.${Number(m[2]) + 1}` : 'v0.1';
};
const short = (t) => String(t || '').split(/[，,；;]/)[0];
const scopeKey = (scope) => (scope?.kind === 'entries' ? (scope.entries || []).map((x) => x.id).sort().join(',') : 'global');
const scopeText = (scope) => (scope?.kind === 'entries' ? (scope.entries || []).map((x) => x.name).join(' ') : '全局');
const pub = ({ detail, ...rest }) => rest;
const emptyCheck = () => ({ checked_at: null, duration_ms: 0, conflicts: [], consistent_with_materials: 0, unsourced: 0, duplicates: 0, broken_refs: 0 });

export function register(ctx) {
  const { on, json, err, db, sc, uid, F, cardOf, emitProj, scopeOf, commandHooks = [], putTask, later, convsOf } = ctx;

  const stateOf = (pid) => {
    if (!db.knowledge) db.knowledge = new Map();
    if (!db.knowledge.has(pid)) {
      const main = pid === F.MAIN_PID;
      const empty = sc.knowledge === 'empty';
      db.knowledge.set(pid, {
        project: main && !empty ? K.projectEntries() : [],
        industry: main ? K.industryEntries() : [],
        check: main && !empty ? K.lastCheck() : emptyCheck(),
        nominations: [],
      });
    }
    return db.knowledge.get(pid);
  };
  const find = (st, id) => st.project.find((e) => e.id === id) || st.industry.find((e) => e.id === id);
  const lockedErr = (pid, id) => (isLocked(scopeOf(pid), { type: KNOWLEDGE_TYPE, key: id })
    ? err(423, 'write_scope_locked', '这条知识在助手的写入范围里，确认生效或撤回前只读', scopeOf(pid)) : null);

  // 保存一次 = 一个版本：项目版本号 +1，推 workspace.changed 和 knowledge.updated
  function commit(pid, ids, change) {
    const card = cardOf(pid);
    const version = bump(card?.version_label);
    if (card) card.version_label = version;
    const sha = uid().padStart(8, '0').slice(-8);
    emitProj(pid, 'workspace.changed', { commit: sha, version_label: version, source: 'web', changeset_id: null, by: 'zhangsan', entries: [], artifacts: [], paths: ids.map((id) => `knowledge/${id}.md`) });
    if (card) emitProj(pid, 'project.updated', { fields: ['version_label'], meta: F.projectMeta(card) });
    emitProj(pid, 'knowledge.updated', { ids, change });
    return { commit: sha, version_label: version };
  }

  function validate(body) {
    const fields = {};
    const title = String(body.title || '').trim();
    if (!title) fields.title = 'required';
    else if (title.length > 60) fields.title = 'too_long';
    if (!TYPES.includes(body.type)) fields.type = 'invalid';
    if (!body.scope || !['global', 'entries'].includes(body.scope.kind) || (body.scope.kind === 'entries' && !body.scope.entries?.length)) fields.scope = 'invalid';
    if (!String(body.rule_md || '').trim()) fields.rule_md = 'required';
    return Object.keys(fields).length ? err(400, 'invalid_argument', '条目内容不完整', { fields }) : null;
  }

  // 语义口径冲突：发到对话里由助手复核（K-03 响应带 review_task；跑完冲突检查里多一条）
  function startReview(pid, entry, industry) {
    if (!putTask) return null;
    const cid = convsOf(pid)[0].id;
    const text = `复核知识口径冲突「${short(entry.title)} / ${short(industry.title)}」`;
    const task = putTask(pid, {
      task_id: `task-${uid()}`, origin: { page: 'knowledge', label: '知识沉淀' }, title: `助手正在${text}`, state: 'running', percent: 30,
      step_label: '对照行业包条目', conversation_id: cid, message_id: `m-${uid()}`, exec_id: null, started_at: new Date().toISOString(), finished_at: null, result: null,
    });
    later(pid, cid, 2600, () => {
      const st = stateOf(pid);
      st.check.conflicts = [...st.check.conflicts.filter((c) => c.project_entry.id !== entry.id), {
        id: `kc-${uid()}`, kind: 'scope_conflict', effective: 'project',
        project_entry: { id: entry.id, title: entry.title }, industry_entry: { id: industry.id, title: industry.title },
        note: '已按项目优先生效；如果是行业通用做法，建议提名回流',
      }];
      st.check.checked_at = new Date().toISOString();
      putTask(pid, { ...task, state: 'done', percent: 100, step_label: '写复核结论', finished_at: new Date().toISOString(), result: { summary: '口径冲突 1 处，已按项目优先', refs: [] } });
      emitProj(pid, 'knowledge.updated', { ids: [entry.id], change: 'updated' });
    });
    return task;
  }

  // ---------- K-06 冲突检查（先登记：路径会被 K-02 的 /knowledge/{id} 匹配到） ----------
  const checkView = (st) => ({ ...st.check, total_entries: st.project.length + st.industry.length });
  on('GET', '/projects/([^/]+)/knowledge/conflicts', ({ m }) => json(200, checkView(stateOf(m[1]))));
  on('POST', '/projects/([^/]+)/knowledge/conflicts', ({ m }) => {
    const st = stateOf(m[1]);
    const conflicts = st.project.filter((e) => e.overrides_industry).map((e) => {
      const ind = st.industry.find((x) => x.id === e.detail?.overrides?.entry_id || x.title === e.title) || { id: null, title: e.detail?.overrides?.entry_title || e.title };
      const old = st.check.conflicts.find((c) => c.project_entry.id === e.id);
      return old || { id: `kc-${uid()}`, kind: 'scope_conflict', effective: 'project', project_entry: { id: e.id, title: e.title }, industry_entry: { id: ind.id, title: ind.title }, note: '已按项目优先生效；如果是行业通用做法，建议提名回流' };
    });
    const titles = st.project.map((e) => `${e.title}|${scopeKey(e.scope)}`);
    st.check = {
      ...st.check, checked_at: new Date().toISOString(), duration_ms: 1200, conflicts,
      duplicates: titles.length - new Set(titles).size,
    };
    return json(200, checkView(st));
  });

  // ---------- K-01 列表 ----------
  on('GET', '/projects/([^/]+)/knowledge', ({ m, query }) => {
    const st = stateOf(m[1]);
    const type = query.get('type');
    const q = (query.get('q') || '').trim();
    const layer = query.get('layer') || 'all';
    const hit = (e) => (!type || e.type === type) && (!q || `${e.title}\n${scopeText(e.scope)}`.includes(q));
    const proj = st.project.filter(hit);
    const ind = st.industry.filter(hit);
    const list = layer === 'project' ? proj : layer === 'industry' ? ind : [...proj, ...ind];
    const limit = Math.max(1, Number(query.get('limit') || 50));
    const start = Number(query.get('cursor') || 0);
    const overridden = st.project.filter((e) => e.overrides_industry).length;
    return json(200, {
      items: list.slice(start, start + limit).map(pub),
      next_cursor: start + limit < list.length ? String(start + limit) : null,
      counts: { all: proj.length + ind.length, project: proj.length, industry: ind.length },
      layers: [
        { layer: 'industry', label: st.industry.length ? K.INDUSTRY_PACK.label : '', count: st.industry.length, packs: st.industry.length ? [K.INDUSTRY_PACK.name] : [], overridden },
        { layer: 'project', label: '本项目', count: st.project.length, overridden },
      ],
      loading_rule: K.LOADING_RULE,
    });
  });

  // ---------- K-02 详情 ----------
  on('GET', '/projects/([^/]+)/knowledge/([^/]+)', ({ m }) => {
    const e = find(stateOf(m[1]), decodeURIComponent(m[2]));
    if (!e) return err(404, 'not_found', '知识条目不存在');
    return json(200, { ...pub(e), ...(e.detail || K.defaultDetail(e)) });
  });

  // ---------- K-03 新增（知识沉淀页「新增条目」、对话里「沉淀为业务知识」） ----------
  on('POST', '/projects/([^/]+)/knowledge', ({ m, body }) => {
    const pid = m[1];
    const bad = validate(body);
    if (bad) return bad;
    const st = stateOf(pid);
    const title = body.title.trim();
    if (st.project.some((e) => e.title === title && scopeKey(e.scope) === scopeKey(body.scope))) {
      return err(409, 'knowledge_duplicate', '已有同名、同范围的知识条目', { title });
    }
    const now = new Date().toISOString();
    const industry = st.industry.find((e) => e.title === title);
    const fromChat = !!body.source?.conversation_id;
    const entry = {
      id: `kn-${uid()}`, title, layer: 'project', pack: null, type: body.type, scope: body.scope, confidence: body.confidence || 'medium',
      source: fromChat ? { kind: 'conversation', label: '对话', at: now } : { kind: 'manual', label: '手动新增', at: now },
      overrides_industry: !!industry, readonly: false, revision: `r1${uid()}`, updated_at: now,
      detail: {
        background_md: body.background_md || '',
        rule_md: body.rule_md,
        source_quote: fromChat ? { text: String(body.rule_md).replace(/^#+.*\n/, '').split('\n')[0].slice(0, 80), conversation_id: body.source.conversation_id, message_id: body.source.message_id || null, at: now, by: '张工' } : null,
        overrides: industry ? { pack: industry.pack, entry_id: industry.id, entry_title: industry.title, note: null } : null,
        used_by: body.scope.kind === 'entries' ? body.scope.entries.slice() : [],
      },
    };
    st.project.unshift(entry);
    const out = commit(pid, [entry.id], 'created');
    const conflicts = industry ? [{ kind: 'scope_conflict', with: `${K.INDUSTRY_PACK.label}行业包 / ${industry.title}`, note: '和行业包同名条目口径不同，已按项目优先生效' }] : [];
    const review = industry ? startReview(pid, entry, industry) : null;
    return json(201, { entry: pub(entry), ...out, conflicts, review_task: review });
  });

  // ---------- K-04 修改 ----------
  on('PUT', '/projects/([^/]+)/knowledge/([^/]+)', ({ m, body }) => {
    const pid = m[1];
    const st = stateOf(pid);
    const e = find(st, decodeURIComponent(m[2]));
    if (!e) return err(404, 'not_found', '知识条目不存在');
    if (e.readonly) return err(403, 'artifact_readonly', '行业包条目只读；要覆盖就在项目里写一条同名条目', { reason: 'industry_layer' });
    const locked = lockedErr(pid, e.id);
    if (locked) return locked;
    if (!body.base_revision) return err(400, 'invalid_argument', '缺少 base_revision', { fields: { base_revision: 'required' } });
    if (body.base_revision !== e.revision) {
      return err(409, 'revision_conflict', '这条知识已被修改', { target: e.id, expected: body.base_revision, actual: e.revision, changed_by: 'web', changed_at: e.updated_at, version_label: cardOf(pid)?.version_label || null });
    }
    const bad = validate(body);
    if (bad) return bad;
    const title = body.title.trim();
    if (st.project.some((x) => x.id !== e.id && x.title === title && scopeKey(x.scope) === scopeKey(body.scope))) {
      return err(409, 'knowledge_duplicate', '已有同名、同范围的知识条目', { title });
    }
    const detail = e.detail || K.defaultDetail(e);
    Object.assign(e, {
      title, type: body.type, scope: body.scope, confidence: body.confidence || e.confidence,
      revision: `r${uid()}`, updated_at: new Date().toISOString(),
      detail: { ...detail, background_md: body.background_md ?? detail.background_md, rule_md: body.rule_md },
    });
    const out = commit(pid, [e.id], 'updated');
    return json(200, { entry: pub(e), ...out, conflicts: [], review_task: null });
  });

  // ---------- K-05 删除 ----------
  on('DELETE', '/projects/([^/]+)/knowledge/([^/]+)', ({ m, body }) => {
    const pid = m[1];
    const st = stateOf(pid);
    const e = find(st, decodeURIComponent(m[2]));
    if (!e) return err(404, 'not_found', '知识条目不存在');
    if (e.readonly) return err(403, 'artifact_readonly', '行业包条目只读', { reason: 'industry_layer' });
    const locked = lockedErr(pid, e.id);
    if (locked) return locked;
    if (body?.base_revision && body.base_revision !== e.revision) {
      return err(409, 'revision_conflict', '这条知识已被修改', { target: e.id, expected: body.base_revision, actual: e.revision, changed_by: 'web', changed_at: e.updated_at, version_label: cardOf(pid)?.version_label || null });
    }
    const detail = e.detail || K.defaultDetail(e);
    st.project = st.project.filter((x) => x.id !== e.id);
    st.check.conflicts = st.check.conflicts.filter((c) => c.project_entry.id !== e.id);
    const out = commit(pid, [e.id], 'deleted');
    return json(200, { ...out, affected_artifacts: detail.used_by.length, restored_industry_entry: detail.overrides ? detail.overrides.entry_title : null });
  });

  // ---------- K-07 / K-08 提名回流 ----------
  on('POST', '/projects/([^/]+)/knowledge/([^/]+)/nomination-preview', ({ m }) => {
    const st = stateOf(m[1]);
    const e = find(st, decodeURIComponent(m[2]));
    if (!e) return err(404, 'not_found', '知识条目不存在');
    if (!st.industry.length) return err(409, 'pack_not_enabled', '本项目没有启用行业包');
    if (e.layer !== 'project') return err(400, 'invalid_argument', '只有项目层条目能提名回流', { fields: { knowledge_id: 'industry_layer' } });
    return json(200, { ...K.nominationPreview(e), pack: { ...K.INDUSTRY_PACK } });
  });
  on('POST', '/projects/([^/]+)/knowledge/([^/]+)/nominations', ({ m, body }) => {
    const st = stateOf(m[1]);
    const e = find(st, decodeURIComponent(m[2]));
    if (!e) return err(404, 'not_found', '知识条目不存在');
    if (body.confirm_general !== true) return err(400, 'invalid_argument', '要先确认这条知识在行业内通用', { fields: { confirm_general: 'required' } });
    if (!String(body.sanitized_text || '').trim()) return err(400, 'invalid_argument', '缺少脱敏后的条目', { fields: { sanitized_text: 'required' } });
    const id = `nm-${uid()}`;
    st.nominations.push({ id, knowledge_id: e.id, industry_pack: body.industry_pack, state: 'pending_review' });
    return json(201, { nomination_id: id, state: 'pending_review' });
  });

  // ---------- S-20：「在对话里复核」 ----------
  commandHooks.push({
    match: (b) => b.command === '/knowledge-review',
    title: (b) => `助手正在${b.text || '复核知识口径冲突'}`,
    steps: ['对照资料原文', '对照行业包条目', '写复核结论'],
    finish: (pid, b) => {
      const st = stateOf(pid);
      const c = st.check.conflicts.find((x) => x.project_entry.id === b.args?.project_entry);
      if (c) c.note = '助手在对话里复核过：项目口径成立，已按项目优先；如果是行业通用做法，建议提名回流';
      emitProj(pid, 'knowledge.updated', { ids: c ? [c.project_entry.id] : [], change: 'updated' });
    },
  });
}
