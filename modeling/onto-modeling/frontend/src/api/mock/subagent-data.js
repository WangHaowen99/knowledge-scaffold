// mock · S-21 子任务步骤（合成数据）：说明页「运行中」示例里的三个子任务
const MIN = 60 * 1000;
const at = (base, offsetMs) => new Date(base + offsetMs).toISOString();

const SEMANTIC_BRIEF = [
  '检查陈列量核定相关的 11 个操作与对象、规则的语义是否一致：',
  '1. 每个操作引用的对象字段在对象文档里都存在，取值范围一致；',
  '2. 判断分支没有遗漏（如「超出标准」之外的处理）；',
  '3. 规则里的阈值与操作里的说法一致。',
  '逐条列出问题、所在步骤和修改建议，不要直接改文件。',
].join('\n');

function tool(id, text, start, end) {
  return { id, kind: 'tool', text, state: end == null ? 'running' : 'done', started_at: start, ended_at: end, duration_ms: null };
}

// 按请求时刻生成：已运行多久、每步耗时都按现在往回推
export function subagentTask(taskId, now = Date.now()) {
  if (taskId === 't1') {
    const base = now - 6 * MIN;
    return {
      task: { task_id: 't1', title: '规则检查', brief: '对 11 个操作逐条跑规则检查，汇总错误与警告。', status: 'done', started_at: at(base, 0), ended_at: at(base, 2.5 * MIN), step_count: 2, summary: '0 个错误' },
      items: [tool('r1', '运行脚本「规则检查」', at(base, 5000), at(base, 98000)), tool('r2', '读取报告「规则检查」', at(base, 99000), at(base, 101000))],
    };
  }
  if (taskId === 't2') {
    const base = now - 11 * MIN;
    return {
      task: { task_id: 't2', title: '语义检查', brief: SEMANTIC_BRIEF, status: 'running', started_at: at(base, 0), ended_at: null, step_count: 5, summary: null },
      items: [
        tool('s1', '读取操作「陈列量核定」', at(base, 4000), at(base, 6000)),
        tool('s2', '读取对象「门店」', at(base, 7000), at(base, 8000)),
        tool('s3', '读取对象「商品陈列标准」', at(base, 8000), at(base, 9500)),
        { id: 's-say-1', kind: 'text', text: '前 5 个操作已核对，发现 1 处字段取值不一致，继续看后面 6 个。', state: 'done', started_at: at(base, 3 * MIN), ended_at: null, duration_ms: null },
        tool('s4', '搜索「超出标准」', at(base, 3 * MIN + 5000), at(base, 3 * MIN + 7000)),
        tool('s5', '读取规则「陈列量上限」', at(base, 3 * MIN + 9000), at(base, 3 * MIN + 12000)),
      ],
    };
  }
  if (taskId === 't3') {
    return { task: { task_id: 't3', title: '覆盖检查', brief: null, status: 'started', started_at: at(now, -20000), ended_at: null, step_count: 0, summary: null }, items: [] };
  }
  return null;
}
