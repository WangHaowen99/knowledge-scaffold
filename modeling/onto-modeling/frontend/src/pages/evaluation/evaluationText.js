// 评测页面用到的纯函数（照 develop 的 CenterPane、EvalRunDetail、SolverModelPanel），单测直接测这里
import { currentRevisionValidation } from './evaluationModel.js';

// 消息/报告里的工作区路径（develop CenterPane 的 PATH_RE、INLINE_PATH_RE）
const PATH_RE = /(?:research|reports|ontology|knowledge|resource)\/[^\s`）)，。;；:"'<>|]+\.(?:md|mmd|drawio|xlsx|xlsm|html|py|csv|docx|pdf|png|jpe?g)/g;
export const INLINE_PATH_RE = /^(?:ontology|research|reports|resource)\/[^\s`]+\.(?:md|py|json|yaml|yml|csv|mmd|drawio|xlsx?|html|txt)$/i;
// 报告里「完整回答」的链接：reports/<报告>.answers.json#<结果 ID>
export const ANSWER_LINK = /^(reports\/[^\s#]+\.answers\.json)#([^\s#]+)$/;

// 正文里的工作区路径包成链接（围栏代码块、行内代码原样）
export function linkifyPaths(md) {
  return String(md || '').split(/(```[\s\S]*?```|~~~[\s\S]*?~~~)/).map((part, i) => {
    if (i % 2 === 1) return part;
    return part.split(/(`[^`\n]*`)/).map((seg, j) => {
      if (j % 2 === 1) return seg;
      return seg.replace(PATH_RE, (m, offset, whole) => {
        const before = whole.slice(Math.max(0, offset - 2), offset);
        if (before.includes('[') || before.includes('(')) return m;
        return `[${m}](#file:${encodeURIComponent(m)})`;
      });
    }).join('');
  }).join('');
}

// ---- 旧版评测记录：评测执行详情 ----

export const PHASES = [['package', '打包'], ['import', '导入'], ['ds', '数据源'], ['cases', '用例'], ['evaluate', '执行']];
const DS_PHASES = ['data_import', 'data_source', 'graph_import'];
export const VERDICTS = [['pass', '通过'], ['partial', '部分'], ['fail', '失败'], ['error', '异常']];
export const VERDICT_TEXT = { ...Object.fromEntries(VERDICTS), running: '执行中', pending: '待执行' };
export const TERMINAL = new Set(VERDICTS.map(([key]) => key));
export const STATUS_TEXT = { running: '运行中', done: '已完成', failed: '存在技术异常', stopped: '已停止', interrupted: '已中断' };
export const POLL_MS = 8000;

export function phaseIndex(st) {
  if (st.status === 'done') return PHASES.length;
  // 排队等待评测槽位：还没进入任何阶段
  if (st.phase === 'queued') return -1;
  const key = DS_PHASES.includes(st.phase) ? 'ds' : st.phase;
  return Math.max(0, PHASES.findIndex(([k]) => k === key));
}

export function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${m}:${s}` : `${m}:${s}`;
}

export function verdictCounts(cases) {
  const counts = { pass: 0, partial: 0, fail: 0, error: 0 };
  (cases || []).forEach((c) => { if (TERMINAL.has(c.verdict)) counts[c.verdict] += 1; });
  return counts;
}

// ---- 求解验证卡 ----

export const shortRevision = (revision) => (revision ? String(revision).replace(/^sha256:/, '').slice(0, 10) : '—');

// 卡上那句状态说明（develop 原文）
export function solverValidationText(status, revision, dirty) {
  const current = currentRevisionValidation(status, revision);
  if (dirty) return '有未保存的修改：保存并发布新版本后，再对新版本发起求解验证。';
  if (status?.configured === false) return '评测服务不可用或未配置，暂时读不到求解验证状态。';
  if (current.state === 'passed') return `当前修订已在 ${current.version} 通过求解验证。`;
  if (current.state === 'published') return '当前修订已发布，尚未通过求解验证：在评测页面对该版本发起求解验证。';
  if (status) return '当前修改尚未发布和验证：发布新版本后，在发布页「已发布版本」里发起求解验证。';
  return '正在读取求解验证状态。';
}
