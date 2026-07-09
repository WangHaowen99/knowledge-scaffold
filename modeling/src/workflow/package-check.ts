import { Type } from '@earendil-works/pi-ai';
import { getDb, nowIso } from '../db/index.js';
import { buildLlm } from '../llm/provider.js';
import { makeAgent, runAgent } from './agent.js';
import type { DataDictTable, PackageSpec, Project } from '../types.js';

// 包体检（知识包完善阶段）：规则引擎（确定性，代码直查）+ 语义检查（LLM agent）。
// 规则集基于既有知识包检查实践整理，
// rule-check / semantic-check / coverage-check agent，按本工具的 objects/operations 格式适配。

export type Severity = '错误' | '警告' | '提示';
export interface CheckFinding {
  id: string;
  source: 'rule' | 'semantic';
  severity: Severity;
  category: string;
  target: string;       // 对象/操作 key 或包内路径
  message: string;
  suggestion?: string;  // 可直接交给修缮对话的修复指令
}

let seq = 0;
const fid = () => `f${++seq}_${Math.abs(Date.now() % 100000)}`;

// ---- 规则检查（确定性） ----
export function runRuleChecks(spec: PackageSpec, tables: DataDictTable[], selectedQuestions: string[]): CheckFinding[] {
  const out: CheckFinding[] = [];
  const add = (severity: Severity, category: string, target: string, message: string, suggestion?: string) =>
    out.push({ id: fid(), source: 'rule', severity, category, target, message, suggestion });
  const objKeys = new Set(spec.objects.map((o) => o.key));
  const tableNames = new Set(tables.map((t) => t.name));

  for (const o of spec.objects) {
    if (!o.fields?.length) add('错误', '对象不完整', o.key, `对象「${o.title || o.key}」没有任何字段`, `为对象 ${o.key} 补充字段清单`);
    if ((o.description ?? '').trim().length < 10) add('提示', '描述过简', o.key, `对象「${o.title || o.key}」描述过短，可能造成消费歧义`, `为对象 ${o.key} 补充业务描述（用途、口径、边界）`);
    if (o.sourceTable && tableNames.size && !tableNames.has(o.sourceTable)) {
      add('警告', '数据映射失效', o.key, `对象「${o.title || o.key}」的来源表 ${o.sourceTable} 不在数据字典中（资料可能已变更）`, `修正对象 ${o.key} 的来源表映射`);
    }
    for (const pk of o.primaryKey ?? []) {
      if (!o.fields?.some((f) => f.key === pk)) add('警告', '主键失配', o.key, `对象「${o.title || o.key}」主键 ${pk} 不在字段清单中`);
    }
  }

  const cqOwner = new Map<string, string>();
  for (const op of spec.operations) {
    if (!op.flow?.length) add('错误', '操作不完整', op.key, `操作「${op.title || op.key}」缺少流程描述（flow）`, `为操作 ${op.key} 补充分步流程`);
    if (!op.relatedObjects?.length) add('提示', '关联缺失', op.key, `操作「${op.title || op.key}」未声明关联对象`);
    for (const ro of op.relatedObjects ?? []) {
      if (!objKeys.has(ro)) add('错误', '悬空引用', op.key, `操作「${op.title || op.key}」关联了不存在的对象 ${ro}`, `修正操作 ${op.key} 的关联对象（改为已有对象或补建对象）`);
    }
    const flowText = (op.flow ?? []).join('\n');
    for (const inp of op.inputs ?? []) {
      if (inp.required && !flowText.includes(inp.param) && !(op.description ?? '').includes(inp.param)) {
        add('提示', '输入未使用', op.key, `操作「${op.title || op.key}」的必填输入 ${inp.param} 未在流程中被引用`);
      }
    }
    if (op.competencyQuestion) {
      const k = op.competencyQuestion.trim();
      if (cqOwner.has(k)) {
        add('错误', '典型问题重复认领', op.key,
          `「${k.slice(0, 40)}…」同时被 ${cqOwner.get(k)} 与 ${op.key} 认领——消费端路由会二义，agent 可能走到不含关键口径的那个操作`,
          `保留业务口径最完整的一个操作认领该问题（把另一个操作的 competencyQuestion 改掉或清空），并在被让位的操作描述里加“该问题请走 ${op.key}”的交叉引用`);
      } else cqOwner.set(k, op.key);
    }
  }

  for (const q of selectedQuestions) {
    const covered = spec.operations.some((op) => op.competencyQuestion &&
      (op.competencyQuestion === q || q.includes(op.competencyQuestion) || op.competencyQuestion.includes(q)));
    if (!covered) add('警告', '领域问题未覆盖', 'index.md', `选中的领域问题「${q.slice(0, 40)}…」没有任何操作认领`, `新增或指派一个操作覆盖该问题`);
  }
  return out;
}

// ---- 语义检查（LLM agent，checklist 源自 semantic-check-agent） ----
export async function runSemanticCheck(project: Project, spec: PackageSpec): Promise<CheckFinding[]> {
  const cap: { findings?: Omit<CheckFinding, 'id' | 'source'>[] } = {};
  const submitFindings = {
    name: 'submit_findings',
    label: '提交体检发现',
    description: '提交语义检查发现（没有问题时提交空数组）',
    parameters: Type.Object({
      findings: Type.Array(Type.Object({
        severity: Type.Union([Type.Literal('错误'), Type.Literal('警告'), Type.Literal('提示')]),
        category: Type.String(),
        target: Type.String(),
        message: Type.String(),
        suggestion: Type.Optional(Type.String()),
      })),
    }),
    async execute(_id: string, params: any) {
      cap.findings = params.findings ?? [];
      return { content: [{ type: 'text' as const, text: `已记录 ${params.findings?.length ?? 0} 条发现。` }], details: {}, terminate: true };
    },
  };

  const sys = [
    '你是知识包语义体检员（package_check）。逐项检查规格并用 submit_findings 提交结构化发现。',
    '对每个操作检查：',
    '1) 逻辑步骤完整性——流程是否缺分支、缺异常路径、条件不完整；',
    '2) 输入参数使用——输入表中的参数是否都在流程中被用到；',
    '3) 输出可推导性——输出能否由流程步骤推导出来；',
    '4) 兜底完整性——流程中引用其他操作/查询数据时，结果可能为空/0/失败的（转换型/判断型/数值计算型/查询型四类），是否有显式兜底（跳过/空值传递/估算/默认值）或天然保护（上游已过滤、成熟态数据、纯查询不触发动作）；两者皆无报「警告/兜底缺失」；',
    '5) 空值语义——确定性空值（业务真为零）与未知性空值（未读到/未同步）是否被混同代入计算；混同报「错误/空值语义模糊」；',
    '6) 口径一致性——同一业务量（如成本、周期、数量）在不同操作间的计算口径是否互相矛盾；矛盾报「错误/口径冲突」，并指出冲突双方。',
    '对每个对象检查：字段描述歧义、关联对象是否有字段/外键支撑、业务定义是否自洽。',
    'target 填对象/操作的 key；suggestion 写成可直接执行的修缮指令；确无问题就提交空数组，不要为凑数报告风格偏好。',
    `项目背景：${project.background || project.domain || project.name}`,
  ].join('\n');

  const body = ['【当前规格】', JSON.stringify({ objects: spec.objects, operations: spec.operations }), '请完成语义体检并提交发现。'].join('\n');
  const agent = makeAgent(buildLlm(), sys, [submitFindings] as any);
  await runAgent(agent, body);
  return (cap.findings ?? []).map((f) => ({ ...f, id: fid(), source: 'semantic' as const }));
}

// ---- 结果存取（最近一次） ----
export function saveCheck(projectId: string, version: string, findings: CheckFinding[]): void {
  getDb().prepare(`INSERT INTO package_checks (project_id, version, findings_json, created_at) VALUES (?,?,?,?)
    ON CONFLICT(project_id) DO UPDATE SET version = excluded.version, findings_json = excluded.findings_json, created_at = excluded.created_at`)
    .run(projectId, version, JSON.stringify(findings), nowIso());
}
export function getCheck(projectId: string): { version: string; findings: CheckFinding[]; createdAt: string } | null {
  const r = getDb().prepare('SELECT * FROM package_checks WHERE project_id = ?').get(projectId) as any;
  return r ? { version: r.version, findings: JSON.parse(r.findings_json), createdAt: r.created_at } : null;
}
