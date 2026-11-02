// 自主构建（l4-run）的纯函数：运行状态分类、运行条文案与指标、流水线格子、按切片进展块的分段

export const RUNNING_STATES = ['queued', 'running', 'pausing', 'stopping', 'converting'];
export const ENDED_STATES = ['completed', 'stopped', 'converted', 'budget_exhausted', 'failed'];

export function runFlags(run) {
  const st = run?.state || null;
  return {
    state: st,
    running: RUNNING_STATES.includes(st),
    // 正在去往安全点：按钮先收起，等运行条变成结束态
    settling: ['pausing', 'stopping', 'converting'].includes(st),
    paused: ['paused', 'interrupted'].includes(st),
    ended: ENDED_STATES.includes(st),
  };
}

const pad2 = (n) => String(n).padStart(2, '0');
export function hhmm(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

// 38 分 / 1 小时 52 分 / 2 小时
export function fmtDur(ms) {
  if (ms == null) return '—';
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m} 分`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} 小时 ${m % 60} 分` : `${h} 小时`;
}

// 预计总用时按分钟说：约 84 分
export const fmtEst = (ms) => (ms == null ? '—' : `约 ${Math.round(ms / 60000)} 分`);

// 2.1M / 6M / 820k
export function fmtTok(n) {
  if (n == null) return '—';
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, '')}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(n);
}

export function money(m) {
  if (!m || m.amount == null) return '—';
  return `${m.currency === 'USD' ? '$' : '¥'}${Math.round(m.amount)}`;
}

const pct = (a, b) => (b ? Math.max(0, Math.min(100, Math.round((a / b) * 100))) : 0);

// 运行条的三格指标：[标签, 当前, 上限, 百分比]
export function stripMetrics(run) {
  if (!run) return [];
  const { ended } = runFlags(run);
  const budgetMs = (run.policy?.budget?.max_duration_min || 0) * 60000;
  const time = ended
    ? ['用时', fmtDur(run.elapsed_ms), fmtDur(budgetMs), pct(run.elapsed_ms, budgetMs)]
    : ['已用时', fmtDur(run.elapsed_ms), run.est_total_ms ? fmtEst(run.est_total_ms) : '—', pct(run.elapsed_ms, run.est_total_ms || budgetMs)];
  return [
    time,
    ['token', fmtTok(run.tokens?.used), fmtTok(run.tokens?.limit), pct(run.tokens?.used, run.tokens?.limit)],
    ['费用', money(run.cost?.used), money(run.cost?.budget), pct(run.cost?.used?.amount, run.cost?.budget?.amount)],
  ];
}

const ENDED_TEXT = {
  completed: ['已完成', '全部切片完成'],
  stopped: ['已停下', '在安全点停下'],
  converted: ['已转为陪伴', '剩下的逐个确认'],
  budget_exhausted: ['预算用完', '在安全点停下'],
  failed: ['出错停下', '已保存到最近的安全点'],
};

// 运行条标题与副标题
export function stripTitle(run) {
  if (!run) return { title: '', sub: '' };
  const n = `运行 #${run.run_no}`;
  const cur = run.current ? `${run.current.slice_name} › ${run.current.step_label}` : '';
  switch (run.state) {
    case 'queued': return { title: `排队中 · ${n}`, sub: '' };
    case 'running': return { title: `自主构建中 · ${n}`, sub: cur ? `当前：${cur}` : '' };
    case 'pausing': return { title: `正在暂停 · ${n}`, sub: '当前步骤做完后暂停' };
    case 'stopping': return { title: `停在安全点 · ${n}`, sub: run.current ? `「${run.current.slice_name}」做完后停下` : '这一片做完后停下' };
    case 'converting': return { title: `转为陪伴 · ${n}`, sub: run.current ? `「${run.current.slice_name}」做完后转为逐个确认` : '这一片做完后转为逐个确认' };
    case 'paused': return { title: `已暂停 · ${n}`, sub: cur ? `停在：${cur}` : '' };
    case 'interrupted': return { title: `${n} 中断了`, sub: '服务重启' };
    default: {
      const [t, tail] = ENDED_TEXT[run.state] || ['已结束', ''];
      return { title: `${n} ${t}`, sub: [run.started_at && run.ended_at ? `${hhmm(run.started_at)} – ${hhmm(run.ended_at)}` : '', tail].filter(Boolean).join(' · ') };
    }
  }
}

// 流水线格子：契约取值 → 设计稿的格子样式与文字
export const CELL = {
  confirmed: ['ok', '已确认'],
  auto: ['auto', '自主完成'],
  running: ['run', '进行中'],
  queued: ['q', '排队'],
  skipped: ['skip', '已跳过'],
};

// 结果列：进行中 / 运行前就完成 / 排队 / 部分完成 / 自主完成
export function matrixTone(row) {
  const cells = Object.values(row.cells || {});
  if (cells.includes('running')) return 'running';
  if (cells.every((c) => c === 'queued')) return 'gray';
  if (cells.every((c) => c === 'confirmed' || c === 'skipped')) return 'gray';
  if (/部分|未开始/.test(row.result || '')) return 'amber';
  return 'indigo';
}

// 按切片的进展块：把时间线拆成「连续的步骤 / 自动决定 / 挂起」几段，按原顺序渲染
export function sliceSegments(timeline) {
  const out = [];
  (timeline || []).forEach((x, i) => {
    if (x && (x.type === 'auto_decision' || x.type === 'suspension')) {
      out.push({ kind: x.type, key: x.card?.card_id || `${x.type}-${i}`, card: x.card });
      return;
    }
    const last = out[out.length - 1];
    if (last && last.kind === 'steps') last.steps.push(x);
    else out.push({ kind: 'steps', key: `steps-${i}`, steps: [x] });
  });
  return out;
}

export const confidenceColor = (v, threshold = 0.8) => (v >= threshold ? '#2563EB' : '#F59E0B');

// 运行详情抽屉的六格小指标
export function miniMetrics(run) {
  if (!run) return [];
  const { ended } = runFlags(run);
  return [
    [fmtDur(run.elapsed_ms), ended ? `用时 · 预算 ${fmtDur((run.policy?.budget?.max_duration_min || 0) * 60000)}` : `已用时 · 预计${fmtEst(run.est_total_ms)}`],
    [fmtTok(run.tokens?.used), `token · 上限 ${fmtTok(run.tokens?.limit)}`],
    [money(run.cost?.used), `费用 · 预算 ${money(run.cost?.budget)}`],
    [`${run.checks?.errors ?? 0} / ${run.checks?.warnings ?? 0}`, '写入即查：错误 / 提醒'],
    [`${run.scenarios?.passed ?? 0} / ${run.scenarios?.total ?? 0}`, '场景推演通过'],
    [`${run.fix_rounds?.used ?? 0} / ${run.fix_rounds?.limit ?? 0}`, '修复轮次 / 上限'],
  ];
}

// 运行报告卡的八格指标
export function reportMetrics(r) {
  const m = r?.metrics;
  if (!m) return [];
  const partial = m.slices?.partial?.length ? `${m.slices.partial.join('、')}部分完成` : '全部完成';
  return [
    ['用时', fmtDur(m.duration_ms), `预算 ${fmtDur(m.budget_duration_ms)}`],
    ['切片', `${m.slices.done} / ${m.slices.total}`, partial],
    ['产出', `+${m.produced.objects} / +${m.produced.operations} / +${m.produced.rules}`, '对象 / 操作 / 规则'],
    ['检查', `${m.checks.errors} 错误`, `${m.checks.warnings} 个提醒`],
    ['场景推演', `${m.scenarios.passed} / ${m.scenarios.total}`, m.scenarios.suspended_failures ? `${m.scenarios.suspended_failures} 个未通过已挂起` : '全部通过'],
    ['自动决策', `${m.decisions.total} 项`, `高置信 ${m.decisions.high_confidence} · 需看 ${m.decisions.needs_look}`],
    ['挂起', `${m.suspended} 项`, m.suspended ? '已放进待处理' : '没有挂起'],
    ['费用', money(m.cost), `token ${fmtTok(m.tokens)} · 预算 ${money(m.budget)}`],
  ];
}

// 报告卡副标题：起止时间 + 崩溃续跑说明
export function reportSub(r) {
  if (!r) return '';
  const span = r.started_at && r.ended_at ? `${hhmm(r.started_at)} – ${hhmm(r.ended_at)}` : '';
  const rec = r.recovered_restarts ? `服务重启 ${r.recovered_restarts} 次，已自动续跑` : '';
  return [span, rec].filter(Boolean).join(' · ');
}

export const REPORT_CHIP = { completed: ['全部切片完成', 'blue'], stopped: ['在安全点停下', 'amber'], converted: ['已转为陪伴', 'indigo'], budget_exhausted: ['预算用完', 'amber'], failed: ['出错停下', 'red'] };
