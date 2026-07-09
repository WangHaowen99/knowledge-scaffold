/**
 * LLM 端点连通性自检：用当前 env 配置跑一次最小 judge 回路
 * （pi agent + 工具调用 + 结构化产出），验证真实端点可用。
 * 用法：set -a && source .env && set +a && npx tsx scripts/check-llm.ts
 * 注意：只输出状态与判定结果，不回显任何配置值。
 */
import { config } from '../src/config.js';
import { judge } from '../src/evaluation/judge.js';

async function main() {
  if (!config.llm.baseUrl || (config.llm.baseUrl !== 'faux' && !config.llm.model)) {
    console.error('✗ 未配置 MODELING_LLM_BASE_URL / MODELING_LLM_MODEL');
    process.exit(1);
  }
  console.log(`模式：${config.llm.baseUrl === 'faux' ? 'faux 演示' : '真实端点'}`);
  const t0 = Date.now();
  const v = await judge(
    'STRUCTURE_LIST 中零件 A001 有多少行记录？',
    '共 2 行记录',
    '查询 STRUCTURE_LIST 表可知，零件 A001 共有 2 行记录。',
  );
  const ms = Date.now() - t0;
  const okPositive = v.verdict === 'pass';
  console.log(`${okPositive ? '✓' : '✗'} judge 回路（应判 pass）：verdict=${v.verdict}（${ms}ms）`);
  if (!okPositive) process.exit(1);

  const v2 = await judge(
    'CHANGE_ORDER 审批流程是什么？',
    '需经工艺、质量双会签',
    '抱歉，根据现有知识无法回答该问题。',
  );
  const okNegative = v2.verdict !== 'pass';
  console.log(`${okNegative ? '✓' : '✗'} judge 回路（应判非 pass）：verdict=${v2.verdict}`);
  process.exit(okNegative ? 0 : 1);
}

main().catch((e) => { console.error('✗ LLM 端点自检失败：', e?.message ?? e); process.exit(1); });
