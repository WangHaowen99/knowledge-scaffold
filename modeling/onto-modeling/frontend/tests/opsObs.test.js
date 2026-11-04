// 运维总览与可观测：页面纯逻辑（overviewModel、obsModel）与 mock（O-01～O-12，含链路检索 O-05）
import assert from 'node:assert/strict';
import test from 'node:test';
import { register } from '../src/api/mock/routes-ops-obs.js';
import * as D from '../src/api/mock/ops-obs-data.js';
import {
  addLabel, appendLog, attributionColumns, attributionRow, exporterProblems, exportersBody, exportersDirty, filterTraces, fmtCount, fmtSeconds,
  labelsQuery, latestTrace, levelCls, levelParam, logFields, matchLog, metricView, parseLabelInput, parseLabelsQuery,
  parseTraceQuery, ruleProblems, rulePatch, ruleView, rulesSub, runbookSummary, selectTraces, sortLogs, sparkPath, visibleMetricCards,
  traceSearchQuery, traceTags, waterfallRows, MAX_LOG_LINES,
} from '../src/pages/ops/obsModel.js';
import {
  adviceView, alertView, autobuildTodayView, barRects, callsText, fmtBytes, fmtDur, fmtMoney, nameMap, relTime,
  runRow, startupItemView, statCards, usageSummary,
} from '../src/pages/ops/overviewModel.js';

const MIN = 60000;

// ---------- 运维总览 ----------

test('金额：人民币取整、美元两位小数、金额为 null 显示「—」', () => {
  assert.equal(fmtMoney({ amount: 12846, currency: 'CNY' }), '¥ 12,846');
  assert.equal(fmtMoney({ amount: 3.27, currency: 'USD' }), '$ 3.27');
  assert.equal(fmtMoney({ amount: 1784.2, currency: 'USD' }), '$ 1,784');
  assert.equal(fmtMoney({ amount: null, currency: 'USD' }), '—');
  assert.equal(fmtMoney(null), '—');
});

test('大小、时长、相对时间', () => {
  assert.equal(fmtBytes(412e6), '412 MB');
  assert.equal(fmtBytes(1.2e9), '1.2 GB');
  assert.equal(fmtBytes(330e9), '330 GB');
  assert.equal(fmtBytes(null), '—');
  assert.equal(fmtDur(48 * MIN), '48 分');
  assert.equal(fmtDur(112 * MIN), '1 小时 52 分');
  assert.equal(fmtDur(120 * MIN), '2 小时');
  assert.equal(fmtDur(10000), '不到 1 分');
  assert.equal(fmtDur(null), '—');
  const now = new Date(2026, 10, 1, 15, 0).getTime();
  assert.equal(relTime(new Date(now - 20000).toISOString(), now), '刚刚');
  assert.equal(relTime(new Date(now - 6 * MIN).toISOString(), now), '6 分钟前');
  assert.equal(relTime(new Date(2026, 10, 1, 9, 12).toISOString(), now), '今天 09:12');
  assert.equal(relTime(new Date(2026, 9, 31, 18, 40).toISOString(), now), '昨天 18:40');
  assert.equal(relTime(new Date(2026, 9, 31, 18, 40).toISOString(), now, { short: true }), '昨天');
  assert.equal(relTime(new Date(2026, 9, 29, 16, 2).toISOString(), now), '10-29 16:02');
  assert.equal(relTime(new Date(2026, 9, 29, 16, 2).toISOString(), now, { short: true }), '10-29');
  assert.equal(relTime(null, now), '—');
});

test('按日用量：14 天合计、日均、峰值、较前期；成本与 tokens 分段', () => {
  const now = new Date(2026, 9, 31, 12, 0).getTime();
  const usage = D.usageDaily('14d', { now });
  const calls = usageSummary(usage, 'calls', '14d');
  assert.equal(calls.totalLabel, '14 天合计');
  assert.equal(calls.unit, '次调用');
  assert.equal(calls.total, '28,934');
  assert.equal(calls.avg, '2,067');
  assert.equal(calls.peakLabel, '峰值 · 10-28');
  assert.equal(calls.peak, '2,890');
  assert.equal(calls.vs, '+18%');
  assert.equal(calls.vsCls, 'up');
  assert.equal(calls.axis.length, 14);
  assert.equal(calls.axis[13], '31');
  const cost = usageSummary(usage, 'cost', '14d');
  assert.equal(cost.unit, '元');
  assert.equal(cost.total, '6,536');
  const tok = usageSummary(usage, 'tok', '14d');
  assert.equal(tok.unit, '万 tokens');
  assert.equal(tok.values[13], 79);
  assert.equal(tok.peak, '99');
  // 7 天：较前 7 天由前后两段现算
  const week = usageSummary(D.usageDaily('7d', { now }), 'calls', '7d');
  assert.equal(week.totalLabel, '7 天合计');
  assert.equal(week.vsLabel, '较前 7 天');
  assert.equal(week.total, '16,730');
  // 今天：只有一根柱，轴上写日期，较昨天
  const today = usageSummary(D.usageDaily('today', { now }), 'calls', 'today');
  assert.equal(today.totalLabel, '今天合计');
  assert.equal(today.vsLabel, '较昨天');
  assert.equal(today.vs, '+90.9%');
  assert.deepEqual(today.axis, ['10-31']);
  // 美元与较前期为 null
  const usd = usageSummary({ days: [{ date: '2026-10-31', calls: 3, cost: { amount: 1.5, currency: 'USD' }, output_tokens: 12000 }], vs_previous_pct: { calls: null } }, 'cost', 'today');
  assert.equal(usd.unit, '美元');
  assert.equal(usd.vs, '—');
  assert.equal(usd.vsCls, 'faint');
  const empty = usageSummary(null, 'calls');
  assert.equal(empty.avg, '—');
  assert.equal(empty.peak, '—');
});

test('柱状图几何：最高的顶到 h-14、最矮 2、柱宽占 62% 且不超过 32、高亮今天', () => {
  const r = barRects([10, 20, 0], { w: 300, h: 100, hi: 2 });
  assert.equal(r.length, 3);
  assert.equal(r[1].h, 86);
  assert.equal(r[1].y, 14);
  assert.equal(r[2].h, 2);
  assert.equal(r[0].w, 32);
  assert.equal(r[0].x, (100 - 32) / 2);
  assert.equal(r[2].hi, true);
  const narrow = barRects(Array(14).fill(1), { w: 720, h: 128 });
  assert.ok(Math.abs(narrow[0].w - (720 / 14) * 0.62) < 1e-9);
  assert.deepEqual(barRects([0, 0], { w: 100, h: 50 }).map((x) => x.h), [2, 2]);
  assert.deepEqual(barRects([]), []);
});

test('统计卡：B2 口径下 accounts、autobuild.limit 为 null 时不出说明行，费用是美元也不出错', () => {
  const s = statCards(D.overview({ p1: true }).stats);
  assert.equal(s.online.desc, null);
  assert.equal(s.autobuild.desc, null);
  assert.equal(s.cost.value, '$ 1,784');
  assert.match(s.cost.desc, /^本月 \$ /);
  assert.equal(s.disk.value, '18%');
  assert.equal(s.disk.small, '330 GB');
  assert.equal(s.disk.desc, '低于 20% 阈值');
  const d = statCards(D.overview().stats);
  assert.equal(d.online.desc, '共 23 个账号');
  assert.equal(d.sessions.pct, 58);
  assert.equal(d.sessions.desc, '排队 1');
  assert.equal(d.autobuild.desc, '平台上限 4 个');
  assert.equal(d.cost.value, '¥ 12,846');
  const none = statCards({});
  assert.equal(none.online.value, '—');
  assert.equal(none.cost.value, '—');
});

test('自主构建今日小结：平均成本为 null 显示「—」', () => {
  const v = autobuildTodayView(D.overview({ p1: true }).autobuild_today);
  assert.deepEqual(v.map((x) => x[1]), ['0', '—', '—']);
  assert.deepEqual(autobuildTodayView(D.overview().autobuild_today).map((x) => x[0]), ['今日已完成', '平均用时', '平均成本']);
  assert.deepEqual(autobuildTodayView(D.overview().autobuild_today).map((x) => x[1]), ['3', '1 小时 52 分', '¥ 41']);
});

test('用户活跃：今日调用为 null 或 0 显示「—」；所有者显示名从用户列表取，找不到用用户名', () => {
  assert.equal(callsText(null), '—');
  assert.equal(callsText(0), '—');
  assert.equal(callsText(412), '412');
  const name = nameMap([{ username: 'zhangsan', display_name: '张工' }]);
  assert.equal(name('zhangsan'), '张工');
  assert.equal(name('nobody'), 'nobody');
  assert.equal(name(null), '—');
  const ov = D.overview();
  const owners = new Set(ov.projects.map((p) => p.owner));
  const users = new Set(ov.users.map((u) => u.username));
  owners.forEach((o) => assert.ok(users.has(o), `项目所有者 ${o} 在用户活跃里`));
  assert.ok(D.overview({ p1: true }).users.every((u) => u.calls_today === null));
});

test('告警：触发中带持续时长或已自动熔断，已恢复带时刻；建议条第一句加粗', () => {
  const now = Date.parse('2026-11-01T07:00:00Z');
  const a = alertView({ name: '磁盘可用低于 20%', detail: '/data', level: 'warning', state: 'firing', since: new Date(now - 185 * MIN).toISOString() }, now);
  assert.equal(a.detail, '/data · 持续 3 小时');
  assert.deepEqual(a.chip, ['警告', 'amber']);
  const b = alertView({ name: 'x', detail: 'industry-retail@1.0.3', level: 'critical', state: 'firing', since: new Date(now).toISOString(), auto_action: 'breaker' }, now);
  assert.equal(b.detail, 'industry-retail@1.0.3 · 已自动熔断');
  assert.equal(b.dot, 'var(--red)');
  const c = alertView({ name: 'y', detail: '峰值排队 3', level: 'warning', state: 'resolved', resolved_at: '2026-11-01T02:42:00Z' }, now);
  assert.match(c.detail, /^峰值排队 3 · 已恢复 · \d\d:42$/);
  assert.deepEqual(c.chip, ['已恢复', 'gray']);
  const adv = adviceView({ level: 'warning', text: '磁盘可用 18%，低于 20% 阈值。建议清理旧备份。' });
  assert.equal(adv.bold, '磁盘可用 18%，低于 20% 阈值。');
  assert.equal(adv.rest, '建议清理旧备份。');
  assert.equal(adv.tone, 'amber');
  assert.equal(adv.icon, 'hdd');
});

test('运行中的 L4 与启动自检摘要', () => {
  const r = runRow(D.overview().autobuild_runs[0]);
  assert.equal(r.name, '共享单车故障诊断');
  assert.equal(r.elapsed, '38 分');
  assert.equal(r.slices, '2 / 5 段');
  assert.equal(r.pct, 58);
  assert.equal(r.cost, '¥ 25');
  assert.deepEqual(r.state, { k: 'running', label: '运行中' });
  assert.equal(runRow({ project: null, progress: { done: 1, total: 4 }, state: 'paused' }).pct, 25);
  const items = D.overview().startup.items.map(startupItemView);
  assert.deepEqual(items.map((x) => x.label), ['已清除', '已补提交', '正常', '正常']);
  assert.deepEqual(items.map((x) => x.cls), ['fix', 'fix', 'ok', 'ok']);
  assert.equal(startupItemView({ label: 'x', result: 'failed' }).tone, 'red');
});

// ---------- 可观测 ----------

test('公共标签：解析 键=值（中文叫法也认）、同键替换、拼查询参数', () => {
  assert.deepEqual(parseLabelInput('pack=core@1.4.2'), { key: 'pack', value: 'core@1.4.2' });
  assert.deepEqual(parseLabelInput('项目=retail-restock'), { key: 'project', value: 'retail-restock' });
  assert.ok(parseLabelInput('pack').error);
  assert.ok(parseLabelInput('color=red').error);
  assert.ok(parseLabelInput('user=zhangsan').error);
  assert.deepEqual(parseLabelInput('用户=zhangsan', { allowUser: true }), { key: 'user', value: 'zhangsan' });
  assert.ok(parseLabelInput('pack=a,b').error);
  let tags = addLabel([], { key: 'project', value: 'a' });
  tags = addLabel(tags, { key: 'pack', value: 'core@1.4.2' });
  tags = addLabel(tags, { key: 'project', value: 'b' });
  assert.deepEqual(tags, [{ key: 'project', value: 'b' }, { key: 'pack', value: 'core@1.4.2' }]);
  assert.equal(labelsQuery(tags), 'project=b,pack=core@1.4.2');
  assert.deepEqual(parseLabelsQuery('project=b,pack=core@1.4.2'), { project: 'b', pack: 'core@1.4.2' });
  assert.deepEqual(traceTags({ project: 'p', session: null, node: 'core.operation@1.4.2' }), [{ key: 'project', value: 'p' }, { key: 'node', value: 'core.operation@1.4.2' }]);
});

test('指标卡：设计稿形状与 B3b 后端形状都能显示，后端下发的底注（note）不画', () => {
  const byId = (cards) => Object.fromEntries(cards.map((c) => [c.id, metricView(c)]));
  const d = byId(D.metrics().cards);
  assert.equal(d.pool.big, '7 / 12');
  assert.equal(d.pool.small, '峰值排队 3');
  assert.equal(d.turn_duration.big, '42 秒');
  assert.equal(d.turn_duration.small, 'p95 3 分 10 秒');
  assert.equal(d.model_latency_p95.big, '18.4 秒');
  assert.equal(d.output_tokens.big, '79 万');
  assert.equal(d.model_fail_rate.big, '0.8%');
  assert.equal(d.model_fail_rate.color, '#DC2626');
  assert.equal(d.guard_denies.big, '23');
  assert.equal(d.guard_denies.small, '次');
  assert.equal('note' in d.guard_denies, false);
  const p = byId(D.metrics({ p1: true }).cards);
  assert.equal(p.pool.big, '7');
  assert.equal(p.pool.small, '上限 12 · 排队 1');
  assert.equal(p.pool.tone, 'var(--amber)');
  assert.equal(p.turn_duration.big, '3 分 10 秒');
  assert.equal(p.merges.big, '0');
  assert.equal('note' in p.merges, false);
  assert.equal('note' in p.collections, false);
  assert.equal(metricView({ id: 'x', value: null, unit: '次' }).big, '—');
  assert.equal(fmtSeconds(190), '3 分 10 秒');
  assert.equal(fmtSeconds(120), '2 分');
  assert.equal(fmtCount(12000), '1.2 万');
  assert.equal(fmtCount(2.3e8), '2.3 亿');
});

test('指标卡哪些画（R18）：没有数据来源的不画（unavailable / available=false），证据采集随证据源开关', () => {
  const ids = (cards, o) => visibleMetricCards(cards, o).map((c) => c.id);
  const p1 = D.metrics({ p1: true }).cards;
  assert.equal(ids(p1).includes('merges'), false);
  assert.equal(ids(p1).includes('collections'), false);
  assert.equal(ids(p1, { evidenceOn: true }).includes('collections'), true);
  assert.equal(ids(p1).length, p1.length - 2);
  const design = D.metrics().cards;
  assert.equal(ids(design).includes('merges'), true);
  assert.equal(ids(design).includes('collections'), false);
  assert.deepEqual(ids(design, { evidenceOn: true }), design.map((c) => c.id));
  assert.deepEqual(ids([{ id: 'merges', available: false }, { id: 'pool' }, null]), ['pool']);
});

test('迷你折线：12 个点铺满宽度，最大值在上、最小值在下；全 0 时是一条平线', () => {
  const p = sparkPath([0, 10], { w: 100, h: 30 });
  assert.equal(p.line, 'M2.0 27.0 L98.0 3.0');
  assert.equal(p.area, 'M2.0 27.0 L98.0 3.0 L 98 30 L 2 30 Z');
  assert.equal(sparkPath(Array(12).fill(0), { w: 180, h: 30 }).line.split(' L').length, 12);
  assert.ok(sparkPath(Array(12).fill(0)).line.split(' ').every((x) => x !== 'NaN'));
  assert.equal(sparkPath([5]), null);
});

test('归因：重跑、首次接受率为 null 显示「—」，包名去掉 @ 版本，失败率 ≥ 5% 标红', () => {
  const p1 = D.attribution({ by: 'pack', p1: true }).items.map(attributionRow);
  assert.equal(p1[0].name, 'core');
  assert.equal(p1[0].version, '1.4.2');
  assert.equal(p1[0].reruns, '—');
  assert.equal(p1[0].accept, null);
  const retail = p1.find((r) => r.name === 'industry-retail');
  assert.equal(retail.fail, '12.0%');
  assert.equal(retail.failHigh, true);
  // 后端按节点时 version_or_source 取 key 里 @ 后面，节点 key 不带版本时为 null
  const node = D.attribution({ by: 'node', p1: true }).items.map(attributionRow);
  assert.equal(node[0].version, '—');
  assert.equal(D.attribution({ by: 'node' }).items.map(attributionRow)[0].version, 'core@1.4.2');
  const d = D.attribution({ by: 'pack' }).items.map(attributionRow);
  assert.equal(d[0].accept, 82);
  // 整列为空的可选列不出（R18）：B3b 口径 reruns、first_accept_pct 全是 null
  assert.deepEqual(attributionColumns(D.attribution({ by: 'pack', p1: true }).items), []);
  assert.deepEqual(attributionColumns(D.attribution({ by: 'pack' }).items), ['reruns', 'first_accept_pct']);
  assert.deepEqual(attributionColumns([{ reruns: null, first_accept_pct: 40 }]), ['first_accept_pct']);
  assert.deepEqual(attributionColumns(null), []);
  assert.equal(d[0].duration, '38 秒');
  assert.equal(d[2].duration, '1 分 46 秒');
});

test('链路瀑布：位置按占总时长的百分比，最窄 0.6%，超出右边界往左收', () => {
  const rows = waterfallRows({
    total_ms: 1000,
    spans: [
      { id: 'b', name: '脚本', kind: 'script', start_offset_ms: 500, duration_ms: 250, status: 'ok' },
      { id: 'a', name: '用户消息', kind: 'message', start_offset_ms: 0, duration_ms: 0, status: 'ok' },
      { id: 'c', name: '守卫', kind: 'guard', start_offset_ms: 998, duration_ms: 1, status: 'denied' },
      { id: 'd', name: '检查', kind: 'check', start_offset_ms: 900, duration_ms: 50, status: 'error' },
    ],
  });
  assert.deepEqual(rows.map((r) => r.id), ['a', 'b', 'd', 'c']);
  assert.deepEqual([rows[0].left, rows[0].width, rows[0].icon, rows[0].cls], [0, 0.6, 'msg', '']);
  assert.deepEqual([rows[1].left, rows[1].width, rows[1].cls, rows[1].ms], [50, 25, 'tool', '0.3s']);
  assert.equal(rows[2].cls, 'err');
  assert.equal(rows[3].width, 0.6);
  assert.equal(rows[3].left, 99.4);
  assert.equal(rows[3].cls, 'wr');
  const main = waterfallRows(D.traceDetail('4f9c2e1d'));
  assert.equal(main.length, 11);
  assert.equal(main.find((r) => r.name.startsWith('子代理')).cls, 'sub');
  main.forEach((r) => assert.ok(r.left + r.width <= 100.0001));
  assert.deepEqual(waterfallRows(null), []);
});

test('没带参数时的链路：取开始时间最晚的一条', () => {
  assert.equal(latestTrace([{ trace_id: 'a', started_at: '2026-11-01T01:00:00Z' }, { trace_id: 'b', started_at: '2026-11-01T03:00:00Z' }, { trace_id: 'c', started_at: '2026-11-01T02:00:00Z' }]).trace_id, 'b');
  assert.equal(latestTrace([]), null);
  assert.equal(latestTrace(D.traceList()).trace_id, '4f9c2e1d');
});

test('日志筛选：级别（多值）、标签、查询 key=value OR 文本', () => {
  const all = D.logs();
  assert.equal(all.length, 10);
  assert.equal(all.filter((e) => matchLog(e, { level: levelParam('WARN') })).length, 2);
  assert.equal(all.filter((e) => matchLog(e, { level: 'WARN,ERROR' })).length, 3);
  assert.equal(all.filter((e) => matchLog(e, { level: levelParam('all') })).length, 10);
  assert.equal(all.filter((e) => matchLog(e, { q: 'trace=4f9c2e1d' })).length, 6);
  assert.equal(all.filter((e) => matchLog(e, { q: 'trace=4f9c2e1d OR pack=industry-retail@1.0.3' })).length, 8);
  assert.equal(all.filter((e) => matchLog(e, { q: 'RuleSchema' })).length, 1);
  assert.equal(all.filter((e) => matchLog(e, { labels: 'project=retail-restock' })).length, 3);
  assert.equal(all.filter((e) => matchLog(e, { labels: { pack: 'core@1.4.2' } })).length, 2);
  assert.equal(levelCls('WARN'), 'W');
  assert.equal(levelCls('DEBUG'), '');
  const sorted = sortLogs(all);
  assert.ok(sorted[0].ts < sorted[9].ts);
  assert.equal(sorted[9].event, 'pack.breaker.open');
  assert.deepEqual(logFields({ message: 'm', a: 1, trace: 't' }).map((f) => [f.k, f.trace]), [['a', false], ['trace', true], ['message', false]]);
});

test('跟随：按 ts 与事件去重，最多留 500 条', () => {
  const e = { ts: '2026-11-01T00:00:00.000Z', event: 'x', level: 'INFO', fields: {} };
  assert.equal(appendLog([e], { ...e }).length, 1);
  let list = [];
  for (let i = 0; i < MAX_LOG_LINES + 5; i += 1) list = appendLog(list, { ...e, ts: String(i).padStart(4, '0') });
  assert.equal(list.length, MAX_LOG_LINES);
  assert.equal(list[0].ts, '0005');
});

test('告警规则：显示、处置手册第一句、校验、只提交改了的字段、包声明的规则只能改 enabled', () => {
  const rules = D.alertRules();
  assert.equal(rulesSub(rules), '7 条');
  assert.equal(rulesSub(rules.filter((r) => r.source === 'kernel')), '5 条');
  const v = ruleView(rules[0]);
  assert.equal(v.condition, 'node.fail_rate > 5%（1 小时）');
  assert.deepEqual(v.state, { label: '触发中', tone: 'red', dot: true });
  assert.deepEqual(v.level, ['严重', 'red']);
  assert.equal(v.source, 'industry-retail@1.0.3');
  assert.equal(v.summary, '熔断出错的包，再查失败原因。');
  assert.equal(ruleView(rules[4]).condition, 'guard.deny 环比 > 300%');
  assert.equal(ruleView({ ...rules[1], enabled: false }).state.label, '已停用');
  assert.equal(runbookSummary('## **先看日志**\n\n其余'), '先看日志');
  assert.equal(runbookSummary(''), '还没写处置手册');
  assert.deepEqual(Object.keys(ruleProblems({ name: '', condition: 'abc', level: 'warning' })), ['name', 'condition']);
  assert.deepEqual(ruleProblems({ name: 'x', condition: 'a ≥ 3', level: 'info' }), {});
  assert.deepEqual(rulePatch(rules[1], { ...rules[1], window: ' 1 小时 ', enabled: false }), { window: '1 小时', enabled: false });
  assert.deepEqual(rulePatch(rules[0], { ...rules[0], name: '改名', enabled: false }), { enabled: false });
  assert.deepEqual(rulePatch(rules[1], { ...rules[1] }), {});
});

test('导出器：开着的要有地址和信号；没改动不能保存', () => {
  const items = D.exporters();
  assert.deepEqual(exporterProblems(items), {});
  const on = items.map((e) => (e.id === 'custom' ? { ...e, enabled: true } : e));
  assert.deepEqual(Object.keys(exporterProblems(on)), ['custom']);
  const filled = on.map((e) => (e.id === 'custom' ? { ...e, target: 'https://telemetry.example.com/ingest' } : e));
  assert.match(exporterProblems(filled).custom, /至少选一种信号/);
  assert.equal(exportersDirty(items, D.exporters()), false);
  assert.equal(exportersDirty(on, items), true);
  assert.deepEqual(exportersBody([items[0]]).items[0], { id: 'otel', enabled: true, target: items[0].target, signals: ['logs', 'metrics', 'traces'] });
});

test('链路检索：trace id 前缀、键=值（含用户）、页面标签打底且可被覆盖、状态与时间范围', () => {
  assert.deepEqual(parseTraceQuery('4F9C project=retail-restock 用户=zhangsan'), {
    traceId: '4f9c', labels: [{ key: 'project', value: 'retail-restock' }, { key: 'user', value: 'zhangsan' }], error: null,
  });
  assert.ok(parseTraceQuery('foo=bar').error);
  const { query, traceId } = traceSearchQuery({ text: 'project=bike-fleet', status: 'error', range: '7d', labels: 'project=retail-restock,pack=core@1.4.2,trace=AB12' });
  assert.deepEqual(query, { labels: 'project=bike-fleet,pack=core@1.4.2', status: 'error', range: '7d' });
  assert.equal(traceId, 'ab12');
  assert.equal(traceSearchQuery({ text: '', status: 'all' }).query.status, '');
  const now = Date.now();
  const all = D.traceList(now);
  assert.equal(selectTraces(all, {}, now).length, 11);
  assert.equal(selectTraces(all, { range: '7d' }, now).length, 13);
  assert.equal(selectTraces(all, { range: '1h' }, now).length, 6);
  assert.deepEqual(selectTraces(all, { status: 'error' }, now).map((t) => t.trace_id), ['a83d0b52', 'e2c470b9']);
  assert.deepEqual(selectTraces(all, { labels: 'user=zhangsan' }, now).map((t) => t.trace_id), ['4f9c2e1d', 'b6f8023e', '8b1a6c04']);
  assert.deepEqual(filterTraces(all, '4f9c').map((t) => t.trace_id), ['4f9c2e1d']);
  assert.equal(filterTraces(all, null).length, all.length);
});

// ---------- mock 路由 ----------

function mockServer(sc = {}) {
  const routes = [];
  const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const err = (status, code, message, details = null) => json(status, { code, message, details });
  const forbid = (user, roles) => (roles.some((r) => (user?.roles || [user?.role]).includes(r)) ? undefined : err(403, 'forbidden', '没有权限'));
  register({ on: (method, pattern, handler) => routes.push({ method, re: new RegExp(`^/api/v1${pattern}$`), handler }), json, err, forbid, sc });
  const call = async (method, path, body, roles = ['user', 'ops']) => {
    const u = new URL(path, 'http://mock.local');
    for (const r of routes.filter((x) => x.method === method && x.re.test(u.pathname))) {
      const res = await r.handler({ m: u.pathname.match(r.re), query: u.searchParams, body: body || {}, user: { username: 'zhangsan', roles } });
      if (res !== undefined) {
        const text = res.status === 204 || (res.headers.get('Content-Type') || '').includes('event-stream') ? null : await res.text();
        return { status: res.status, body: text ? JSON.parse(text) : null, res };
      }
    }
    throw new Error(`${method} ${path} 没有路由`);
  };
  return { call };
}

const O = '/api/v1/ops';

test('mock O-01：读 ops、admin，建模者 403；range 决定天数；ops:p1 按 B2 口径', async () => {
  const { call } = mockServer();
  assert.equal((await call('GET', `${O}/overview`, null, ['user'])).status, 403);
  const admin = await call('GET', `${O}/overview?range=7d`, null, ['user', 'admin']);
  assert.equal(admin.status, 200);
  assert.equal(admin.body.usage_daily.days.length, 7);
  assert.equal(admin.body.alerts.filter((a) => a.state === 'firing').length, 2);
  assert.equal(admin.body.projects.length, 8);
  assert.ok(admin.body.projects.every((p, i, a) => !i || a[i - 1].disk_bytes >= p.disk_bytes));
  assert.ok(admin.body.projects.every((p) => !('goal' in p)), '项目只给元数据视图');
  const p1 = (await mockServer({ ops: 'p1' }).call('GET', `${O}/overview`)).body;
  assert.equal(p1.stats.accounts, null);
  assert.equal(p1.stats.autobuild.limit, null);
  assert.equal(p1.autobuild_today.avg_cost.amount, null);
  assert.equal(p1.stats.cost.total.currency, 'USD');
  assert.deepEqual(p1.alerts, []);
  assert.deepEqual(p1.autobuild_runs, []);
});

test('mock O-02 / O-03：range 校验、按标签筛归因', async () => {
  const { call } = mockServer();
  assert.equal((await call('GET', `${O}/metrics?range=30d`)).status, 400);
  const m = await call('GET', `${O}/metrics?range=1h`);
  assert.equal(m.body.cards.length, 10);
  assert.equal((await call('GET', `${O}/attribution?by=x`)).status, 400);
  const a = await call('GET', `${O}/attribution?by=pack&labels=${encodeURIComponent('pack=core@1.4.2')}`);
  assert.deepEqual(a.body.items.map((x) => x.key), ['core']);
});

test('mock O-04 / O-05：链路详情（十六进制 ID 都能打开，其余 trace_not_found）、检索分页', async () => {
  const { call } = mockServer();
  const t = await call('GET', `${O}/traces/4f9c2e1d`);
  assert.equal(t.body.total_ms, 84300);
  assert.equal(t.body.labels.project, 'retail-restock');
  assert.equal((await call('GET', `${O}/traces/a83d0b52`)).body.spans.some((s) => s.status === 'error'), true);
  assert.equal((await call('GET', `${O}/traces/0bad1dea`)).status, 200);
  const missing = await call('GET', `${O}/traces/not-a-trace`);
  assert.equal(missing.status, 404);
  assert.equal(missing.body.code, 'trace_not_found');
  const p1 = await call('GET', `${O}/traces?limit=5`);
  assert.equal(p1.body.items.length, 5);
  assert.equal(p1.body.next_cursor, '5');
  const p3 = await call('GET', `${O}/traces?limit=5&cursor=10`);
  assert.equal(p3.body.items.length, 1);
  assert.equal(p3.body.next_cursor, null);
  const byUser = await call('GET', `${O}/traces?labels=${encodeURIComponent('user=sunyu')}&status=error`);
  assert.deepEqual(byUser.body.items.map((x) => x.trace_id), ['a83d0b52', 'e2c470b9']);
});

test('mock O-06 / O-07：级别筛选、非法级别 400、日志流按事件信封推送', async () => {
  const { call } = mockServer();
  assert.equal((await call('GET', `${O}/logs?level=ERROR`)).body.items.length, 1);
  assert.equal((await call('GET', `${O}/logs?level=FATAL`)).status, 400);
  const s = await call('GET', `${O}/logs/stream`);
  assert.equal(s.status, 200);
  const reader = s.res.body.getReader();
  const dec = new TextDecoder();
  let text = '';
  while (!text.includes('"type":"log"')) text += dec.decode((await reader.read()).value);
  await reader.cancel();
  const env = JSON.parse(text.split('\n').find((l) => l.startsWith('data: ')).slice(6));
  assert.equal(env.type, 'log');
  assert.equal(env.seq, 1);
  assert.equal(env.data.event, 'session.turn.start');
});

test('mock O-08～O-10：触发数、新建校验、包声明的规则只能启停不能删、内核规则可删；admin 写 403', async () => {
  const { call } = mockServer();
  const list = await call('GET', `${O}/alerts/rules`, null, ['user', 'admin']);
  assert.equal(list.body.items.length, 7);
  assert.equal(list.body.firing, 2);
  assert.equal((await call('POST', `${O}/alerts/rules`, { name: 'x', condition: 'a > 1', level: 'warning' }, ['user', 'admin'])).status, 403);
  const bad = await call('POST', `${O}/alerts/rules`, { name: '', condition: '', level: 'nope' });
  assert.deepEqual(Object.keys(bad.body.details.fields).sort(), ['condition', 'level', 'name']);
  const created = await call('POST', `${O}/alerts/rules`, { name: '回收站过大', condition: 'trash.bytes > 2GB', window: '1 天', level: 'info', runbook_md: '清空回收站' });
  assert.equal(created.status, 201);
  assert.equal(created.body.source, 'kernel');
  assert.equal((await call('PUT', `${O}/alerts/rules/node-fail-rate`, { name: '改名' })).status, 403);
  const off = await call('PUT', `${O}/alerts/rules/node-fail-rate`, { enabled: false });
  assert.equal(off.body.enabled, false);
  assert.equal((await call('GET', `${O}/alerts/rules`)).body.firing, 1);
  assert.equal((await call('DELETE', `${O}/alerts/rules/node-fail-rate`)).status, 403);
  assert.equal((await call('DELETE', `${O}/alerts/rules/${created.body.id}`)).status, 204);
  assert.equal((await call('GET', `${O}/alerts/rules`)).body.items.length, 7);
});

test('mock O-11 / O-12：开启没有地址的导出器 400，填了地址后保存、滚动重启生效', async () => {
  const { call } = mockServer();
  const bad = await call('PUT', `${O}/exporters`, { items: [{ id: 'custom', enabled: true }] });
  assert.equal(bad.status, 400);
  const ok = await call('PUT', `${O}/exporters`, { items: [{ id: 'custom', enabled: true, target: 'https://telemetry.example.com/ingest', signals: ['metrics'] }] });
  assert.equal(ok.body.effective, 'rolling_restart');
  const custom = ok.body.items.find((e) => e.id === 'custom');
  assert.deepEqual([custom.enabled, custom.configured], [true, true]);
  assert.equal((await call('PUT', `${O}/exporters`, { items: [] }, ['user', 'admin'])).status, 403);
});

test('mock obs:p1：告警规则与导出器后端没做，返回 404', async () => {
  const { call } = mockServer({ obs: 'p1' });
  assert.equal((await call('GET', `${O}/alerts/rules`)).status, 404);
  assert.equal((await call('GET', `${O}/exporters`)).status, 404);
  const merges = (await call('GET', `${O}/metrics`)).body.cards.find((c) => c.id === 'merges');
  assert.equal(merges.note, '变更集在 P2');
  assert.equal(merges.unavailable, true);
});
