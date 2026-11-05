// mock · 3.20 发布（PB-01～PB-07），以及门禁「求解契约回执」用到的验证回执（SV-04，代码生成分组没登记时由这里兜底）
//
// 门禁链（主示例项目）：数据绑定（3 个对象对不上，可降级）→ 求解契约回执（r7 通过）→ 包结构 → 合规检查（零售行业包，
// 还没运行，挡住发布；点「去运行」经 FL-05 在对话里开始后视为已运行）→ 证据出处检查（通过，2 个提醒）。
// 改数据来源（PB-02）会提交一个新版本（经 routes-versions 的 commitWrite）；发布成功 4 秒后模拟评测服务拉取一次（EP-19）。
//
// mock 参数：publish:degrade（合规检查已运行，只剩数据绑定，可以降级发布）；publish:ready（全部通过）；publish:empty（还没有发布记录）
import { isLocked } from '../../state/readonly.js';
import * as P from './publish-data.js';
import { entryRef } from './scenario.js';

const zip = () => new Blob(['PK\u0005\u0006'.padEnd(22, '\u0000')]);
const semver = (v) => String(v).split('.').map(Number);
const newer = (a, b) => { const [x, y] = [semver(a), semver(b)]; for (let i = 0; i < 3; i += 1) if (x[i] !== y[i]) return x[i] > y[i]; return false; };

// 发布版本号 0.{n}.0 必须大于已有的全部发布（contract-changes 顶部主控那条）；不满足时取已有最大 minor 下的下一个 patch
export function nextReleaseVersion(n, existing) {
  const want = `0.${n}.0`;
  if (existing.every((v) => newer(want, v))) return want;
  const max = existing.reduce((m, v) => (newer(v, m) ? v : m), '0.0.0');
  const [, minor, patch] = semver(max);
  return `0.${minor}.${patch + 1}`;
}

const DEFAULT_RECEIPT = {
  id: 'rcp-0007', receipt_id: 'rcp-0007', revision: 'r7', revision_no: 7, release_version: null, solver_keys: ['restock_plan'],
  status: 'passed', authoritative: true, contract_digest: 'c7e1a90b', request_digest: '3f02d6aa', result_digest: '9b1c44e0',
  candidate_count: 12, error_code: null, message: null, completed_at: null, received_at: null, by_token: '评测服务',
};

export function register(ctx) {
  const { on, json, err, db, sc, F, cardOf, emitProj, scopeOf, convsOf, hub, convKey, uid } = ctx;
  const ps = db.pstore || (db.pstore = { issues: new Map(), ran: new Set() });
  const isMain = (pid) => pid === F.MAIN_PID;
  if (sc.publish === 'empty') { db.releases = db.releases || new Map(); db.releases.set(F.MAIN_PID, []); }

  const issuesOf = (pid) => {
    if (!ps.issues.has(pid)) ps.issues.set(pid, isMain(pid) && sc.publish !== 'ready' ? P.bindingIssues().map((x, i) => ({ ...x, revision: `rb${i + 1}` })) : []);
    return ps.issues.get(pid);
  };
  const openIssues = (pid) => issuesOf(pid).filter((x) => !x.fixed);
  // 「去运行」经 FL-05 发到对话里（index.js 记一条「开始「合规检查」」）就算运行过
  const complianceRan = (pid) => sc.publish === 'ready' || sc.publish === 'degrade' || ps.ran.has(pid)
    || convsOf(pid).some((c) => hub.history(convKey(pid, c.id)).some((e) => e.type === 'message.user' && /开始「合规检查」/.test(e.data?.text || '')));

  function receipts() {
    const at = new Date(Date.now() - 26 * 3600 * 1000).toISOString();
    const r = { ...DEFAULT_RECEIPT, completed_at: at, received_at: at };
    return { revision: 'r7', revision_no: 7, latest: r, items: [r], gate: { status: 'pass', finding: null } };
  }
  // 求解契约回执的结论：先问 SV-04（代码生成分组登记了就用它的），拿不到用本文件的
  async function solverReceipts(pid, init) {
    if (ctx.self) {
      try {
        const res = await ctx.self(`/api/v1/projects/${encodeURIComponent(pid)}/solver/receipts`, { method: 'GET', headers: init?.headers || {} });
        if (res.ok) return res.json();
      } catch { /* 用缺省 */ }
    }
    return receipts();
  }

  async function check(pid, target, init) {
    const card = cardOf(pid);
    const issues = openIssues(pid);
    const fmt = P.formatOf(pid, target.n);
    const gates = [];
    const g = (o) => gates.push({ order: gates.length + 1, findings: [], action: null, degradable: false, blocking: false, ...o });
    g({
      id: 'data_binding', title: '数据绑定检查', source: { kind: 'platform', label: '平台检查' },
      status: issues.length ? 'fail' : 'pass', blocking: issues.length > 0, degradable: true,
      summary: issues.length ? `${issues.length} 个对象和数据表对不上` : `${isMain(pid) ? P.OBJECT_COUNT : 8} 个对象都对得上数据表`,
      findings: issues.map((x) => ({ code: x.kind, level: 'error', message: x.message, target: x.object })),
    });
    if (fmt.format === 'solver') {
      const sv = await solverReceipts(pid, init);
      const s = sv.gate?.status || 'required';
      g({
        id: 'solver_receipt', title: '求解契约回执', source: { kind: 'platform', label: '平台检查' },
        status: s === 'pass' ? 'pass' : s === 'required' ? 'not_run' : 'fail', blocking: s !== 'pass',
        summary: {
          pass: `r${sv.revision_no ?? ''} 已通过 · 评测服务回传`, required: '还没有验证回执 · 挡住发布',
          stale: '契约改过，回执过期 · 挡住发布', failed: `r${sv.revision_no ?? ''} 验证没通过`,
        }[s] || '还没有验证回执',
        findings: sv.gate?.finding ? [sv.gate.finding] : [],
      });
    }
    g({ id: 'package_structure', title: '包结构检查', source: { kind: 'platform', label: '平台检查' }, status: 'pass', summary: '脚本绑定、入参、文档元数据都通过' });
    if (isMain(pid)) {
      const ran = complianceRan(pid);
      g({
        id: 'retail.compliance', title: '合规检查', source: { kind: 'pack', label: '零售行业包', pack: 'industry-retail' },
        status: ran ? 'pass' : 'not_run', blocking: !ran, summary: ran ? '补货单格式与审批留痕都符合' : '还没运行 · 挡住发布',
        action: ran ? null : { type: 'start_node', node_id: 'retail.compliance' },
      });
      g({
        id: 'rev.anchor_coverage', title: '证据出处检查', source: { kind: 'pack', label: '系统逆向建模能力', pack: 'rev' },
        status: 'warn', summary: '出处都能找到 · 2 个提醒',
        findings: [
          { code: 'property_unevidenced', level: 'warning', message: '属性「生效日期」没有出处', target: entryRef('display_qty_std') },
          { code: 'property_unevidenced', level: 'warning', message: '属性「检查方法」只有专家确认', target: entryRef('display_qty_std') },
        ],
      });
    }
    const rel = P.releasesOf(db, pid, card);
    return {
      commit: target.commit, version_label: target.version_label, package_name: card?.package_name || '', ...fmt, gates,
      blocking_count: gates.filter((x) => x.blocking).length,
      binding: {
        issues, warnings: [], tables: P.PACKAGE_TABLES, table_count: P.PACKAGE_TABLES.length,
        objects: isMain(pid) ? P.OBJECT_COUNT : 8, bound_objects: (isMain(pid) ? P.OBJECT_COUNT : 8) - issues.length,
        table_resources: isMain(pid) ? P.UNMARKED_TABLE_RESOURCES : [], truncated: false,
      },
      degradable: issues.length > 0,
      uncommitted: { count: 0, paths: [] },
      last_release: rel[0] || null,
      checked_at: new Date().toISOString(),
      duration_ms: 2600 + Math.round(Math.random() * 900),
    };
  }

  async function targetOf(pid, commit, init) {
    const list = await db.vstore.history(pid, init);
    if (!list) return { list: null, target: null };
    return { list, target: commit ? db.vstore.resolve(list, commit) : list[list.length - 1] };
  }

  // ---------- PB-01 发布检查 ----------
  on('GET', '/projects/([^/]+)/publish/check', async ({ m, query, init }) => {
    const { list, target } = await targetOf(m[1], query.get('commit'), init);
    if (!list) return err(404, 'project_not_found', '项目不存在');
    if (!target) return err(400, 'invalid_commit', '版本号不合法', null);
    return json(200, await check(m[1], target, init));
  });

  // ---------- PB-02 设置对象数据来源 ----------
  on('PUT', '/projects/([^/]+)/objects/([^/]+)/data-source', async ({ m, body, user, init }) => {
    const pid = m[1];
    const key = decodeURIComponent(m[2]);
    let ref;
    try { ref = entryRef(key); } catch { return err(404, 'not_found', '对象不存在'); }
    if (ref.kind !== 'object') return err(404, 'not_found', '对象不存在');
    if (isLocked(scopeOf(pid), ref)) return err(423, 'write_scope_locked', '在助手写入范围内', scopeOf(pid));
    const kind = body.kind;
    let value = String(body.value || '').trim();
    if (kind === 'anchor') return err(404, 'anchor_not_found', '证据快照里找不到这个锚点', { anchor: value });
    if (kind === 'table') {
      const hit = P.PACKAGE_TABLES.find((t) => t.toLowerCase() === value.toLowerCase());
      if (!hit) return err(400, 'invalid_argument', '表名不在 Schema 资料和快照里', { fields: { value: 'unknown_table' } });
      value = hit;
    } else if (kind !== 'none') return err(400, 'invalid_argument', 'kind 只能是 table、anchor、none', { fields: { kind: 'invalid' } });
    const issue = issuesOf(pid).find((x) => x.object.key === key && !x.fixed);
    if (issue && body.base_revision && body.base_revision !== issue.revision) {
      return err(409, 'revision_conflict', '对象已被修改', { target: ref.id, expected: body.base_revision, actual: issue.revision, kind: 'entry' });
    }
    if (issue) issue.fixed = { kind, value: kind === 'none' ? null : value };
    const r = await db.vstore.commitWrite(pid, {
      source: 'web', by: user?.username || 'zhangsan', entries: [ref],
      summary: `网页编辑：${ref.name} 的数据来源改为${kind === 'none' ? '「无」' : ` ${value}`}`,
      changes: [{ target: ref, change: 'mod', detail: '修改 · 1 处', review: null }],
    }, init);
    return json(200, { entry: ref, revision: `rb${uid()}`, commit: r.commit, version_label: r.version_label, binding: null });
  });

  // ---------- PB-03 发布 ----------
  on('POST', '/projects/([^/]+)/releases', async ({ m, body, user, init }) => {
    const pid = m[1];
    const card = cardOf(pid);
    if (card?.archived) return err(409, 'project_archived', '项目已归档');
    const { list, target } = await targetOf(pid, body.commit, init);
    if (!list) return err(404, 'project_not_found', '项目不存在');
    if (!target) return err(400, 'invalid_commit', '版本号不合法', null);
    const c = await check(pid, target, init);
    const failing = c.gates.filter((x) => x.blocking && !(body.degrade && x.degradable));
    if (failing.length) return err(422, 'gate_blocked', '门禁链有未通过项', { gates: failing });
    const rel = P.releasesOf(db, pid, card);
    const same = rel.find((r) => r.commit === target.commit);
    if (same) return json(201, same);
    const degraded = !!body.degrade && c.binding.issues.length > 0;
    const rec = P.releaseRecord(pid, target, {
      at: new Date().toISOString(), by: user?.username || 'zhangsan', degraded, unbound: degraded ? c.binding.issues.map((x) => x.object.name) : [],
    });
    const version = nextReleaseVersion(target.n, rel.map((r) => r.version));
    if (version !== rec.version) Object.assign(rec, { version, tag: `release/${version}` });
    rel.unshift(rec);
    emitProj(pid, 'release.updated', { release: rec });
    const pull = setTimeout(() => {
      rec.pulls = [...rec.pulls, { at: new Date().toISOString(), by_token: '评测服务', variant: 'standard' }];
      emitProj(pid, 'release.updated', { release: rec });
    }, 4000);
    pull.unref?.(); // 单测（node）里不拖住进程
    return json(201, rec);
  });

  // ---------- PB-04～PB-07 ----------
  const findRelease = (pid, v) => P.releasesOf(db, pid, cardOf(pid)).find((r) => r.version === v || r.version_label === v);
  on('GET', '/projects/([^/]+)/releases', ({ m }) => json(200, { items: P.releasesOf(db, m[1], cardOf(m[1])), next_cursor: null }));
  on('GET', '/projects/([^/]+)/releases/([^/]+)/manifest', async ({ m, init }) => {
    const rec = findRelease(m[1], decodeURIComponent(m[2]));
    if (!rec) return err(404, 'release_not_found', '发布记录不存在');
    const list = await db.vstore.history(m[1], init);
    return json(200, P.manifestOf(m[1], rec, list || [], cardOf(m[1])?.package_name || ''));
  });
  on('GET', '/projects/([^/]+)/releases/([^/]+)/download', ({ m, query }) => {
    const rec = findRelease(m[1], decodeURIComponent(m[2]));
    if (!rec) return err(404, 'release_not_found', '发布记录不存在');
    const variant = query.get('variant') || 'standard';
    if (!['standard', 'graph'].includes(variant)) return err(400, 'invalid_argument', '不支持这个变体', { fields: { variant: 'unsupported' } });
    const name = `${cardOf(m[1])?.package_name || 'package'}-${rec.version}${variant === 'graph' ? '-graph' : ''}.zip`;
    return new Response(zip(), {
      status: 200,
      headers: {
        'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="${encodeURIComponent(name)}"`,
        'X-Package-Version': rec.version, 'X-Package-Commit': rec.commit, 'X-Package-Format': rec.format, 'X-Package-Variant': variant,
      },
    });
  });
  on('GET', '/projects/([^/]+)/releases/([^/]+)', ({ m }) => {
    const rec = findRelease(m[1], decodeURIComponent(m[2]));
    return rec ? json(200, rec) : err(404, 'release_not_found', '发布记录不存在');
  });

  // ---------- SV-04 验证回执（兜底） ----------
  on('GET', '/projects/([^/]+)/solver/receipts', () => json(200, receipts()));
}
