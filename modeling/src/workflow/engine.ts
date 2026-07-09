import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { NodeStatus, NodeType, WorkflowNode, DataDictTable, AnalysisResult } from '../types.js';
import { buildLlm, type LlmDeps } from '../llm/provider.js';
import { buildTools, type ToolContext } from './tools.js';
import { makeAgent, runAgent } from './agent.js';

const TITLES: Record<NodeType, string> = {
  project_setup: '项目创建', ingest: '资料上传与解析', analyze: '初步分析',
  interview: '建模访谈', competency: '胜任力问题', generate: '知识包生成',
  review: '包评审', testcases: '评测用例录入', evaluate: '评测运行', optimize: '优化建议与应用',
  dyn_followup: '追问访谈', dyn_deepread: '深读文档', dyn_probe: '数据探查', dyn_conflict: '冲突消解',
};

function rowToNode(r: any): WorkflowNode {
  return {
    id: r.id, projectId: r.project_id, type: r.type, status: r.status,
    ordinal: r.ordinal, title: r.title, reason: r.reason ?? undefined,
    agentMessages: r.agent_messages ? JSON.parse(r.agent_messages) : undefined,
    output: r.output_json ? JSON.parse(r.output_json) : undefined,
    createdAt: r.created_at, updatedAt: r.updated_at,
  };
}

export function listNodes(projectId: string): WorkflowNode[] {
  const db = getDb();
  const rows = db.prepare('SELECT * FROM wf_nodes WHERE project_id = ? ORDER BY ordinal').all(projectId);
  return rows.map(rowToNode);
}

export function getNode(id: string): WorkflowNode | null {
  const db = getDb();
  const r = db.prepare('SELECT * FROM wf_nodes WHERE id = ?').get(id);
  return r ? rowToNode(r as any) : null;
}

export function upsertNode(node: {
  projectId: string; type: NodeType; status?: NodeStatus; ordinal: number;
  reason?: string; agentMessages?: unknown[]; output?: unknown; id?: string;
}): WorkflowNode {
  const db = getDb();
  const now = nowIso();
  const id = node.id ?? `nd_${randomUUID().slice(0, 12)}`;
  const existing = node.id ? getNode(node.id) : null;
  if (existing) {
    db.prepare(`UPDATE wf_nodes SET status=?, reason=?, agent_messages=?, output_json=?, updated_at=? WHERE id=?`)
      .run(node.status ?? existing.status, node.reason ?? existing.reason ?? null,
        node.agentMessages ? JSON.stringify(node.agentMessages) : (existing.agentMessages ? JSON.stringify(existing.agentMessages) : null),
        node.output ? JSON.stringify(node.output) : (existing.output ? JSON.stringify(existing.output) : null),
        now, id);
  } else {
    db.prepare(`INSERT INTO wf_nodes (id, project_id, type, status, ordinal, title, reason, agent_messages, output_json, created_at, updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?)`)
      .run(id, node.projectId, node.type, node.status ?? 'pending', node.ordinal, TITLES[node.type],
        node.reason ?? null, node.agentMessages ? JSON.stringify(node.agentMessages) : null,
        node.output ? JSON.stringify(node.output) : null, now, now);
  }
  return getNode(id)!;
}

export function nextOrdinal(projectId: string): number {
  const db = getDb();
  const r = db.prepare('SELECT MAX(ordinal) AS m FROM wf_nodes WHERE project_id = ?').get(projectId) as { m: number | null };
  return (r.m ?? -1) + 1;
}

// 节点 3：初步分析。跑一次 pi agent run，让其读数据字典/文档，产出结构化分析。
// llmOverride 供冒烟注入 faux。
export async function runAnalyze(
  projectId: string,
  tables: DataDictTable[],
  documents: { filename: string; textContent?: string }[],
  llmOverride?: LlmDeps,
): Promise<{ node: WorkflowNode; analysis: AnalysisResult }> {
  const llm = llmOverride ?? buildLlm();
  const captured: Record<string, unknown> = {};
  const ctx: ToolContext = { tables, documents, captured };
  const tools = buildTools(ctx);

  const systemPrompt = [
    '你是企业知识工程建模分析助手。基于已解析的数据表与文档，完成初步分析：',
    '1) 用 list_tables / describe_table / list_documents / read_document 探查资料；',
    '2) 归纳领域概览；识别候选业务对象（对应哪些表）；',
    '3) 列出需要向领域专家澄清的开放问题（对象划分/字段含义/关系/口径）；',
    '4) 最后调用 submit_analysis 提交结构化结果。',
  ].join('\n');

  const node = upsertNode({ projectId, type: 'analyze', status: 'running', ordinal: nextOrdinal(projectId) });

  const agent = makeAgent(llm, systemPrompt, tools);
  const messages = await runAgent(agent, '请对已上传的资料做初步分析，完成后调用 submit_analysis。');

  const analysis = (captured.analysis as AnalysisResult) ?? {
    domainOverview: '(agent 未提交结构化分析)',
    candidateObjects: [], openQuestions: [],
  };

  const done = upsertNode({
    id: node.id, projectId, type: 'analyze', status: 'done', ordinal: node.ordinal,
    agentMessages: messages, output: analysis,
  });
  return { node: done, analysis };
}
