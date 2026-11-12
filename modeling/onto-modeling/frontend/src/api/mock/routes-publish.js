// mock · 3.20 发布（PB-01～PB-07），版本号按知识包 Hub 的规则分配（ADR-0098）
//
// 门禁链（主示例项目）：数据绑定（3 个对象对不上，可降级）→ 求解契约（静态检查通过，发布时要确认未验证）→ 包结构 → 合规检查（零售行业包，
// 还没运行，挡住发布；点「去运行」经 FL-05 在对话里开始后视为已运行）→ 证据出处检查（通过，2 个提醒）。
// 改数据来源（PB-02）会提交一个新版本（经 routes-versions 的 commitWrite）；发布成功 4 秒后模拟评测服务拉取一次（EP-19）。
//
// mock 参数：publish:degrade（合规检查已运行，只剩数据绑定，可以降级发布）；publish:ready（全部通过）；publish:empty（还没有发布记录）；
// publish:stages（资料分析与质量检查没做完，门禁链第一关「建模阶段」挡住，ADR-0098）。
// 知识包 Hub（HB-01～HB-07）：本人的包、版本、导入、项目所在包的版本（含别人发布的）与共享资料状态
import { isLocked } from '../../state/readonly.js';
import * as P from './publish-data.js';
import { entryRef } from './scenario.js';

const zip = () => new Blob(['PK\u0005\u0006'.padEnd(22, '\u0000')]);

// 发布版本号由知识包 Hub 分配（ADR-0098）：0.1.N 按包递增，旧版本地发布的 0.{n}.0 不参与编号
export function nextReleaseVersion(existing) {
  const used = existing.map((v) => /^0\.1\.(\d+)$/.exec(v)).filter(Boolean).map((m) => Number(m[1]));
  return `0.1.${Math.max(0, ...used) + 1}`;
}

export function register(ctx) {
  const { on, json, err, db, sc, F, cardOf, emitProj, scopeOf, convsOf, hub, convKey, uid } = ctx;
  const ps = db.pstore || (db.pstore = { issues: new Map(), ran: new Set() });
  const isMain = (pid) => pid === F.MAIN_PID;
  if (sc.publish === 'empty') { db.releases = db.releases || new Map(); db.releases.set(F.MAIN_PID, []); }

  // 发布前必经的四个建模阶段（与后端 publish/stages.py 同规则）
  const STAGES = [['research', '资料分析'], ['object', '对象提取'], ['operation', '操作提取'], ['validate', '质量检查']];
  const stagesOf = () => {
    const missing = sc.publish === 'stages' ? ['research', 'validate'] : [];
    const stages = STAGES.map(([key, title]) => ({ key, title, finished: !missing.includes(key) }));
    return { ready: !missing.length, stages, missing_stages: stages.filter((x) => !x.finished).map(({ key, title }) => ({ key, title })) };
  };

  // ---------- 知识包 Hub ----------
  const hubOf = (pid) => `pkg-${String(pid).replace(/[^a-z0-9]/gi, '').slice(-8)}`;
  // 项目所在包的版本：本项目发布的（发布记录里有 Hub 身份）加一条别人发布到同一个包的
  const hubReleasesOf = (pid) => {
    const own = P.releasesOf(db, pid, cardOf(pid)).map((r, i) => ({
      ontology_id: hubOf(pid), release_id: r.release_id || `rel-${pid}-${r.version}`, name: cardOf(pid)?.name || pid,
      version: r.version, source_service: 'modeling', created_at: r.at, committed_at: r.at, kind: 'release',
      package_digest: r.sha256 || `${i}`.padStart(64, 'a'),
    }));
    if (!isMain(pid) || !own.length) return own;
    const other = { ...own[own.length - 1], release_id: `rel-${pid}-peer`, version: '0.1.2', source_service: 'modeling',
      created_at: new Date(Date.parse(own[0].created_at) + 3600000).toISOString() };
    return [...own, other].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  };
  const page = (rows, query) => {
    const offset = Number(query.get('offset') || 0);
    const limit = Number(query.get('limit') || 20);
    const items = rows.slice(offset, offset + limit);
    return { items, releases: items, total: rows.length, offset, limit, next_offset: offset + items.length < rows.length ? offset + items.length : null };
  };
  on('GET', '/projects/([^/]+)/hub/releases', ({ m, query }) => json(200, page(hubReleasesOf(m[1]), query))); // HB-05
  on('GET', '/projects/([^/]+)/hub/resources/status', () => json(200, { pending: false, generation: 0, selected: [], shared: false })); // HB-07
  on('GET', '/projects/([^/]+)/hub/releases/([^/]+)/download', () => new Response(zip(), { status: 200, headers: { 'Content-Type': 'application/zip' } })); // HB-06
  const hubPackages = () => (db.cards || []).filter((c) => P.releasesOf(db, c.id, c).length).map((c) => {
    const [latest] = hubReleasesOf(c.id);
    return { ...latest };
  });
  on('GET', '/hub/packages', ({ query }) => json(200, page(hubPackages(), query))); // HB-01
  on('GET', '/hub/packages/([^/]+)/releases', ({ m, query }) => {
    const owner = (db.cards || []).find((c) => hubOf(c.id) === m[1]);
    return owner ? json(200, page(hubReleasesOf(owner.id), query)) : err(404, 'not_found', 'Hub 包不存在或当前用户无权访问');
  }); // HB-02
  on('POST', '/hub/imports', ({ body }) => {
    const source = (db.cards || []).find((c) => hubOf(c.id) === body.ontology_id);
    if (!source) return err(404, 'not_found', 'Hub 包不存在或当前用户无权访问');
    const created = { ...source, id: `p-${uid()}`, name: body.name, last_release: null };
    db.cards.unshift(created);
    return json(201, created);
  }); // HB-04

  const issuesOf = (pid) => {
    if (!ps.issues.has(pid)) ps.issues.set(pid, isMain(pid) && sc.publish !== 'ready' ? P.bindingIssues().map((x, i) => ({ ...x, revision: `rb${i + 1}` })) : []);
    return ps.issues.get(pid);
  };
  const openIssues = (pid) => issuesOf(pid).filter((x) => !x.fixed);
  // 「去运行」经 FL-05 发到对话里（index.js 记一条「开始「合规检查」」）就算运行过
  const complianceRan = (pid) => sc.publish === 'ready' || sc.publish === 'degrade' || ps.ran.has(pid)
    || convsOf(pid).some((c) => hub.history(convKey(pid, c.id)).some((e) => e.type === 'message.user' && /开始「合规检查」/.test(e.data?.text || '')));

  async function check(pid, target, init) {
    const card = cardOf(pid);
    const issues = openIssues(pid);
    const fmt = P.formatOf(pid, target.n);
    const gates = [];
    const g = (o) => gates.push({ order: gates.length + 1, findings: [], action: null, degradable: false, blocking: false, ...o });
    const stages = stagesOf();
    if (!stages.ready) {
      const text = `建模未完成，请先完成：${stages.missing_stages.map((x) => x.title).join('、')}`;
      g({
        id: 'modeling_stages', title: '建模阶段', source: { kind: 'platform', label: '平台检查' }, status: 'fail', blocking: true,
        summary: text, findings: [{ code: 'modeling_stages_incomplete', level: 'error', message: text }], missing_stages: stages.missing_stages,
      });
    }
    g({
      id: 'data_binding', title: '数据绑定检查', source: { kind: 'platform', label: '平台检查' },
      status: issues.length ? 'fail' : 'pass', blocking: issues.length > 0, degradable: true,
      summary: issues.length ? `${issues.length} 个对象和数据表对不上` : `${isMain(pid) ? P.OBJECT_COUNT : 8} 个对象都对得上数据表`,
      findings: issues.map((x) => ({ code: x.kind, level: 'error', message: x.message, target: x.object })),
    });
    if (fmt.format === 'solver') {
      g({
        id: 'solver_contract', title: '求解契约', source: { kind: 'platform', label: '平台检查' },
        status: 'pass', summary: '求解契约静态检查通过', requires_acknowledgement: true,
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
    const solver = fmt.format === 'solver'
      ? { enabled: true, revision: 'sha256:7c1e90b4d2a8f3561e0c', operations: [{ solver_key: 'restock_plan', enabled: true }], findings: [], blocking: 0,
        requires_validation_after_publish: true, truncated: false }
      : { enabled: false };
    return {
      commit: target.commit, version_label: target.version_label, package_name: card?.package_name || '', ...fmt, gates,
      solver_revision: solver.revision || '',
      solver,
      structure: { blocking: 0, findings: [], truncated: false, skipped: null },
      stages,
      last_publish: rel[0] ? { at: rel[0].at, by: rel[0].by, degraded: !!rel[0].gates?.degraded, unbound_count: rel[0].gates?.unbound_objects?.length || 0, version: rel[0].version } : null,
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
    if (c.gates.some((x) => x.requires_acknowledgement) && body.solver_unverified_acknowledged !== true) {
      return err(409, 'solver_validation_acknowledgement_required', '求解能力尚未经端到端验证，要确认后再发布');
    }
    const rel = P.releasesOf(db, pid, card);
    // 同一 request_id 重试得到同一个 Hub 版本；新的请求总是下一个版本
    const same = body.request_id && rel.find((r) => r.request_id === body.request_id);
    if (same) return json(201, same);
    const degraded = !!body.degrade && c.binding.issues.length > 0;
    const rec = P.releaseRecord(pid, target, {
      at: new Date().toISOString(), by: user?.username || 'zhangsan', degraded, unbound: degraded ? c.binding.issues.map((x) => x.object.name) : [],
    });
    const version = nextReleaseVersion(rel.map((r) => r.version));
    Object.assign(rec, { version, tag: `release/${version}`, request_id: body.request_id || null });
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
}
