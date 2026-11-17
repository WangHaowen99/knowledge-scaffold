// mock：运维总览与可观测（O-01～O-12，含没挂界面的链路检索 O-05）。页面：ops、ops-obs
// 运维面路径登记成 '/ops/...'（客户端按 ops_base_url 拼，mock 只看路径）；角色用 ctx.forbid 把关：读 ops、admin，写只有 ops。
// 地址参数（?mock=，逗号分隔）：
//   ops:p1  —— O-01 按 B2 后端的口径返回：calls_today、autobuild.limit、accounts、avg_cost.amount 为 null，
//              费用是美元，告警与运行中的自主构建为空（contract-changes「B2 · 非项目级路由」第 1、16 条）。
//   obs:p1  —— 可观测按 B3b 后端的口径返回：指标卡是数加单位、合入恒为 0 带 note、归因的 reruns 与
//              first_accept_pct 为 null、日志有 DEBUG 与 message；告警规则与导出器（O-08～O-12）后端没做，返回 404。
import { matchLog, parseLabelsQuery, selectTraces } from '../../pages/ops/obsModel.js';
import * as D from './ops-obs-data.js';

const READ = ['ops', 'admin'];
const WRITE = ['ops'];
const LEVELS = ['critical', 'warning', 'info'];

export function register(ctx) {
  const { on, json, err, forbid, sc } = ctx;
  const p1Ops = sc.ops === 'p1';
  const p1Obs = sc.obs === 'p1';
  // 本服务端的规则与导出器状态（StrictMode 下两个服务端各一份）
  const st = { rules: D.alertRules(), exporters: D.exporters(), seq: 0 };
  const firing = () => st.rules.filter((r) => r.enabled && r.state === 'firing').length;
  const notDone = () => err(404, 'not_found', '这个接口后端还没提供');

  // O-01
  on('GET', '/ops/overview', ({ user, query }) => forbid(user, READ) || json(200, D.overview({ range: query.get('range') || '14d', p1: p1Ops })));

  // O-02、O-03
  on('GET', '/ops/metrics', ({ user, query }) => {
    const range = query.get('range') || '24h';
    if (!['1h', '24h', '7d'].includes(range)) return err(400, 'invalid_argument', 'range 只能是 1h、24h、7d', { fields: { range: 'enum' } });
    return forbid(user, READ) || json(200, D.metrics({ range, labels: query.get('labels') || '', p1: p1Obs }));
  });
  on('GET', '/ops/attribution', ({ user, query }) => {
    const by = query.get('by') || 'pack';
    if (!['pack', 'node'].includes(by)) return err(400, 'invalid_argument', 'by 只能是 pack 或 node', { fields: { by: 'enum' } });
    const wanted = parseLabelsQuery(query.get('labels') || '');
    const res = D.attribution({ by, p1: p1Obs });
    // 带了包或节点标签时只留对得上的行
    const key = by === 'pack' ? wanted.pack : wanted.node;
    if (key) res.items = res.items.filter((it) => key.startsWith(String(it.key).split('@')[0]));
    return forbid(user, READ) || json(200, res);
  });

  // O-04、O-05
  on('GET', '/ops/traces/([^/]+)', ({ user, m }) => {
    const denied = forbid(user, READ);
    if (denied) return denied;
    const t = D.traceDetail(decodeURIComponent(m[1]));
    return t ? json(200, t) : err(404, 'trace_not_found', '链路不存在或已过保留期');
  });
  on('GET', '/ops/traces', ({ user, query }) => {
    const denied = forbid(user, READ);
    if (denied) return denied;
    const items = selectTraces(D.traceList(), {
      labels: query.get('labels') || '', status: query.get('status') || '', range: query.get('range') || '24h',
    });
    const limit = Math.min(200, Number(query.get('limit')) || 50);
    const start = Number(query.get('cursor')) || 0;
    const page = items.slice(start, start + limit);
    return json(200, { items: page, next_cursor: start + limit < items.length ? String(start + limit) : null });
  });

  // O-06、O-07
  const logFilter = (query) => ({ level: query.get('level') || '', q: query.get('q') || '', labels: query.get('labels') || '' });
  on('GET', '/ops/logs', ({ user, query }) => {
    const denied = forbid(user, READ);
    if (denied) return denied;
    const f = logFilter(query);
    const bad = f.level.split(',').filter(Boolean).find((x) => !['DEBUG', 'INFO', 'WARN', 'ERROR'].includes(x.toUpperCase()));
    if (bad) return err(400, 'invalid_argument', 'level 只能是 INFO、WARN、ERROR', { fields: { level: 'enum' } });
    const items = D.logs({ p1: p1Obs }).filter((e) => matchLog(e, f));
    return json(200, { items: items.slice(0, Number(query.get('limit')) || 100), next_cursor: null });
  });
  on('GET', '/ops/logs/stream', ({ user, query }) => {
    const denied = forbid(user, READ);
    if (denied) return denied;
    const f = logFilter(query);
    const enc = new TextEncoder();
    let timer;
    let n = 0;
    let seq = 0;
    const body = new ReadableStream({
      start(ctrl) {
        ctrl.enqueue(enc.encode('retry: 3000\n\n'));
        timer = setInterval(() => {
          const entry = D.liveLog(n);
          n += 1;
          try {
            if (matchLog(entry, f)) {
              seq += 1;
              ctrl.enqueue(enc.encode(`data: ${JSON.stringify({ type: 'log', seq, data: entry })}\n\n`));
            } else ctrl.enqueue(enc.encode(': keep-alive\n\n'));
          } catch { clearInterval(timer); }
        }, 1600);
      },
      cancel() { clearInterval(timer); },
    });
    return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  });

  // O-08～O-10
  const ruleProblems = (b, partial) => {
    const fields = {};
    if (!partial || 'name' in b) if (!String(b.name || '').trim()) fields.name = 'required';
    if (!partial || 'condition' in b) if (!String(b.condition || '').trim()) fields.condition = 'required';
    if (!partial || 'level' in b) if (!LEVELS.includes(b.level)) fields.level = 'enum';
    return Object.keys(fields).length ? err(400, 'invalid_argument', '参数不合法', { fields }) : null;
  };
  on('GET', '/ops/alerts/rules', ({ user }) => forbid(user, READ) || (p1Obs ? notDone() : json(200, { items: st.rules, firing: firing() })));
  on('POST', '/ops/alerts/rules', ({ user, body }) => {
    const denied = forbid(user, WRITE) || (p1Obs ? notDone() : null) || ruleProblems(body, false);
    if (denied) return denied;
    st.seq += 1;
    const rule = {
      id: `rule-${st.seq}`, name: body.name.trim(), condition: body.condition.trim(), window: (body.window || '').trim(), level: body.level,
      state: 'ok', enabled: body.enabled !== false, source: 'kernel', runbook_md: body.runbook_md || '', auto_action: body.auto_action || null,
    };
    st.rules.push(rule);
    return json(201, rule);
  });
  on('PUT', '/ops/alerts/rules/([^/]+)', ({ user, m, body }) => {
    const denied = forbid(user, WRITE) || (p1Obs ? notDone() : null);
    if (denied) return denied;
    const rule = st.rules.find((r) => r.id === decodeURIComponent(m[1]));
    if (!rule) return err(404, 'not_found', '规则不存在');
    if (rule.source !== 'kernel' && Object.keys(body).some((k) => k !== 'enabled')) {
      return err(403, 'forbidden', '包声明的规则只能停用或启用', { reason: 'pack_rule' });
    }
    const bad = ruleProblems(body, true);
    if (bad) return bad;
    ['name', 'condition', 'window', 'level', 'runbook_md', 'auto_action', 'enabled'].forEach((k) => { if (k in body) rule[k] = body[k]; });
    if (!rule.enabled) rule.state = 'ok';
    else if (['node-fail-rate', 'disk-free'].includes(rule.id)) rule.state = 'firing';
    return json(200, rule);
  });
  on('DELETE', '/ops/alerts/rules/([^/]+)', ({ user, m }) => {
    const denied = forbid(user, WRITE) || (p1Obs ? notDone() : null);
    if (denied) return denied;
    const rule = st.rules.find((r) => r.id === decodeURIComponent(m[1]));
    if (!rule) return err(404, 'not_found', '规则不存在');
    if (rule.source !== 'kernel') return err(403, 'forbidden', '包声明的规则不能删除，只能停用', { reason: 'pack_rule' });
    st.rules = st.rules.filter((r) => r !== rule);
    return new Response(null, { status: 204 });
  });

  // O-11、O-12
  on('GET', '/ops/exporters', ({ user }) => forbid(user, READ) || (p1Obs ? notDone() : json(200, { items: st.exporters })));
  on('PUT', '/ops/exporters', ({ user, body }) => {
    const denied = forbid(user, WRITE) || (p1Obs ? notDone() : null);
    if (denied) return denied;
    const fields = {};
    (body.items || []).forEach((it) => {
      const cur = st.exporters.find((e) => e.id === it.id);
      if (!cur) { fields[it.id] = 'unknown'; return; }
      const target = 'target' in it ? it.target : cur.target;
      if (it.enabled && !String(target || '').trim()) fields[`${it.id}.target`] = 'required';
    });
    if (Object.keys(fields).length) return err(400, 'invalid_argument', '参数不合法', { fields });
    (body.items || []).forEach((it) => {
      const cur = st.exporters.find((e) => e.id === it.id);
      Object.assign(cur, {
        enabled: !!it.enabled,
        ...('target' in it ? { target: it.target || null } : null),
        ...('signals' in it ? { signals: it.signals || [] } : null),
      });
      cur.configured = !!cur.target;
    });
    return json(200, { items: st.exporters, effective: 'rolling_restart' });
  });
}
