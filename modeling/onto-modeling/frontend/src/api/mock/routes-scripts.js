// mock · 3.18 脚本与求解契约（SC-01～SC-03、SV-01～SV-03）与代码生成页的页面命令（S-20 → commandHooks）
// 只有主示例项目「门店补货单编制」有脚本和求解契约，其余项目是空的。数据见 scripts-data.js。
//
// mock 参数（?mock=…，和其他参数用逗号拼）：
//   sandbox:off     脚本验证沙箱不可用（SC-01 / SC-03）
//   scripts:empty   还没扫描过、还没有脚本，也没有求解契约
//   sv:conflict     第一次保存求解契约时返回 409 revision_conflict（助手刚改过这份契约，修订号变了）
//   task:scripts    一打开就有「为「补货编制主流程」生成评估器」的进行中任务（taskSeeds）
//   scope:running / scope:pending   写入范围锁两态（index.js；lockScope 把「补货编制主流程」的求解契约并进锁）
import { isLocked } from '../../state/readonly.js';
import { isEnabled } from '../../lib/solver-contract-model.js';
import * as S from './scenario.js';
import * as D from './scripts-data.js';

export const SOLVER_TYPE = 'codegen.solver_contract';
const SOLVER_KEY_RE = /^[a-z][a-z0-9_]{0,31}$/;
const MIN = 60 * 1000;
const SIDECAR_FILES = {
  entry: 'model.yaml', input_schema: 'input.schema.json', result_schema: 'result.schema.json',
  fact_snapshot_schema: 'fact-snapshot.schema.json', presentation: 'presentation.yaml',
};

export const lockScope = {
  artifacts: [{ type: SOLVER_TYPE, key: D.MAIN_SOLVER.key, title: D.MAIN_SOLVER.operation }],
  paths: [`ontology/solver/${D.MAIN_SOLVER.key}/**`],
};

export const taskSeeds = {
  scripts: () => ({
    origin: { page: 'scripts', label: '代码生成' },
    title: `助手正在为「${D.MAIN_SOLVER.operation}」生成评估器`,
    percent: 62,
    step_label: '第 2 步 / 共 3 步',
  }),
};

const hex = () => Math.random().toString(16).slice(2, 10).padEnd(8, '0');
const newRevision = () => `sha256:${hex()}${hex()}`;
const sameJson = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

function nextVersion(label) {
  const m = String(label || 'v0.12').match(/^v(\d+)\.(\d+)$/);
  return m ? `v${m[1]}.${Number(m[2]) + 1}` : 'v0.13';
}

// 和后端同口径的结构检查（只做 mock 自洽需要的几条）：error 拒绝保存，warning 只提示
export function validateOperations(ops, operationNames, families = D.FAMILIES) {
  const rejected = [];
  const findings = [];
  const keys = new Map();
  ops.forEach((op) => {
    const k = String(op?.binding?.solver_key || '').trim();
    keys.set(k, (keys.get(k) || 0) + 1);
  });
  ops.forEach((op) => {
    const b = op?.binding || {};
    const key = String(b.solver_key || '').trim();
    const path = `ontology/solver/${key || '?'}/binding.yaml`;
    const f = (list, code, message, field, level = 'error') => list.push({ code, level, message, path, field });
    if (!SOLVER_KEY_RE.test(key)) f(rejected, 'solver_contract_invalid', `求解标识「${key}」须以小写字母开头，只含小写字母、数字、下划线`, 'solver_key');
    else if (keys.get(key) > 1) f(rejected, 'solver_contract_invalid', `求解标识「${key}」重复`, 'solver_key');
    const operation = String(b.operation || '').trim();
    if (!operationNames.includes(operation)) f(rejected, 'solver_contract_reference_missing', `Operation「${operation}」不存在`, 'operation');
    const family = b.declaration?.engine_requirements?.problem_family;
    const known = families.find((x) => x.id === family);
    if (!known) f(rejected, 'solver_contract_version_incompatible', `问题族 ${family || '（空）'} 不在平台注册目录中`, 'declaration.engine_requirements.problem_family');
    else {
      const bad = (b.declaration.engine_requirements.capabilities || []).filter((c) => !known.capabilities.includes(c));
      if (bad.length) f(rejected, 'solver_contract_version_incompatible', `问题族 ${family} 不支持能力：${bad.join('、')}`, 'declaration.engine_requirements.capabilities');
    }
    if (op.template) {
      const m = op.template.display_mapping || {};
      if (!m.OPTION_0 || !m.OPTION_1) f(rejected, 'solver_contract_invalid', 'display_mapping 的显示名称不能为空', 'display_mapping');
      else if (m.OPTION_0 === m.OPTION_1) f(rejected, 'solver_contract_invalid', 'display_mapping 中两个选项的显示名称相同，结果无法区分', 'display_mapping');
      if (Boolean(op.template.process_name) !== Boolean(op.template.stream_name)) f(rejected, 'solver_contract_invalid', '结果文字的两项须同时填写，或都留空', 'text_presentation');
    } else {
      Object.keys(SIDECAR_FILES).forEach((role) => {
        if (!String(op?.sidecars?.[role] || '').trim()) f(rejected, 'solver_contract_invalid', `${SIDECAR_FILES[role]} 内容为空`, role);
      });
    }
    if (!(b.units || []).length) f(findings, 'solver_contract_invalid', '没有声明字段单位，求解结果里的数值不带单位', 'units', 'warning');
  });
  return { rejected, findings };
}

export function register(ctx) {
  const { on, json, err, db, sc, F, cardOf, convsOf, emitProj, scopeOf, setScope, commandHooks } = ctx;
  const isMain = (pid) => pid === F.MAIN_PID;
  const empty = sc.scripts === 'empty';

  // 脚本原文放进文件仓库（routes-studio 的 F-02 按条目路径读脚本时取这里）
  const putSources = () => {
    db.files = db.files || new Map();
    ['build_replenish_order', 'safety_stock_strictest', 'overage_compare'].forEach((k) => {
      const path = D.scriptPath(`${k}.py`);
      if (!db.files.has(path)) db.files.set(path, { content: D.scriptSource(k), revision: `r-${k.length}` });
    });
  };
  putSources();

  const store = db.codegen || (db.codegen = new Map());
  function state(pid) {
    if (!store.has(pid)) {
      const main = isMain(pid) && !empty;
      const revision = main ? newRevision() : '';
      store.set(pid, {
        candidates: main ? D.candidates() : { scanned_at: null, items: [], unsuitable: [] },
        scripts: main ? D.scripts() : [],
        verifications: main ? { safety_stock_strictest: D.verification('safety_stock_strictest'), build_replenish_order: D.verification('build_replenish_order') } : {},
        solver: {
          revision, revision_no: main ? 7 : 0,
          operations: main ? D.solverOperations() : [],
          conflicted: false, last_change: null,
        },
      });
    }
    return store.get(pid);
  }
  const operationRefs = (pid) => (isMain(pid) ? S.ENTRIES.filter((x) => x.kind === 'operation').map((x) => S.entryRef(x.key)) : []);
  const refByName = (pid, name) => operationRefs(pid).find((o) => o.name === name || o.key === name) || { kind: 'operation', id: `operation:${name}`, key: name, name };
  const solverLock = (pid) => {
    const lock = scopeOf(pid);
    const ops = state(pid).solver.operations;
    return ops.some((o) => isLocked(lock, { type: SOLVER_TYPE, key: o.solver_key })) ? lock : null;
  };

  // ---------- 视图 ----------
  function opView(pid, op) {
    const b = op.binding || {};
    const p = op.template_params || {};
    const map = p.display_mapping || {};
    return {
      solver_key: op.solver_key,
      operation: refByName(pid, b.operation),
      enabled: isEnabled(b),
      mode: op.mode,
      problem_family: b.declaration?.engine_requirements?.problem_family || '',
      capabilities: [...(b.declaration?.engine_requirements?.capabilities || [])],
      field_units: (b.units || []).map((u) => ({ field: String(u.field || ''), unit: String(u.unit || ''), note: String(u.note || '') })),
      evaluator: op.evaluator ? { path: op.evaluator, kind: 'declarative' } : null,
      option_labels: map.OPTION_0 || map.OPTION_1 ? [map.OPTION_0 || '', map.OPTION_1 || ''] : null,
      result_title: p.title || null,
      result_texts: p.process_name || p.stream_name ? [p.process_name || '', p.stream_name || ''] : null,
      sidecars: { ...op.sidecars },
      managed: op.mode === 'template' ? [...D.ROLES] : [],
      binding: JSON.parse(JSON.stringify(b)),
      template_params: JSON.parse(JSON.stringify(p)),
    };
  }
  function view(pid) {
    const s = state(pid).solver;
    return {
      revision: s.revision,
      revision_no: s.revision_no,
      enabled: s.operations.length > 0,
      operations: s.operations.map((op) => opView(pid, op)),
      findings: [],
      // 端到端验证在发布后的独立评测里做（ADR-0091）：启用了求解操作时发布要确认
      requires_validation_after_publish: s.operations.some((o) => isEnabled(o.binding)),
      available_operations: operationRefs(pid),
      evaluators: s.operations.filter((o) => o.evaluator).map((o) => ({ path: o.evaluator, file: o.evaluator.split('/').pop() })),
      problem_families: D.FAMILIES.map((f) => ({ ...f, capabilities: [...f.capabilities] })),
      supported_profiles: [...D.PROFILES],
      template: D.template(),
      lock: solverLock(pid),
    };
  }

  // ---------- SC-01 ～ SC-03 ----------
  const sandbox = () => ({
    available: sc.sandbox !== 'off', network: 'denied', python: sc.sandbox !== 'off' ? '3.11.9' : null,
    checked_at: new Date(Date.now() - 3 * MIN).toISOString(),
  });
  on('GET', '/sandbox/status', () => json(200, sandbox()));
  on('GET', '/projects/([^/]+)/scripts', ({ m }) => {
    putSources();
    const st = state(m[1]);
    return json(200, { sandbox: sandbox(), candidates: st.candidates, items: st.scripts });
  });
  on('GET', '/projects/([^/]+)/scripts/([^/]+)/verification', ({ m }) => {
    const st = state(m[1]);
    const key = decodeURIComponent(m[2]);
    const row = st.scripts.find((r) => r.script.key === key);
    if (!row) return err(404, 'not_found', '脚本不存在');
    const v = st.verifications[key];
    return json(200, v ? { script: row.script, ...v } : { script: row.script, ran_at: null, status: 'not_run', duration_ms: null, network_access: false, cases: [] });
  });

  // ---------- SV-01 ～ SV-03 ----------
  on('GET', '/projects/([^/]+)/solver/contract', ({ m }) => json(200, view(m[1])));

  const operationsOf = (body) => (body && typeof body.contract === 'object' && body.contract ? body.contract.operations : undefined);
  on('POST', '/projects/([^/]+)/solver/contract/check', ({ m, body }) => {
    const pid = m[1];
    const ops = operationsOf(body);
    if (!Array.isArray(ops)) return err(400, 'solver_contract_invalid', 'contract.operations 必须是列表', { findings: [], fields: { 'contract.operations': 'list' } });
    const { rejected, findings } = validateOperations(ops, operationRefs(pid).map((o) => o.name));
    const current = state(pid).solver.operations;
    const withEvaluator = ops.find((op) => isEnabled(op.binding) && current.some((c) => c.solver_key === op.binding?.solver_key && c.evaluator));
    return json(200, {
      rejected,
      findings,
      rendered: ops.map((op) => (op.template ? D.renderSidecars(String(op.binding?.solver_key || ''), op.template) : null)),
      evaluator: withEvaluator ? { kind: 'declarative', constraints: 3 } : null,
    });
  });

  on('PUT', '/projects/([^/]+)/solver/contract', ({ m, body }) => {
    const pid = m[1];
    const st = state(pid);
    const s = st.solver;
    const ops = operationsOf(body);
    if (!Array.isArray(ops)) return err(400, 'solver_contract_invalid', 'contract.operations 必须是列表', { findings: [], fields: { 'contract.operations': 'list' } });
    // 改到写入范围里的求解契约：423
    const lock = scopeOf(pid);
    const before = new Map(s.operations.map((o) => [o.solver_key, o]));
    const touched = [
      ...ops.filter((op) => {
        const old = before.get(op.binding?.solver_key);
        return !old || !sameJson(old.binding, op.binding) || (op.template ? !sameJson(old.template_params, op.template) || old.mode !== 'template' : old.mode !== 'manual' || !sameJson(old.sidecars, op.sidecars));
      }).map((op) => op.binding?.solver_key),
      ...[...before.keys()].filter((k) => !ops.some((op) => op.binding?.solver_key === k)),
    ];
    if (touched.some((k) => isLocked(lock, { type: SOLVER_TYPE, key: k }))) return err(423, 'write_scope_locked', '这份求解契约在助手写入范围内', lock);
    // sv:conflict：第一次保存前助手刚改过（评估器自检时补了一个字段说明），修订号变了
    if (sc.sv === 'conflict' && !s.conflicted && s.operations.length) {
      s.conflicted = true;
      const units = s.operations[0].binding.units || [];
      if (units[0]) units[0].note = `${units[0].note}（按到货批次）`;
      s.revision = newRevision();
      s.revision_no += 1;
      s.last_change = { by: 'assistant', at: new Date(Date.now() - 2 * MIN).toISOString(), version_label: nextVersion(cardOf(pid)?.version_label) };
    }
    if (body.base_revision && body.base_revision !== s.revision) {
      const lc = s.last_change || {};
      return err(409, 'revision_conflict', '求解契约已被修改', {
        target: 'ontology/solver/', expected: body.base_revision, actual: s.revision,
        changed_by: lc.by || 'assistant', changed_at: lc.at || null, version_label: lc.version_label || null, kind: 'solver_contract',
      });
    }
    const { rejected } = validateOperations(ops, operationRefs(pid).map((o) => o.name));
    if (rejected.length) return err(422, 'solver_contract_rejected', '求解契约结构被拒', { findings: rejected });

    s.operations = ops.map((op) => {
      const key = String(op.binding.solver_key).trim();
      const old = before.get(key);
      const managed = Boolean(op.template);
      const params = managed ? JSON.parse(JSON.stringify(op.template)) : (old?.template_params || D.template().params);
      return {
        solver_key: key,
        binding: JSON.parse(JSON.stringify(op.binding)),
        template_params: params,
        mode: managed ? 'template' : 'manual',
        sidecars: managed ? D.renderSidecars(key, params) : { ...op.sidecars },
        evaluator: (managed && params.evaluator_path) || old?.evaluator || null,
      };
    });
    s.revision = newRevision();
    s.revision_no += 1;
    const card = cardOf(pid);
    const versionLabel = nextVersion(card?.version_label);
    const commit = hex().slice(0, 8);
    if (card) {
      card.version_label = versionLabel;
      emitProj(pid, 'project.updated', { fields: ['version_label'], meta: F.projectMeta(card) });
    }
    emitProj(pid, 'workspace.changed', { commit, version_label: versionLabel, source: 'web', changeset_id: null, by: 'zhangsan', entries: [], artifacts: s.operations.map((o) => ({ type: SOLVER_TYPE, key: o.solver_key, title: o.binding.operation })), paths: ['ontology/solver/'] });
    emitProj(pid, 'artifacts.updated', { type: SOLVER_TYPE, change: 'updated', keys: s.operations.map((o) => o.solver_key) });
    return json(200, { ...view(pid), commit, version_label: versionLabel });
  });

  // ---------- 页面命令（S-20）：按 steps 推进，跑完改数据、发 artifacts.updated ----------
  const fromScripts = (body) => body?.origin?.page === 'scripts';
  const scriptName = (key) => `${key}.py`;
  const solverTitle = (key) => {
    const op = state(F.MAIN_PID).solver.operations.find((o) => o.solver_key === key);
    return op?.binding?.operation || key;
  };
  const scriptsUpdated = (pid, keys) => emitProj(pid, 'artifacts.updated', { type: 'codegen.script', change: 'updated', keys });
  const markPassed = (pid, key, operation, ms) => {
    const st = state(pid);
    const row = st.scripts.find((r) => r.script.key === key);
    const next = {
      script: row?.script || (S.entry(key) ? S.entryRef(key) : D.scriptRef(key)), operation: row?.operation || operation || null,
      contract: { status: 'passed', findings: [] }, descriptor: 'synced', sandbox: { status: 'passed', duration_ms: ms, ran_at: new Date().toISOString() }, actions: ['regenerate'],
    };
    if (row) Object.assign(row, next); else st.scripts.push(next);
    st.verifications[key] = st.verifications[key]?.status === 'passed' ? { ...st.verifications[key], ran_at: new Date().toISOString() } : D.verificationAfterRun(key, ms);
    db.files = db.files || new Map();
    const path = D.scriptPath(scriptName(key));
    if (!db.files.has(path)) db.files.set(path, { content: D.scriptSource(key), revision: `r-${key}` });
  };

  commandHooks.push({
    match: (b) => fromScripts(b) && b.command === '/ontology-codegen-scan',
    title: () => '助手正在扫描可代码化的操作',
    steps: ['已读 4 / 11 个操作', '已读 11 / 11 个操作', '写扫描报告'],
    finish: (pid) => {
      const st = state(pid);
      st.candidates = isMain(pid) ? { ...D.candidates(), scanned_at: new Date().toISOString() } : { scanned_at: new Date().toISOString(), items: [], unsuitable: [] };
      emitProj(pid, 'artifacts.updated', { type: 'codegen.scan_report', change: 'updated', keys: ['latest'] });
    },
  });
  commandHooks.push({
    match: (b) => fromScripts(b) && b.command === '/ontology-codegen' && Array.isArray(b.args?.operations),
    title: (b) => `助手正在生成 ${b.args.operations.length} 个脚本并在沙箱里跑通`,
    steps: ['按操作写脚本', '契约检查', '沙箱验证（合成输入）'],
    finish: (pid, b) => {
      const keys = b.args.operations.map((op) => D.SCRIPT_FOR_OPERATION[op] || `${op}_script`);
      b.args.operations.forEach((op, i) => markPassed(pid, keys[i], S.entry(op) ? S.entryRef(op) : null, 900 + i * 300));
      scriptsUpdated(pid, keys);
    },
  });
  const scriptHook = (mode, title, steps, apply) => commandHooks.push({
    match: (b) => fromScripts(b) && b.command === '/ontology-codegen' && b.args?.mode === mode && b.args?.script,
    title: (b) => title(scriptName(b.args.script)),
    steps,
    finish: (pid, b) => { apply(pid, b.args.script); scriptsUpdated(pid, [b.args.script]); },
  });
  scriptHook('fix', (n) => `助手正在修复 ${n} 的契约问题`, ['对照头部注释和 argparse', '契约检查', '沙箱验证（合成输入）'], (pid, key) => markPassed(pid, key, null, 900));
  scriptHook('regenerate', (n) => `助手正在重新生成 ${n}`, ['重写脚本', '契约检查', '沙箱验证（合成输入）'], (pid, key) => markPassed(pid, key, null, 1000));
  scriptHook('descriptor', (n) => `助手正在重新生成 ${n} 的描述符`, ['读头部注释', '写描述符'], (pid, key) => {
    const row = state(pid).scripts.find((r) => r.script.key === key);
    if (row) Object.assign(row, { descriptor: 'synced', actions: row.actions.filter((a) => a !== 'regenerate_descriptor') });
  });

  // 生成评估器 / 按模板重新生成：跑的时候这份求解契约在助手写入范围里（运行中只读）。
  // 钩子的 title 只拿得到请求体，mock 里只有主示例项目有求解契约，开跑时就把它的锁设上；
  // 跑完直接解除（mock 的工作台没有这一轮的确认卡，不进「修改待确认」，那一态用 ?mock=scope:pending 看）
  const evaluatorLock = (key, cid) => ({
    state: 'assistant_running', changeset_id: null, conversation_id: cid, run_id: null, slice: null,
    node: { id: 'codegen.solver', version: 'codegen@0.9.2' }, entries: [],
    artifacts: [{ type: SOLVER_TYPE, key, title: solverTitle(key) }], paths: [`ontology/solver/${key}/**`, D.evaluatorPath(key)],
    reason_text: `助手正在为「${solverTitle(key)}」生成评估器`, since: new Date().toISOString(),
  });
  const ownsLock = (pid, key) => {
    const lock = scopeOf(pid);
    return lock.state === 'assistant_running' && !lock.slice && (lock.artifacts || []).length === 1 && lock.artifacts[0].key === key;
  };
  const startLock = (b) => {
    const key = b.args?.solver_key;
    const pid = F.MAIN_PID;
    if (!state(pid).solver.operations.some((o) => o.solver_key === key) || scopeOf(pid).state !== 'none') return;
    setScope(pid, evaluatorLock(key, b.conversation_id || convsOf(pid)[0].id));
  };
  const solverHook = (mode, title, steps, apply) => commandHooks.push({
    match: (b) => fromScripts(b) && b.command === '/ontology-solver-evaluator' && (b.args?.mode || 'evaluator') === mode,
    title: (b) => { startLock(b); return title(solverTitle(b.args?.solver_key)); },
    steps,
    finish: (pid, b) => {
      const key = b.args?.solver_key;
      const s = state(pid).solver;
      const op = s.operations.find((o) => o.solver_key === key);
      if (op) {
        apply(op);
        s.revision = newRevision();
        s.revision_no += 1;
        emitProj(pid, 'artifacts.updated', { type: SOLVER_TYPE, change: 'updated', keys: [key] });
      }
      if (ownsLock(pid, key)) setScope(pid, { ...scopeOf(pid), state: 'none', artifacts: [], paths: [], reason_text: '' });
    },
  });
  solverHook('evaluator', (t) => `助手正在为「${t}」生成评估器`, ['第 1 步 / 共 3 步', '第 2 步 / 共 3 步', '第 3 步 / 共 3 步'], (op) => {
    op.evaluator = op.evaluator || D.evaluatorPath(op.solver_key);
  });
  solverHook('template', (t) => `助手正在按模板重新生成「${t}」的附属文件和评估器`, ['按模板生成附属文件', '生成评估器', '自检'], (op) => {
    op.mode = 'template';
    op.sidecars = D.renderSidecars(op.solver_key, op.template_params);
    op.evaluator = op.evaluator || D.evaluatorPath(op.solver_key);
  });
}
