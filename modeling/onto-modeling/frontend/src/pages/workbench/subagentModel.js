// 子任务进度面板的纯计算：步骤合并、时长、思考中与结果的说法（面板 subagent.jsx 用，单测直接测）
import { subagentStatus } from '../../state/conversation.js';

const ms = (iso) => {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isFinite(t) ? t : null;
};

export function fmtElapsed(value) {
  if (value == null || value < 0) return '';
  const s = Math.floor(value / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分 ${s % 60} 秒`;
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

// 接口取到的明细在前（按出现顺序），对话流里实时到的同一步只改状态与结束时刻，新的一步接在后面
export function panelSteps(fetched, live) {
  const out = (fetched || []).map((s) => ({ ...s }));
  const at = new Map(out.map((s, i) => [s.id, i]));
  (live || []).forEach((s) => {
    const i = at.get(s.id);
    if (i == null) {
      at.set(s.id, out.length);
      out.push({ ...s });
      return;
    }
    const cur = out[i];
    const over = s.state !== 'running';
    out[i] = {
      ...cur,
      state: over || cur.state === 'running' ? s.state : cur.state,
      text: s.text || cur.text,
      ended_at: cur.ended_at || s.ended_at || null,
    };
  });
  return out;
}

// 一步的耗时：做完的按开始到结束；在做的按开始到现在；说的话没有耗时。
// 工具本身多在几十到几百毫秒（时间花在步骤之间的模型思考上），不满 1 秒不写，免得满屏「0 秒」
const atLeastSecond = (value) => (value != null && value >= 1000 ? fmtElapsed(value) : '');
export function stepTime(step, now) {
  if (step.kind !== 'tool') return '';
  const start = ms(step.started_at);
  if (start == null) return '';
  if (step.state === 'running') return atLeastSecond(now - start);
  const end = ms(step.ended_at);
  if (step.duration_ms != null) return atLeastSecond(step.duration_ms);
  return end == null ? '' : atLeastSecond(end - start);
}

// 面板顶部：状态、已运行多久、做了几步
export function panelHead(sub, task, ended, now) {
  // 实时到的终态最新；接口取到的终态次之（打开面板时卡片可能还没收到收尾）
  const terminal = (x) => x?.status === 'done' || x?.status === 'failed';
  const status = subagentStatus(terminal(sub) || !terminal(task) ? sub : task, ended);
  const start = ms(task?.started_at) ?? ms(sub.started_at);
  const over = ['done', 'failed', 'interrupted', 'timeout'].includes(status);
  const end = over ? (ms(task?.ended_at) ?? ms(sub.ended_at)) : now;
  const label = { done: '已完成', failed: '失败', interrupted: '已中断', timeout: '执行超时' }[status] || '运行中';
  const steps = Math.max(task?.step_count || 0, sub.step_count || 0);
  const parts = [label];
  if (start != null && end != null) parts.push(`${over ? '用时' : '已运行'} ${fmtElapsed(end - start)}`);
  if (steps) parts.push(`${steps} 步`);
  return { status, text: parts.join(' · ') };
}

// 子代理只在想、不调工具时的一行；token 拿不到就只给时长
export function thinkingText(thinking, now) {
  if (!thinking) return '';
  const since = ms(thinking.since);
  const elapsed = since == null ? thinking.elapsed_ms : Math.max(thinking.elapsed_ms || 0, now - since);
  const tokens = thinking.output_tokens ? ` · 已输出 ${thinking.output_tokens >= 10000 ? `${(thinking.output_tokens / 10000).toFixed(1)} 万` : thinking.output_tokens} token` : '';
  return `思考中${tokens} · ${fmtElapsed(elapsed)}`;
}

// 小结第一行是结论，其后「· 」开头的每行一条明细（后端把检查、修复类子代理交回的 JSON 换成的说法）
export function summaryParts(summary) {
  const [head = '', ...rest] = String(summary || '').split('\n');
  return { head: head.trim(), details: rest.map((x) => x.replace(/^·\s*/, '').trim()).filter(Boolean) };
}

// 结束后面板底部：结论与明细，或失败原因
export function outcome(status, summary) {
  const { head, details } = summaryParts(summary);
  if (status === 'done') return { tone: 'green', label: '结果', text: head || '已完成，结果交回给助手', details };
  if (status === 'failed') return { tone: 'red', label: '失败原因', text: head || '子任务出错，没有交回结果', details };
  if (status === 'interrupted') return { tone: 'gray', label: '已中断', text: '会话已停下，这个子任务没有交回结果', details: [] };
  if (status === 'timeout') return { tone: 'gray', label: '执行超时', text: '长时间没有响应，已判定执行超时', details: [] };
  return null;
}
