import { Type } from '@earendil-works/pi-ai';
import { buildLlm, type LlmDeps } from '../llm/provider.js';
import { makeAgent, runAgentCaptured } from '../workflow/agent.js';

// LLM Judge：比对 (question, ground_truth, final_answer) → 通过/部分/失败 + 理由。
// 走与建模同一 LLM 配置（faux 模式下由 faux 分支按包含关系启发式判定）。
export interface Verdict { verdict: 'pass' | 'partial' | 'fail'; reason: string; }

export function buildJudgeSystemPrompt(): string {
  return [
    '你是评测判分员（judge）。比对智能体回答与标准答案，判定：',
    'pass=关键结论与数值一致；partial=方向正确但遗漏或部分错误；fail=结论错误或未回答。',
    '先对齐比较范围：标准答案是全月、月度、整体或汇总口径时，只能与回答中同层级的汇总值比较，不能拿单批次、单行或局部明细代替。',
    '“组合 N”“方案 N”“场景 N”通常是用例标识，不能自行解释成“第 N 批”；只有问题明确这样定义时才能按批次理解。',
    '数值一致性按业务含义而非字符串逐字判断：百分比绝对误差不超过 0.05 个百分点，或相对误差不超过 0.1%，任一条件满足就视为舍入或显示精度差异。',
    '一般数值相对误差不超过 0.1% 时，如果关键结论一致，也应视为舍入或显示精度差异。',
    '例如 98.56% 与 98.50% 的绝对误差为 0.06 个百分点，但相对误差约 0.061%，满足相对误差条件，应按数值一致处理。',
    '当关键结论一致且数值仅有上述容差内差异时必须判 pass，不得仅因小数位数、尾随零或舍入方式不同判 partial。',
    '只评判标准答案要求的关键事实；回答中的额外说明除非与标准答案矛盾，否则不降低判定。',
    '用 submit_verdict 提交，reason 一句话说明依据。',
  ].join('\n');
}

export function extractJudgeScopeHighlights(finalAnswer: string): string[] {
  const scopePattern = /全月|月度|整体|汇总|总计|合计|总结/u;
  const substantivePattern = /\d|%|过载|超限|通过|失败|可行|不可行|一致|不一致/u;
  const highlights: string[] = [];
  let totalLength = 0;
  for (const rawLine of String(finalAnswer || '').split(/\r?\n/u)) {
    const line = rawLine.trim();
    if (!line || !scopePattern.test(line) || !substantivePattern.test(line)) continue;
    if (highlights.includes(line)) continue;
    if (highlights.length >= 24 || totalLength + line.length > 6_000) break;
    highlights.push(line);
    totalLength += line.length;
  }
  return highlights;
}

export function buildJudgeUserPrompt(question: string, groundTruth: string, finalAnswer: string): string {
  const scopeHighlights = extractJudgeScopeHighlights(finalAnswer);
  return [
    `问题：${question}`,
    `标准答案：${groundTruth}`,
    ...(scopeHighlights.length
      ? ['智能体回答中的同层级汇总索引（先用它与标准答案对齐范围，再核对完整回答）：', ...scopeHighlights]
      : []),
    `智能体完整回答：${finalAnswer || ''}`,
  ].join('\n');
}

export async function judge(
  question: string,
  groundTruth: string,
  finalAnswer: string,
  llmOverride?: LlmDeps,
): Promise<Verdict> {
  const cap: { verdict?: Verdict } = {};
  const submitVerdict = {
    name: 'submit_verdict',
    label: '提交判分',
    description: '提交判分结论',
    parameters: Type.Object({
      verdict: Type.Union([Type.Literal('pass'), Type.Literal('partial'), Type.Literal('fail')]),
      reason: Type.String(),
    }),
    async execute(_id: string, params: Verdict) {
      cap.verdict = params;
      return { content: [{ type: 'text' as const, text: '已记录。' }], details: {}, terminate: true };
    },
  };
  const sys = buildJudgeSystemPrompt();
  const agent = makeAgent(llmOverride ?? buildLlm(), sys, [submitVerdict] as any);
  await runAgentCaptured(
    agent,
    buildJudgeUserPrompt(question, groundTruth, finalAnswer),
    () => Boolean(cap.verdict),
    '不要继续输出判分说明正文。请立即调用 submit_verdict 提交 verdict 和 reason；未通过工具提交的结论不会被保存。',
  );
  if (!cap.verdict) throw new Error('评测判分服务未按协议提交结论');
  return cap.verdict;
}
