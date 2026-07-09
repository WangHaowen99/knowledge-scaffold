import { Type } from '@earendil-works/pi-ai';
import { buildLlm } from '../llm/provider.js';
import { makeAgent, runAgent } from '../workflow/agent.js';

// LLM Judge：比对 (question, ground_truth, final_answer) → 通过/部分/失败 + 理由。
// 走与建模同一 LLM 配置（faux 模式下由 faux 分支按包含关系启发式判定）。
export interface Verdict { verdict: 'pass' | 'partial' | 'fail'; reason: string; }

export async function judge(question: string, groundTruth: string, finalAnswer: string): Promise<Verdict> {
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
  const sys = [
    '你是评测判分员（judge）。比对智能体回答与标准答案，判定：',
    'pass=关键结论与数值一致；partial=方向正确但遗漏或部分错误；fail=结论错误或未回答。',
    '用 submit_verdict 提交，reason 一句话说明依据。',
  ].join('\n');
  const agent = makeAgent(buildLlm(), sys, [submitVerdict] as any);
  await runAgent(agent, [
    `问题：${question}`,
    `标准答案：${groundTruth}`,
    `智能体回答：${(finalAnswer || '').slice(0, 4000)}`,
  ].join('\n'));
  return cap.verdict ?? { verdict: 'fail', reason: 'judge 未产出结论' };
}
