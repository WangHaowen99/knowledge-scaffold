// mock · 3.21b 知识包数据飞轮（FW-01～FW-08，形状照 develop !630 的后端）：每个已发布项目一份合成状态，
// 一条待确认的评测优化加 23 条历史。优化约 6 秒、发布约 4 秒、评测约 8 秒，每次读取时按经过的时间推进。
// ?mock=flywheel:off 未接统一账号（不可用），flywheel:readonly 已归档项目（只读）
import { releasesOf } from './publish-data.js';

const UNAVAILABLE = '数据飞轮需要统一账号登录：评测结果和 Hub 发布都按账号隔离';
// 与后端 RUN_FILTERS 的分组一致
const FILTERS = {
  all: null,
  waiting: ['waiting_optimize', 'waiting_publish', 'waiting_evaluation'],
  running: ['optimizing', 'publishing', 'evaluating'],
  success: ['published', 'restored', 'passed'],
  blocked: ['blocked', 'interrupted'],
  rolledback: ['rolledback'],
};
const ACTIVE = FILTERS.running;
const WAITING = FILTERS.waiting;
const RETRY = ['hub_failed', 'publish_interrupted', 'hub_expired', 'request_conflict', 'authorization', 'solver_unverified'];
const RECHECK = ['checks_failed', 'content_changed'];
const RESUMABLE = ['optimization_interrupted', 'optimization_failed', 'assistant_unavailable'];
const IGNORABLE = ['version_mismatch', 'suggestions_failed', 'no_changes', ...RESUMABLE];
const ROLLBACKABLE = ['published', 'waiting_publish', 'waiting_evaluation', 'blocked'];
const ROUNDS = [1, 3, 5, 0];
const DAYS = [1, 3, 7, 30, 0];
const PAGE_LIMIT = 50;
const PHASE = { optimize: 6, publish: 4, evaluate: 8 };
const LABELS = {
  optimize: '自动化处理优化建议',
  publish: '自动化确认发布与发起评测',
  max_rounds: '连续自动处理上限',
  delegation_days: '后台运行有效期',
};
const describe = {
  optimize: (on) => (on ? '已开启' : '已关闭'),
  publish: (on) => (on ? '已开启' : '已关闭'),
  max_rounds: (n) => (n ? `最多 ${n} 轮` : '无限次'),
  delegation_days: (n) => (n ? `${n} 天` : '无限制'),
};
const DIFF = [
  ['objects/补货建议单.md', [
    ['hunk', '@@ 属性 @@'],
    ['context', '| 建议数量 | suggest_qty | 数值 |'],
    ['removed', '| 状态 | status | 文本 |'],
    ['added', '| 状态 | status | 枚举（待审核、已下发、已关闭） |'],
    ['added', '| 审核人 | reviewer | 文本 |'],
  ]],
  ['rules/安全库存.md', [
    ['hunk', '@@ 计算口径 @@'],
    ['removed', '安全库存 = 日均销量 × 3。'],
    ['added', '安全库存 = 日均销量 × 补货周期 + 波动缓冲；新品按同类均值估算。'],
  ]],
  ['operations/生成补货建议.md', [
    ['hunk', '@@ 前置条件 @@'],
    ['context', '门店已完成当日盘点。'],
    ['added', '库存低于[[安全库存]]时才生成[[补货建议单]]。'],
  ]],
];

const runId = (n) => `FW-${String(n).padStart(3, '0')}`;
const bump = (v) => {
  const parts = String(v || '0.1.0').split('.').map(Number);
  parts[parts.length - 1] += 1;
  return parts.join('.');
};
const isFeedback = (run) => run.origin === 'consumer_feedback';
const canResume = (run) => ['waiting_optimize', 'interrupted'].includes(run.status)
  || (run.status === 'blocked' && RESUMABLE.includes(run.reason));
const canEvaluate = (run) => run.status === 'waiting_evaluation' || (run.status === 'blocked' && run.reason === 'evaluation_failed');
const canIgnore = (run) => ['waiting_optimize', 'interrupted'].includes(run.status)
  || (run.status === 'blocked' && IGNORABLE.includes(run.reason));
const pub = (run) => Object.fromEntries(Object.entries(run).filter(([k]) => !k.startsWith('_')));

// 23 条历史（FW-001～FW-023，新的在前）加一条刚收到的评测建议 FW-024
function seedRuns(pid, cur, prev, t) {
  const runs = [];
  let iter = 0;
  for (let n = 1; n <= 22; n += 1) {
    const ver = `0.${Math.floor(n / 4) + 1}.${n % 4}`;
    const at = t - (26 + (23 - n) * 6) * 3600;
    const score = 35 + ((n * 13) % 40);
    const run = {
      id: runId(n), kind: 'evaluation', origin: 'evaluation', version: ver, release_id: `rel-${pid}-${ver}`,
      eval_run_id: `eval-${runId(n).toLowerCase()}`, trigger: 'evaluation_page', source_id: null,
      score, cases: { pass: Math.round(score / 12.5), total: 8 }, suggestions: { count: 2 + (n % 3), categories: ['规则'] },
      status: 'published', reason: null, human_hold: true, mode: 'manual', received_at: at, snapshot: null, changed: null,
      evaluation: null, earlier_changes: false,
    };
    const published = () => {
      iter += 1;
      const after = Math.min(95, score + 10 + (n % 3) * 5);
      Object.assign(run, {
        status: 'published', snapshot: { iteration: iter }, changed: 1 + (n % 3), content_version: ver, target_version: bump(ver),
        base_release_id: run.release_id, published_release_id: `rel-${pid}-${bump(ver)}`, optimized_at: at + 300, published_at: at + 420,
        after_score: after, evaluation: { status: 'done', mode: n % 2 ? 'auto' : 'manual', passed: false, score: after },
      });
    };
    if (n === 22) {
      // 回退 FW-021：只恢复建模内容
      Object.assign(run, {
        kind: 'rollback', origin: 'rollback', source_id: runId(21), trigger: null, eval_run_id: null, score: null, cases: null,
        suggestions: null, status: 'restored', version: runs[0].version, release_id: runs[0].release_id,
        content_version: runs[0].version, snapshot: runs[0].snapshot, changed: runs[0].changed,
      });
    } else if (n === 21) {
      published();
      run.status = 'rolledback';
    } else if (n === 20) {
      published();
      Object.assign(run, {
        origin: 'consumer_feedback', score: null, after_score: null, cases: { total: 3 }, evaluation: null,
        suggestions: { count: 3, categories: ['操作'] },
      });
    } else if (n === 18) {
      iter += 1;
      Object.assign(run, { status: 'interrupted', reason: 'optimization_interrupted', snapshot: { iteration: iter } });
    } else {
      const kind = n === 19 ? 0 : n % 7;
      if (kind === 0 || kind === 4) {
        Object.assign(run, { status: 'passed', score: 100, cases: { pass: 8, total: 8 }, suggestions: { count: 0, categories: [] } });
      } else if (kind === 1 || kind === 5) run.status = 'ignored';
      else if (kind === 3) Object.assign(run, { status: 'expired', reason: 'version_mismatch' });
      else published();
    }
    runs.unshift(run);
  }
  iter += 1;
  const last = t - 26 * 3600;
  runs.unshift({
    id: runId(23), kind: 'evaluation', origin: 'evaluation', version: prev.version, release_id: prev.release_id,
    eval_run_id: 'eval-fw-023', trigger: 'evaluation_page', source_id: null, score: 37.5, cases: { pass: 3, total: 8 },
    suggestions: { count: 5, categories: ['对象', '规则', '操作'] }, status: 'published', reason: null, human_hold: false,
    mode: 'auto', publish_mode: 'auto', received_at: last, snapshot: { iteration: iter }, changed: 3,
    content_version: prev.version, target_version: cur.version, base_release_id: prev.release_id,
    published_release_id: cur.release_id, optimized_at: last + 360, published_at: last + 480, after_score: 62.5,
    evaluation: { status: 'done', mode: 'auto', passed: false, score: 62.5 }, earlier_changes: false,
  });
  runs.unshift({
    id: runId(24), kind: 'evaluation', origin: 'evaluation', version: cur.version, release_id: cur.release_id,
    eval_run_id: 'eval-fw-024', trigger: 'flywheel', source_id: runId(23), score: 62.5, cases: { pass: 5, total: 8 },
    suggestions: { count: 4, categories: ['对象', '规则'] }, status: 'waiting_optimize', reason: null, human_hold: true,
    mode: 'manual', received_at: t - 40 * 60, snapshot: null, changed: null, evaluation: null, earlier_changes: false,
  });
  return { runs, iter };
}

function seedAudit(t) {
  const at = (hours, sec = 0) => t - hours * 3600 - sec;
  return [
    { at: at(20), action: LABELS.publish, description: '已关闭', actor: 'zhangsan' },
    { at: at(20, 60), action: LABELS.optimize, description: '已关闭', actor: 'zhangsan' },
    { at: at(50), action: LABELS.max_rounds, description: '最多 3 轮', actor: 'zhangsan' },
    { at: at(50, 120), action: LABELS.publish, description: '已开启', actor: 'zhangsan' },
    { at: at(50, 180), action: LABELS.optimize, description: '已开启', actor: 'zhangsan' },
    { at: at(72), action: LABELS.delegation_days, description: '30 天', actor: 'zhangsan' },
  ];
}

const headerOf = (init, name) => {
  const h = init?.headers;
  if (!h) return '';
  if (typeof h.get === 'function') return h.get(name) || '';
  const key = Object.keys(h).find((k) => k.toLowerCase() === name.toLowerCase());
  return key ? h[key] : '';
};

export function register({
  on, json, err, db, sc = {}, cardOf, clock,
}) {
  const now = clock || (() => Date.now() / 1000);
  const readonly = sc.flywheel === 'readonly';
  const off = sc.flywheel === 'off';
  db.flywheel = db.flywheel || new Map();

  const stateOf = (pid) => {
    if (db.flywheel.has(pid)) return db.flywheel.get(pid);
    const t = now();
    const rel = releasesOf(db, pid, cardOf?.(pid));
    const pkgOf = (r) => ({ ontology_id: `pkg-${String(pid).replace(/[^a-z0-9]/gi, '').slice(-8)}`,
      release_id: r.release_id || `rel-${pid}-${r.version}`, version: r.version });
    const cur = rel[0] ? pkgOf(rel[0]) : null;
    const prev = rel[1] ? pkgOf(rel[1]) : { version: '0.1.0', release_id: `rel-${pid}-0.1.0` };
    const seeded = cur ? seedRuns(pid, cur, prev, t) : { runs: [], iter: 0 };
    const fw = {
      pid, pkg: cur || { ontology_id: null, release_id: null, version: null }, runs: seeded.runs, iter: seeded.iter,
      seq: seeded.runs.length, audit: cur ? seedAudit(t) : [], loginSaved: false, grantedAt: t,
      settings: { optimize: false, publish: false, max_rounds: 3, delegation_days: 30 },
    };
    db.flywheel.set(pid, fw);
    return fw;
  };

  // 一个阶段到时就接着下一个阶段（_t 是当前阶段的开始时间），读取一次能追上经过的全部时间
  const step = (fw, run, t) => {
    if (run.status === 'optimizing') {
      const spent = t - run._t;
      if (spent < PHASE.optimize) { run.step = Math.min(3, Math.floor(spent / 1.5)); return false; }
      Object.assign(run, { changed: 3, optimized_at: run._t + PHASE.optimize, step: 0 });
      if (fw.settings.publish) {
        Object.assign(run, { status: 'publishing', publish_mode: 'auto', _t: run._t + PHASE.optimize });
        return true;
      }
      Object.assign(run, { status: 'waiting_publish', human_hold: true });
      delete run._t;
      return false;
    }
    if (run.status === 'publishing') {
      const spent = t - run._t;
      if (spent < PHASE.publish) { run.step = Math.min(3, Math.floor(spent)); return false; }
      const ver = bump(fw.pkg.version);
      const releaseId = `rel-${fw.pid}-${ver}`;
      Object.assign(run, {
        target_version: ver, published_release_id: releaseId, base_release_id: run.base_release_id || fw.pkg.release_id,
        published_at: run._t + PHASE.publish, step: 0,
      });
      fw.pkg = { ...fw.pkg, version: ver, release_id: releaseId };
      if (run.kind === 'rollback' || isFeedback(run)) {
        run.status = 'published';
        delete run._t;
        return false;
      }
      if (fw.settings.publish && fw.loginSaved) {
        Object.assign(run, { status: 'evaluating', evaluation: { status: 'running', mode: 'auto' }, _t: run._t + PHASE.publish });
        return true;
      }
      Object.assign(run, { status: 'waiting_evaluation', human_hold: true, evaluation: { status: 'waiting' } });
      delete run._t;
      return false;
    }
    if (run.status === 'evaluating') {
      if (t - run._t < PHASE.evaluate) return false;
      Object.assign(run, {
        status: 'published', after_score: 100, evaluation: { ...run.evaluation, status: 'done', passed: true, score: 100 },
      });
      delete run._t;
    }
    return false;
  };
  const advance = (fw) => {
    const t = now();
    fw.runs.filter((run) => run._t != null).forEach((run) => { while (step(fw, run, t)); });
  };

  const view = (fw) => {
    const { settings } = fw;
    const active = fw.runs.find((run) => ACTIVE.includes(run.status));
    const candidate = fw.runs.find((run) => run.status === 'waiting_publish');
    const expires = settings.delegation_days ? fw.grantedAt + settings.delegation_days * 86400 : null;
    const background = settings.optimize || settings.publish;
    return {
      available: true,
      message: '',
      can_write: !readonly,
      project: { id: fw.pid, name: cardOf?.(fw.pid)?.name || fw.pid },
      package: fw.pkg,
      evaluation: { configured: true, reachable: true, notifications: true, evaluating: fw.runs.some((run) => run.status === 'evaluating') },
      settings: { ...settings },
      auto_rounds: 0,
      delegation: {
        evaluation: { status: settings.optimize ? 'active' : 'off', expires_at: settings.optimize ? expires : null, message: '' },
        hub: { status: !fw.pkg.release_id ? 'unlinked' : settings.publish ? 'active' : 'off', expires_at: settings.publish ? expires : null, message: '' },
        login: settings.publish ? (fw.loginSaved ? 'active' : 'missing') : 'off',
        background,
      },
      assistant_busy: false,
      active_id: active?.id || null,
      candidate_id: candidate?.id || null,
      pending: fw.runs.filter((run) => (WAITING.includes(run.status) && run.human_hold !== false) || run.status === 'blocked').length,
      runs: fw.runs.slice(0, PAGE_LIMIT).map(pub),
      total: fw.runs.length,
      audit: fw.audit.slice(0, PAGE_LIMIT),
    };
  };

  const record = (fw, key, value, user) => {
    fw.audit.unshift({ at: now(), action: LABELS[key], description: describe[key](value), actor: user?.username || 'zhangsan' });
  };
  const pause = (fw, user) => ['optimize', 'publish'].forEach((key) => {
    if (!fw.settings[key]) return;
    fw.settings[key] = false;
    if (key === 'publish') fw.loginSaved = false;
    record(fw, key, false, user);
  });
  const page = (items, query) => {
    const offset = Math.max(0, Number(query.get('offset')) || 0);
    const limit = Math.min(PAGE_LIMIT, Math.max(1, Number(query.get('limit')) || 20));
    const slice = items.slice(offset, offset + limit);
    return { items: slice, total: items.length, next_offset: offset + slice.length < items.length ? offset + slice.length : null };
  };
  const guard = (write) => {
    if (off) return err(401, 'login_required', UNAVAILABLE);
    if (write && readonly) return err(409, 'project_archived', '项目已归档，数据飞轮只能查看');
    return null;
  };

  on('GET', '/projects/([^/]+)/flywheel', ({ m }) => {
    if (off) return json(200, { available: false, message: UNAVAILABLE });
    const fw = stateOf(m[1]);
    advance(fw);
    return json(200, view(fw));
  });

  on('PUT', '/projects/([^/]+)/flywheel/settings', ({ m, body, init, user }) => {
    const blocked = guard(true);
    if (blocked) return blocked;
    const fw = stateOf(m[1]);
    advance(fw);
    const changes = body || {};
    for (const key of ['optimize', 'publish']) {
      if (key in changes && typeof changes[key] !== 'boolean') return err(400, 'invalid_argument', `${LABELS[key]}只能是开或关`);
    }
    if ('max_rounds' in changes && !ROUNDS.includes(changes.max_rounds)) {
      return err(400, 'invalid_argument', '连续自动处理上限只能是 1、3、5 轮或无限次');
    }
    if ('delegation_days' in changes && !DAYS.includes(changes.delegation_days)) {
      return err(400, 'invalid_argument', '后台授权有效期只能是 1、3、7、30 天或无限制');
    }
    Object.keys(LABELS).forEach((key) => {
      if (!(key in changes) || changes[key] === fw.settings[key]) return;
      fw.settings[key] = changes[key];
      record(fw, key, changes[key], user);
    });
    // 转交了 refresh token 且开着发布开关：评测服务保存后台评测登录
    if (!fw.settings.publish) fw.loginSaved = false;
    else if (headerOf(init, 'X-Refresh-Token')) fw.loginSaved = true;
    fw.grantedAt = now();
    return json(200, view(fw));
  });

  on('GET', '/projects/([^/]+)/flywheel/runs', ({ m, query }) => {
    const blocked = guard(false);
    if (blocked) return blocked;
    const filter = query.get('filter') || 'all';
    if (!(filter in FILTERS)) return err(400, 'invalid_argument', '运行记录筛选条件无效');
    const fw = stateOf(m[1]);
    advance(fw);
    const group = FILTERS[filter];
    return json(200, page(fw.runs.filter((run) => !group || group.includes(run.status)).map(pub), query));
  });

  on('GET', '/projects/([^/]+)/flywheel/audit', ({ m, query }) => {
    const blocked = guard(false);
    if (blocked) return blocked;
    return json(200, page(stateOf(m[1]).audit, query));
  });

  const find = (pid, id) => {
    const fw = stateOf(pid);
    advance(fw);
    return [fw, fw.runs.find((run) => run.id === decodeURIComponent(id))];
  };

  on('GET', '/projects/([^/]+)/flywheel/runs/([^/]+)', ({ m }) => {
    const blocked = guard(false);
    if (blocked) return blocked;
    const [, run] = find(m[1], m[2]);
    if (!run) return err(404, 'not_found', '运行记录不存在');
    return json(200, {
      ...pub(run), history: historyOf(run), checks: run.changed != null ? checksOf(run) : null,
      candidate: null, report: null, hub_error: null, conversation_id: null, error: run.error || null,
    });
  });

  on('GET', '/projects/([^/]+)/flywheel/runs/([^/]+)/changes', ({ m }) => {
    const blocked = guard(false);
    if (blocked) return blocked;
    const [, run] = find(m[1], m[2]);
    if (!run) return err(404, 'not_found', '运行记录不存在');
    return json(200, changesOf(run));
  });

  const ACTIONS = {
    optimize(fw, run, body, t) {
      if (!canResume(run)) return '本次建议已处理，可在运行记录中查看结果';
      if (!isFeedback(run) && run.release_id !== fw.pkg.release_id) {
        return `该版本与当前建模项目版本 ${fw.pkg.version || '未发布'} 不一致，不能评测优化`;
      }
      if (fw.runs.some((item) => ACTIVE.includes(item.status))) return '项目正在执行任务，请等待本次任务完成';
      if (fw.runs.some((item) => item.status === 'waiting_publish' && item.id !== run.id)) return '请先处理当前待发布候选内容';
      fw.iter += 1;
      Object.assign(run, {
        status: 'optimizing', step: 0, reason: null, mode: 'manual', snapshot: { iteration: fw.iter },
        base_release_id: fw.pkg.release_id, changed: null, earlier_changes: false, _t: t,
      });
      return null;
    },
    publish(fw, run, body, t) {
      if (!(run.status === 'waiting_publish' || (run.status === 'blocked' && RETRY.includes(run.reason)))) return '该运行没有待发布的内容';
      if (fw.runs.some((item) => ACTIVE.includes(item.status))) return '项目正在执行任务，请等待本次任务完成';
      Object.assign(run, { status: 'publishing', step: 0, reason: null, publish_mode: 'manual', _t: t });
      return null;
    },
    evaluate(fw, run, body, t) {
      if (!canEvaluate(run)) return '该运行没有待评测的版本';
      if (run.graph_confirmation && !body.replace_graph) return '需要确认删除评测图范围内的旧数据';
      Object.assign(run, { status: 'evaluating', reason: null, evaluation: { status: 'running', mode: 'manual' }, _t: t });
      return null;
    },
    'skip-evaluation': (fw, run) => {
      if (!canEvaluate(run) && run.status !== 'evaluating') return '该运行没有待评测的版本';
      Object.assign(run, { status: 'published', reason: null, evaluation: { status: 'skipped' } });
      delete run._t;
      return null;
    },
    recheck: (fw, run) => {
      if (!(run.status === 'blocked' && RECHECK.includes(run.reason))) return '该运行不需要重新检查';
      Object.assign(run, { status: 'waiting_publish', reason: null, human_hold: true });
      return null;
    },
    ignore: (fw, run) => {
      if (!canIgnore(run)) return '该运行不能忽略';
      run.status = 'ignored';
      return null;
    },
    stop: (fw, run, body, t, user) => {
      if (run.status !== 'optimizing') return '该运行不在优化中';
      Object.assign(run, { status: 'interrupted', reason: 'optimization_interrupted', step: 0 });
      delete run._t;
      if (fw.settings.optimize) {
        fw.settings.optimize = false;
        record(fw, 'optimize', false, user);
      }
      return null;
    },
    rollback: (fw, run, body, t, user) => {
      if (!['workspace', 'hub'].includes(body.scope)) return err(400, 'invalid_argument', '回退范围无效');
      if (!run.snapshot) return '该运行没有可回退的执行前快照';
      if (!ROLLBACKABLE.includes(run.status)) return '该运行不能回退';
      if (fw.runs.some((item) => ACTIVE.includes(item.status))) return '请等待当前运行结束后再回退';
      run.status = 'rolledback';
      delete run._t;
      fw.runs.forEach((item) => { if (item.status === 'waiting_publish') item.status = 'ignored'; });
      pause(fw, user);
      fw.seq += 1;
      const hub = body.scope === 'hub';
      fw.runs.unshift({
        id: runId(fw.seq), kind: 'rollback', origin: 'rollback', source_id: run.id, trigger: null, eval_run_id: null,
        version: run.version, release_id: run.release_id, content_version: run.version, snapshot: run.snapshot,
        changed: run.changed ?? 1, score: null, cases: null, suggestions: null, reason: null, human_hold: true,
        mode: 'manual', received_at: t, status: hub ? 'publishing' : 'restored', step: 0, evaluation: null,
        base_release_id: fw.pkg.release_id, earlier_changes: false, ...(hub ? { _t: t } : {}),
      });
      return null;
    },
  };

  on('POST', '/projects/([^/]+)/flywheel/runs/([^/]+)/([a-z-]+)', ({ m, body, user }) => {
    const blocked = guard(false);
    if (blocked) return blocked;
    const [fw, run] = find(m[1], m[2]);
    const handler = ACTIONS[m[3]];
    if (!run || !handler) return (run ? err(404, 'not_found', `不支持的操作：${m[3]}`) : err(404, 'flywheel_run_not_found', '运行记录不存在'));
    if (readonly) return err(409, 'project_archived', '项目已归档，数据飞轮只能查看');
    const result = handler(fw, run, body || {}, now(), user);
    if (result instanceof Response) return result;
    if (typeof result === 'string') return err(409, 'flywheel_blocked', result);
    return json(200, view(fw));
  });
}

function historyOf(run) {
  const t = run.received_at;
  const items = [{
    at: t, result: 'done',
    title: run.kind === 'rollback' ? '发起回退' : isFeedback(run) ? '收到用户反馈' : '收到评测建议',
    text: run.kind === 'rollback' ? `恢复 ${run.source_id} 执行前的内容`
      : isFeedback(run) ? `${run.cases?.total ?? 0} 个反馈问题` : `严格通过率 ${Math.round(run.score ?? 0)}%`,
  }];
  const add = (offset, title, text, result = 'done') => items.push({ at: t + offset, title, text, result });
  if (run.status === 'passed') add(5, '评测集全部通过', '无需优化');
  if (run.status === 'ignored') add(600, '已忽略', '未修改知识包，记录保留');
  if (run.status === 'expired') add(5, '已过期', '项目已换到新版本，本条不再优化', 'wait');
  if (run.snapshot) add(20, '保存快照', `iteration-${String(run.snapshot.iteration).padStart(3, '0')}`);
  if (run.status === 'optimizing') add(30, '修订知识内容', '建模助手正在按优化建议修改', 'active');
  if (run.status === 'interrupted') add(200, '已中止', '执行前内容已恢复', 'fail');
  if (run.changed != null && run.kind !== 'rollback') add(360, '优化完成', `${run.changed} 份文档已更新，质量与发布检查通过`);
  if (run.status === 'waiting_publish') add(361, '等待确认发布', '确认后发布到 Hub', 'wait');
  if (run.status === 'restored') add(30, '仅恢复建模内容', 'Hub 版本保持不变');
  if (run.status === 'publishing') add(400, '发布到 Hub', '正在上传知识包', 'active');
  if (run.published_release_id) add(480, 'Hub 已回执', `v${run.target_version}`);
  if (run.status === 'waiting_evaluation') add(481, '等待确认评测', '确认后用上一轮的评测集评测新版本', 'wait');
  if (run.status === 'evaluating') add(500, '新版本评测中', '评测服务正在执行用例', 'active');
  if (run.evaluation?.status === 'done') {
    add(900, '评测结果已关联', run.evaluation.passed ? '全部用例通过' : `严格通过率 ${Math.round(run.evaluation.score)}%`);
  }
  if (run.evaluation?.status === 'skipped') add(901, '已跳过新版本评测', '本轮结束');
  if (run.status === 'rolledback') add(1200, '已回退', '执行前内容已恢复');
  return items;
}

function checksOf(run) {
  return {
    checked_at: run.optimized_at || run.received_at + 360,
    items: [
      { key: 'structure', label: '结构校验', message: '对象、规则、操作的引用完整', passed: true, details: [] },
      { key: 'semantic', label: '语义检查', message: '未发现新的冲突', passed: true, details: [] },
      { key: 'publish', label: '发布条件', message: '清单与版本号一致', passed: true, details: [] },
    ],
  };
}

function changesOf(run) {
  if (!run.snapshot) return { total: 0, truncated: false, files: [] };
  const count = run.status === 'optimizing' || run.changed == null ? 1 : Math.min(DIFF.length, run.changed);
  const swap = { added: 'removed', removed: 'added' };
  const files = DIFF.slice(0, count).map(([path, rows]) => {
    const lines = rows.map(([kind, text]) => ({ kind: run.kind === 'rollback' ? swap[kind] || kind : kind, text }));
    return {
      path, lines, truncated: false,
      added: lines.filter((line) => line.kind === 'added').length,
      removed: lines.filter((line) => line.kind === 'removed').length,
    };
  });
  return { total: files.length, truncated: false, files };
}
