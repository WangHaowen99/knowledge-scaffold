// 3.15 报告（RP-01～RP-06）；规则检查报告的专家裁决用 S-11 回答（index.js），这里按对话里的 question.answered 判断裁决过没有。
// 只有主示例项目「门店补货单编制」有报告，其余项目报告列表为空。
// 没有额外的 mock 参数：逐条「发给助手」（RP-05）、「确认裁决」（S-11）、「重新校验」（RP-06）都会改内存里的状态。
import * as S from './scenario.js';

// 10 月某天某时（落在过去），与 fixtures 的写法一致
function at(month, day, h, m) {
  const d = new Date();
  d.setMonth(month - 1, day);
  d.setHours(h, m, 0, 0);
  if (d.getTime() > Date.now()) d.setFullYear(d.getFullYear() - 1);
  return d.toISOString();
}

const ARB_QID = 'q-rp-arb1';
const e = (key) => S.entryRef(key);
const loc = (label, key, page = 'studio-table') => ({ label, target: key ? e(key) : null, page: key ? page : null });
const item = (id, code, level, text, location, status, status_note) => ({ id, code, level, text, location, status, status_note });

function seed() {
  const cnt = S.ENTRIES.length;
  const rule = {
    id: 'rp-rule', name: '规则检查报告 · 超量与审批提示', type: 'rule', type_label: '规则', at: at(10, 31, 21, 52), summary: '错误 1 · 警告 2',
    path: 'reports/rule/超量与审批提示.md',
    scope_text: '切片「超量与审批提示」', mode: 'regression', duration_ms: 3200,
    previous: { at: at(10, 31, 21, 31), summary: '错误 2' },
    progress: { handled: 2, total: 5, note: '没有错误后，节点「校验与走查」才能确认，发布关卡才会放行。' },
    repair: { rounds: [{ n: 1, count: 4 }, { n: 2, count: 2 }, { n: 3, count: 1 }], limit: 2, exhausted: true },
    items: [
      item('ri1', 'E-ROUTE-01', 'error', '操作「超量与审批提示」的输出「审批提示」没有交接对象', loc('操作 · 超量与审批提示 · 步骤 4', 'overage_and_hint'), 'expert', '第 2 轮仍未收敛'),
      item('ri2', 'W-REF-03', 'warning', '规则 R-11 引用了未定义对象「区域审批」', loc('规则 · R-11 第 6 行', 'r11_temp_increase'), 'fixing', '助手修复中'),
      item('ri3', 'W-EVID-01', 'warning', '属性「生效日期」既没有证据锚点也没有专家确认', loc('对象 · 陈列量标准 · 属性', 'display_qty_std'), 'pending', '待处理'),
      item('ri4', 'E-SRC-02', 'error', '对象「超量清单」缺少数据来源', loc('对象 · 超量清单 · 数据来源', 'overage_list'), 'fixed', '第 1 轮已修复'),
      item('ri5', 'W-REL-05', 'warning', '操作步骤 3 引用的 [[安全库存标准]] 与关系声明不一致', loc('操作 · 超量与审批提示 · 步骤 3', 'overage_and_hint'), 'fixed', '第 2 轮已修复'),
    ],
    raw: (y) => [
      '# 规则检查报告 · 超量与审批提示',
      `生成时间：${y}-10-31 21:52`,
      '范围：切片「超量与审批提示」（回归模式，只查改动）',
      '',
      '## 错误（1）',
      '- [ ] E-ROUTE-01 操作「超量与审批提示」的输出「审批提示」没有交接对象',
      '      位置：ontology/operation/超量与审批提示.md 步骤 4',
      '',
      '## 警告（2）',
      '- [ ] W-REF-03 规则 R-11 引用了未定义对象「区域审批」',
      '- [ ] W-EVID-01 属性「生效日期」既没有证据锚点也没有专家确认',
      '',
      '## 已修复（2）',
      '- [x] E-SRC-02 对象「超量清单」缺少数据来源',
      '- [x] W-REL-05 步骤 3 的引用与关系声明不一致',
    ].join('\n'),
  };
  const link = {
    id: 'rp-link', name: '链接检查报告', type: 'link', type_label: '链接', at: at(10, 31, 21, 52), summary: '孤立 2 · 未用 1 · 失效 1',
    path: 'reports/link/全知识包.md',
    scope_text: `全知识包 · ${cnt} 个元素`, mode: 'full', duration_ms: 800,
    previous: { at: at(10, 30, 16, 40), summary: '失效 2' },
    progress: { handled: 0, total: 4, note: '失效链接会让关系图断边，建议先处理。' },
    repair: null,
    extras: { orphans: 2, unused: 1, dangling: 1 },
    items: [
      item('li1', 'LINK-ORPHAN', 'warning', '对象「门店等级」', loc('对象 · 门店等级', 'store_level', 'studio'), 'pending', '没有任何链接进出'),
      item('li2', 'LINK-ORPHAN', 'warning', '对象「历史补货单」', loc('对象 · 历史补货单', 'history_order', 'studio'), 'pending', '没有任何链接进出'),
      item('li3', 'LINK-UNUSED', 'warning', '规则「R-03 必填项提示」', loc('规则 · R-03', 'r03_required_hint', 'studio'), 'pending', '没有被任何操作引用'),
      item('li4', 'LINK-DANGLING', 'warning', '[[区域审批]]', loc('规则 · R-11 第 6 行', 'r11_temp_increase', 'studio'), 'pending', '指向的元素不存在'),
    ],
    raw: (y) => [
      '# 链接检查报告',
      `生成时间：${y}-10-31 21:52`,
      `范围：全知识包（${cnt} 个元素）`,
      '',
      '## 孤立（2）',
      '- 对象「门店等级」：没有任何链接进出',
      '- 对象「历史补货单」：没有任何链接进出',
      '',
      '## 未用（1）',
      '- 规则「R-03 必填项提示」：没有被任何操作引用',
      '',
      '## 失效链接（1）',
      '- [[区域审批]]：指向的元素不存在（规则 R-11 第 6 行）',
    ].join('\n'),
  };
  const counts = [
    { objects: 3, terms: 4 }, { objects: 6, rules: 14, terms: 21 }, { objects: 2, rules: 3 }, { objects: 1, rules: 2 },
    { objects: 2, terms: 6 }, { fields: 8 }, { fields: 12 },
  ];
  const kindOf = (n) => (/xlsx$/.test(n) ? 'xls' : /pdf$/.test(n) ? 'pdf' : 'doc');
  const research = {
    id: 'rp-research', name: '资料分析报告', type: 'research', type_label: '资料', at: at(10, 30, 18, 20), summary: '7 份读全 · 核验 46 处',
    path: 'reports/research/资料分析.md',
    scope_text: '资料区', mode: 'research', duration_ms: 400000,
    previous: { at: at(10, 28, 15, 10), summary: '读全 86%' },
    progress: { handled: 41, total: 41, note: '资料有新文件时，助手会在对话里提议增量更新。' },
    repair: null,
    extras: {
      total: S.MATERIALS.length, read_pct: 100,
      found: { items: 41, terms: 13, rule_candidates: 17 },
      candidates: { objects: 14, rules: 17 }, open_questions: 0,
      contradictions: { total: 1, resolved: 1 },
      purposes: ['goal', 'evidence', 'schema'].map((p) => ({ value: p, count: S.MATERIALS.filter((m) => m.purpose === p).length })),
      items: S.MATERIALS.map((m, i) => ({ path: m.path, name: m.name, kind: kindOf(m.name), purpose: m.purpose, purpose_auto: true, read_pct: 100, status: 'done', counts: counts[i] })),
      problems: [{ id: 'pb-1', kind: 'contradiction', text_md: '资料矛盾 1 处（陈列上限：补货规则 §7.3 和样例补货单 B 第 4 节不一致），已按补货规则裁决，记在决策台账。', anchors: [], state: 'resolved' }],
      verification: { total: 46, passed: 46, unverifiable: 0, pending: 0, checked_at: at(10, 30, 18, 20) },
    },
    items: [],
    raw: () => ['# 资料分析报告', '', `资料 ${S.MATERIALS.length} 份，全部读完；识别业务项 41、术语 13、规则候选 17。`, '', '## 资料矛盾（1，已裁决）', '- 陈列上限：补货规则 §7.3 和样例补货单 B 第 4 节不一致，按补货规则执行'].join('\n'),
  };
  const simple = (id, name, type, type_label, when, summary, scope, items, progress) => ({
    id, name, type, type_label, at: when, summary, path: `reports/${type}/${name}.md`, scope_text: scope, mode: type === 'trace' ? 'full' : 'regression',
    duration_ms: 1500, previous: null, progress, repair: null, items,
    raw: (y) => [`# ${name}`, `生成时间：${y}-${fmtMd(when)}`, `范围：${scope}`, '', ...items.map((x) => `- [${x.status === 'fixed' ? 'x' : ' '}] ${x.code} ${x.text}`)].join('\n'),
  });
  return [
    rule, link, research,
    simple('rp-semantic', '语义检查报告', 'semantic', '语义', at(10, 31, 21, 50), '警告 2', '切片「超量与审批提示」', [
      item('si1', 'W-SEM-02', 'warning', '规则 R-09「门店申请取严」和操作步骤 2 的说法不一致', loc('规则 · R-09', 'r09_store_stricter'), 'pending', '待处理'),
      item('si2', 'W-SEM-04', 'warning', '对象「审批提示」的描述没有说明交给谁', loc('对象 · 审批提示', 'approval_hint'), 'pending', '待处理'),
    ], { handled: 0, total: 2, note: '语义检查只给提醒，不挡发布。' }),
    simple('rp-trace', '场景追踪报告', 'trace', '追踪', at(10, 31, 21, 31), '21/23 通过', '全部场景 23 个', [
      item('ti1', 'T-FAIL', 'error', '场景 7「门店申请超出标准」推演结果和期望不一致', loc('操作 · 超量与审批提示', 'overage_and_hint'), 'pending', '待处理'),
      item('ti2', 'T-FAIL', 'error', '场景 12「临期品按先到先出核对」缺少到货批次', loc('操作 · 临期损耗编制', 'expiry_check'), 'pending', '待处理'),
    ], { handled: 21, total: 23, note: '两个没通过的场景处理完，全量回归才算通过。' }),
    simple('rp-candidate', '资料分析候选', 'candidate', '候选', at(10, 30, 18, 20), '候选 41 · 已审阅 41', '资料区 7 份', [], { handled: 41, total: 41, note: '候选都已审阅，进入建模。' }),
    simple('rp-coverage', '覆盖检查报告', 'coverage', '覆盖', at(10, 30, 17, 55), '错误 0 · 警告 1', '全知识包', [
      item('ci1', 'W-COV-01', 'warning', '规则「R-03 必填项提示」没有场景覆盖', loc('规则 · R-03', 'r03_required_hint'), 'pending', '待处理'),
    ], { handled: 0, total: 1, note: '覆盖不足只给提醒。' }),
    simple('rp-diagram', '流程图校验', 'diagram', '流程图校验', at(10, 29, 16, 2), '全部通过', '流程图 2 张', [], { handled: 0, total: 0, note: '流程图校验全部通过。' }),
  ];
}
function fmtMd(iso) {
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function arbitration(cid) {
  return {
    question_id: ARB_QID, kind: 'expert', conversation_id: cid, exec_id: null, step_label: '规则检查',
    eyebrow: '交给你裁决', title: '「审批提示」交给谁处理？',
    background_md: '补货规则 §11.4 说「提交区域审批」[^1]，但资料里没有「区域审批」的定义。',
    anchors: [{ n: 1, anchor: 'file:resource/others/02-门店补货规则.docx#p:11.4' }],
    options: [
      { id: 'A', key: 'A', label: '新建对象「区域审批」，审批提示交给它', consequence: '新增 1 个对象，补 1 条关系；R-11 的警告一起消除', recommended: false, preview: null, custom: false },
      { id: 'B', key: 'B', label: '审批提示挂到「补货台账」，不单独建对象', consequence: '不新增对象；台账多一个「审批状态」属性', recommended: false, preview: null, custom: false },
      { id: 'C', key: 'C', label: '先保留错误，等业务确认后再定', consequence: '发布检查会被这 1 个错误挡住', recommended: false, preview: null, custom: false },
    ],
    multi: false, allow_custom: false, custom_placeholder: '', state: 'open', answer: null,
    inbox_item_id: `report:rp-rule`, affected: [e('overage_and_hint'), e('approval_hint')],
  };
}

export function register(ctx) {
  const { on, json, err, db, hub, uid, F, convKey, convsOf, emitConv, runReply } = ctx;
  const isMain = (pid) => pid === F.MAIN_PID;
  const store = () => db.reports || (db.reports = seed());
  const find = (pid, id) => (isMain(pid) ? store().find((r) => r.id === id) : null);
  const answered = (pid, cid) => hub.history(convKey(pid, cid)).some((ev) => ev.type === 'question.answered' && ev.data?.question_id === ARB_QID);

  // 裁决过以后：裁决卡收起，「交给你裁决」那条转成修复中
  function view(pid, r) {
    const cid = convsOf(pid)[0].id;
    const done = r.type === 'rule' && answered(pid, cid);
    if (done) r.items.forEach((x) => { if (x.status === 'expert') Object.assign(x, { status: 'fixing', status_note: '按你的裁决修改中' }); });
    const { raw, items, ...rest } = r;
    return { ...rest, items: items.slice(0, 100), items_next_cursor: null, arbitration: r.type === 'rule' && !done && items.some((x) => x.status === 'expert') ? arbitration(cid) : null, extras: r.extras || null };
  }
  const summaryOf = ({ id, name, type, type_label, at: when, summary, path }) => ({ id, name, type, type_label, at: when, summary, path });

  // 发给助手：等价于 S-20，发一条报告修复命令到工作台对话
  function command(pid, text, origin) {
    const cid = convsOf(pid)[0].id;
    const msgId = `m-${uid()}`;
    emitConv(pid, cid, 'message.user', { message_id: msgId, text, attachments: [], references: [], origin: { kind: 'report', page: 'reports', label: origin, task_id: null }, delivery: 'immediate' });
    runReply(pid, cid, text);
    return {
      task: { task_id: `task-${uid()}`, origin: { page: 'reports', label: origin }, title: text, state: 'running', percent: null, step_label: null, conversation_id: cid, message_id: msgId, exec_id: null, started_at: new Date().toISOString(), finished_at: null, result: null },
      conversation_id: cid, message_id: msgId,
    };
  }

  on('GET', '/projects/([^/]+)/reports', ({ m, query }) => {
    const types = (query.get('type') || '').split(',').filter(Boolean);
    const items = isMain(m[1]) ? store().filter((r) => !types.length || types.includes(r.type)).map(summaryOf) : [];
    return json(200, { items, next_cursor: null });
  });
  on('POST', '/projects/([^/]+)/reports/recheck', ({ m, body }) => {
    const pid = m[1];
    if (!['rule', 'semantic', 'link', 'coverage', 'diagram'].includes(body.kind)) return err(400, 'invalid_argument', '检查类型不对', { fields: { kind: 'invalid' } });
    if (body.kind === 'link') {
      const r = find(pid, 'rp-link');
      if (!r) return err(404, 'not_found', '报告不存在');
      r.at = new Date().toISOString();
      r.previous = { at: r.at, summary: r.summary };
      return json(200, { report: view(pid, r) });
    }
    const out = command(pid, `重新校验：${body.kind === 'rule' ? '规则检查' : body.kind}（${body.mode === 'full' ? '全量' : '回归模式'}）`, '报告');
    return json(202, { command_task: out.task });
  });
  on('GET', '/projects/([^/]+)/reports/([^/]+)/items', ({ m, query }) => {
    const r = find(m[1], m[2]);
    if (!r) return err(404, 'not_found', '报告不存在');
    view(m[1], r);
    const status = query.get('status');
    const level = query.get('level');
    const items = r.items.filter((x) => (!status || x.status === status) && (!level || x.level === level));
    return json(200, {
      items, next_cursor: null,
      counts: { error: r.items.filter((x) => x.level === 'error').length, warning: r.items.filter((x) => x.level === 'warning').length, fixed: r.items.filter((x) => x.status === 'fixed').length },
    });
  });
  on('GET', '/projects/([^/]+)/reports/([^/]+)/raw', ({ m }) => {
    const r = find(m[1], m[2]);
    if (!r) return err(404, 'not_found', '报告不存在');
    return json(200, { path: r.path, content_md: r.raw(new Date(r.at).getFullYear()), truncated: false });
  });
  on('POST', '/projects/([^/]+)/reports/([^/]+)/fix', ({ m, body }) => {
    const pid = m[1];
    const r = find(pid, m[2]);
    if (!r) return err(404, 'not_found', '报告不存在');
    const ids = body.item_ids || null;
    const targets = r.items.filter((x) => x.status !== 'fixed' && (!ids || ids.includes(x.id)));
    if (ids && !targets.length) return err(400, 'invalid_argument', '条目已修复', { fields: { item_ids: 'fixed' } });
    targets.forEach((x) => Object.assign(x, { status: 'fixing', status_note: '助手修复中' }));
    const text = ids && targets.length === 1 ? `修复报告条目：${targets[0].text.replace(/\[\[|\]\]/g, '')}` : `逐条修复「${r.name}」，完成后重新运行对应校验并汇报`;
    return json(202, command(pid, text, r.name));
  });
  on('GET', '/projects/([^/]+)/reports/([^/]+)', ({ m }) => {
    const r = find(m[1], m[2]);
    if (!r) return err(404, 'not_found', '报告不存在');
    return json(200, view(m[1], r));
  });
}
