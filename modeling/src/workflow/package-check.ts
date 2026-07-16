import { Type } from '@earendil-works/pi-ai';
import { getDb, nowIso } from '../db/index.js';
import { buildLlm } from '../llm/provider.js';
import { makeAgent, runAgent } from './agent.js';
import { findObjectDataMappingProblems } from './data-mapping.js';
import { validateKey } from './keys.js';
import {
  mappingFieldName,
  mappingObjectName,
  mappingPrimaryNames,
} from './mapping-contract.js';
import { parseObjectIndex } from './object-index.js';
import type { PackageFiles } from './package-render.js';
import type { DataDictTable, PackageSpec, Project } from '../types.js';

// 包体检（知识包完善阶段）双层：
// ① spec 语义层——规则引擎（确定性，代码直查）+ 语义检查（LLM agent）。
//    规则集基于既有知识包检查实践整理，
//    rule-check / semantic-check / coverage-check agent，按本工具的 objects/operations 格式适配。
// ② 渲染产物层（runFormatChecks）——对最终 files（含 override 后内容）校验包格式：
//    必选文件、frontmatter、索引-详情一致性、manifest/mappings 结构、key 合法性。
//    override 写入与导出前都过这层（routes.ts），人工改坏的包不再原样导出。

export type Severity = '错误' | '警告' | '提示';
export interface CheckFinding {
  id: string;
  source: 'rule' | 'semantic' | 'format';
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
  const ruleKeys = new Set((spec.rules ?? []).map((r) => r.key));
  const metricKeys = new Set((spec.metrics ?? []).map((m) => m.key));

  for (const problem of findObjectDataMappingProblems(spec, tables)) {
    add(
      '错误',
      '数据映射失效',
      problem.objectKey,
      problem.message,
      `修正对象 ${problem.objectKey} 的来源表、字段或主键映射`,
    );
  }

  for (const o of spec.objects) {
    if (!o.fields?.length) add('错误', '对象不完整', o.key, `对象「${o.title || o.key}」没有任何字段`, `为对象 ${o.key} 补充字段清单`);
    if ((o.description ?? '').trim().length < 10) add('提示', '描述过简', o.key, `对象「${o.title || o.key}」描述过短，可能造成消费歧义`, `为对象 ${o.key} 补充业务描述（用途、口径、边界）`);
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
    for (const rk of op.usesRules ?? []) {
      if (!ruleKeys.has(rk)) add('错误', '悬空引用', op.key, `操作「${op.title || op.key}」引用了不存在的规则 ${rk}`, `修正操作 ${op.key} 的 usesRules（改为已声明的规则或补建规则）`);
    }
    for (const mk of op.usesMetrics ?? []) {
      if (!metricKeys.has(mk)) add('错误', '悬空引用', op.key, `操作「${op.title || op.key}」引用了不存在的指标 ${mk}`, `修正操作 ${op.key} 的 usesMetrics（改为已声明的指标或补建指标）`);
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

  // rules / metrics（可选目录启用时）：必需正文缺失、悬空引用
  for (const r of spec.rules ?? []) {
    if (!(r.content ?? '').trim()) add('错误', '规则不完整', r.key, `规则「${r.title || r.key}」缺少规则内容（content）`, `为规则 ${r.key} 补充完整的判断口径`);
    for (const ok of r.appliesToObjects ?? []) {
      if (!objKeys.has(ok)) add('警告', '悬空引用', r.key, `规则「${r.title || r.key}」的适用对象 ${ok} 不存在`, `修正规则 ${r.key} 的适用对象`);
    }
  }
  for (const m of spec.metrics ?? []) {
    if (!(m.caliber ?? '').trim()) add('错误', '指标不完整', m.key, `指标「${m.title || m.key}」缺少业务口径（caliber）`, `为指标 ${m.key} 补充口径公式`);
    if (!(m.boundaries ?? []).length) add('错误', '指标不完整', m.key, `指标「${m.title || m.key}」缺少计算边界（boundaries）`, `为指标 ${m.key} 补充统计范围与不计入项`);
    for (const ok of m.usesObjects ?? []) {
      if (!objKeys.has(ok)) add('警告', '悬空引用', m.key, `指标「${m.title || m.key}」引用的对象 ${ok} 不存在`, `修正指标 ${m.key} 的 usesObjects`);
    }
  }

  for (const q of selectedQuestions) {
    const covered = spec.operations.some((op) => op.competencyQuestion &&
      (op.competencyQuestion === q || q.includes(op.competencyQuestion) || op.competencyQuestion.includes(q)));
    if (!covered) {
      add('警告', '领域问题未覆盖', 'index.md',
        `选中的领域问题「${q.slice(0, 40)}…」没有任何操作认领`,
        `为领域问题「${q}」新增一个操作认领（competencyQuestion 填问题原文，flow 写清回答该问题的取数与计算步骤），或指派给现有最相关的操作`);
    }
  }
  return out;
}

// ---- 渲染产物层格式校验（对最终 files——含 override 后内容） ----
// 覆盖：必选文件存在、全部 .md frontmatter 完整（resource/key/title 且 resource 与路径匹配）、
// 索引声明的详情文件存在、manifest 可解析且必选字段齐、mappings JSON 结构合法、key 合法性。
// 纯渲染产物由构造保证通过；这层的目标是拦截 override 改坏的内容（routes 写入与导出前调用）。

const REQUIRED_FILES = ['manifest.yaml', 'index.md', 'objects/index.md', 'operations/index.md'];
const MANIFEST_REQUIRED = ['kind', 'schema_version', 'id', 'name', 'description', 'version'];
// index 文件 → 期望 resource；详情目录 → 期望 resource
const INDEX_RESOURCE: Record<string, string> = {
  'index.md': 'root', 'objects/index.md': 'object_index', 'operations/index.md': 'operation_index',
  'rules/index.md': 'rule_index', 'metrics/index.md': 'metric_index', 'scripts/index.md': 'script_index',
};
const DETAIL_RESOURCE: Record<string, string> = {
  objects: 'object', operations: 'operation', rules: 'rule', metrics: 'metric', scripts: 'script',
};
// 索引文件 → 其表格声明的详情所在目录
const INDEX_DETAIL_DIR: [string, string][] = [
  ['objects/index.md', 'objects'], ['operations/index.md', 'operations'],
  ['rules/index.md', 'rules'], ['metrics/index.md', 'metrics'], ['scripts/index.md', 'scripts'],
];

// 解析 frontmatter 顶层扁平字段（嵌套块的子行带缩进，不会被匹配——够用于 resource/key/title 校验）
function parseFrontmatter(md: string): Record<string, string> | null {
  if (!md.startsWith('---\n')) return null;
  const end = md.indexOf('\n---', 4);
  if (end < 0) return null;
  const out: Record<string, string> = {};
  for (const line of md.slice(4, end).split('\n')) {
    const m = line.match(/^([A-Za-z_][\w-]*):\s*(.*)$/);
    if (!m) continue;
    let v = m[2].trim();
    if (v.startsWith('"')) { try { v = JSON.parse(v); } catch { /* 保留原文 */ } }
    out[m[1]] = v;
  }
  return out;
}

// 提取 markdown 索引表格的第一列（跳过表头与分隔行、以及「—」占位）
function tableFirstColumn(md: string): string[] {
  const keys: string[] = [];
  let headerSeen = false;
  for (const line of md.split('\n')) {
    const t = line.trim();
    if (!t.startsWith('|')) continue;
    const first = (t.split('|').map((c) => c.trim())[1] ?? '');
    if (/^:?-{3,}:?$/.test(first)) { headerSeen = true; continue; }
    if (!headerSeen || !first || first === '—') continue;
    keys.push(first.replace(/\\\|/g, '|'));
  }
  return keys;
}

export function runFormatChecks(spec: PackageSpec, files: PackageFiles): CheckFinding[] {
  const out: CheckFinding[] = [];
  const add = (severity: Severity, category: string, target: string, message: string, suggestion?: string) =>
    out.push({ id: fid(), source: 'format', severity, category, target, message, suggestion });

  // 1) 必选文件存在且非空（规范 §2.1）
  for (const p of REQUIRED_FILES) {
    if (!(p in files) || !files[p].trim()) add('错误', '必选文件缺失', p, `必选文件 ${p} 缺失或为空，本体包不能被稳定消费`);
  }
  for (const o of spec.objects ?? []) {
    if (!(`objects/${o.key}.md` in files)) add('错误', '必选文件缺失', `objects/${o.key}.md`, `对象 ${o.key} 的详情文件缺失`);
  }
  for (const op of spec.operations ?? []) {
    if (!(`operations/${op.key}.md` in files)) add('错误', '必选文件缺失', `operations/${op.key}.md`, `操作 ${op.key} 的详情文件缺失`);
  }

  // 2) manifest 可解析且必选字段齐（规范 §6）
  const mani = files['manifest.yaml'];
  if (mani?.trim()) {
    const kv: Record<string, string> = {};
    for (const line of mani.split('\n')) {
      const m = line.match(/^([A-Za-z_][\w-]*):\s*(.*)$/);
      if (!m) continue;
      let v = m[2].trim();
      if (v.startsWith('"')) { try { v = JSON.parse(v); } catch { /* 保留原文 */ } }
      kv[m[1]] = v;
    }
    if (kv.kind !== 'ontology-package') add('错误', 'manifest 非法', 'manifest.yaml', `manifest 的 kind 必须为 ontology-package（当前：${kv.kind ?? '缺失'}）`);
    for (const f of MANIFEST_REQUIRED) {
      if (!kv[f]) add('错误', 'manifest 非法', 'manifest.yaml', `manifest 缺少必选字段 ${f}`);
    }
  }

  // 3) 全部 .md：frontmatter 完整、resource 与路径匹配、key 合法
  for (const [path, content] of Object.entries(files)) {
    if (!path.endsWith('.md')) continue;
    const fm = parseFrontmatter(content);
    if (!fm) { add('错误', 'frontmatter 缺失', path, `${path} 缺少 frontmatter（--- 包裹的 resource/key/title），Agent 无法按 resource+key 消费`); continue; }
    for (const k of ['resource', 'key', 'title']) {
      if (!fm[k]) add('错误', 'frontmatter 不完整', path, `${path} 的 frontmatter 缺少 ${k} 字段`);
    }
    const expected = INDEX_RESOURCE[path] ?? DETAIL_RESOURCE[path.split('/')[0]];
    if (expected && fm.resource && fm.resource !== expected) {
      add('错误', 'resource 声明不符', path, `${path} 的 resource 应为 ${expected}（当前：${fm.resource}）`);
    }
    if (fm.key) {
      try { validateKey(fm.key, `${path} 的`); } catch (e) { add('错误', 'key 非法', path, e instanceof Error ? e.message : String(e)); }
    }
  }

  // 4) 索引声明的详情文件必须存在（规范 §2.2）
  for (const [idxPath, dir] of INDEX_DETAIL_DIR) {
    const content = files[idxPath];
    if (!content) continue;
    for (const key of tableFirstColumn(content)) {
      if (!(`${dir}/${key}.md` in files)) {
        add('错误', '索引与详情不一致', idxPath, `${idxPath} 声明了「${key}」，但详情文件 ${dir}/${key}.md 不存在`);
      }
    }
  }

  const objectIndex = files['objects/index.md'];
  if (objectIndex?.trim()) {
    try {
      const entries = parseObjectIndex(objectIndex);
      for (const object of spec.objects ?? []) {
        if (!entries.has(object.key)) {
          add('错误', '对象索引非法', 'objects/index.md', `objects/index.md 缺少对象 ${object.key} 及其来源 Sheet`);
        }
      }
    } catch (error) {
      add('错误', '对象索引非法', 'objects/index.md', error instanceof Error ? error.message : String(error));
    }
  }

  // 5) mappings JSON 结构合法（规范 §18）
  const mp = files['mappings/object-data-mappings.json'];
  if (mp !== undefined) {
    try {
      const j = JSON.parse(mp);
      if (!j.schema_version) add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', 'mapping 缺少 schema_version');
      if (!j.default_provider_alias) add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', 'mapping 缺少 default_provider_alias');
      if (!j.objects || typeof j.objects !== 'object' || Array.isArray(j.objects)) {
        add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', 'mapping 缺少 objects 映射（以 object key 为键）');
      } else {
        const specObjects = new Map((spec.objects ?? []).map((object) => [mappingObjectName(object), object]));
        for (const object of spec.objects ?? []) {
          const objectKey = mappingObjectName(object);
          if (object.sourceTable && !Object.hasOwn(j.objects, objectKey)) {
            add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `对象 ${object.key} 声明了 sourceTable「${object.sourceTable}」，但最终 mapping 中缺少 object key ${objectKey}`);
          }
        }
        for (const [objectKey, v] of Object.entries<any>(j.objects)) {
          if (!v || typeof v !== 'object') { add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `mapping 的 objects.${objectKey} 不是对象`); continue; }
          if (!v.provider_alias) add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `mapping 的 objects.${objectKey} 缺少 provider_alias`);
          if (!v.resource) add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `mapping 的 objects.${objectKey} 缺少 resource`);
          if (!Array.isArray(v.primary_key)) add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `mapping 的 objects.${objectKey} 的 primary_key 必须是数组`);
          if (!v.fields || typeof v.fields !== 'object') add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `mapping 的 objects.${objectKey} 缺少 fields`);

          const object = specObjects.get(objectKey);
          if (!object) {
            add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `最终 mapping 声明了规格中不存在的 object key ${objectKey}`);
            continue;
          }
          if (!object.sourceTable) {
            add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `对象 ${object.key} 没有 sourceTable，属于概念实体，不得声明数据 mapping`);
            continue;
          }
          if (v.resource && v.resource !== object.key) {
            add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `对象 ${object.key} 的 mapping resource 应为 ${object.key}（当前：${v.resource}）`);
          }

          const expectedPrimary = mappingPrimaryNames(object);
          if (Array.isArray(v.primary_key) && JSON.stringify(v.primary_key) !== JSON.stringify(expectedPrimary)) {
            add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `对象 ${object.key} 的 primary_key 与字段 key 不一致`);
          }
          if (v.fields && typeof v.fields === 'object' && !Array.isArray(v.fields)) {
            const expectedFields = new Map((object.fields ?? []).map((field) => [mappingFieldName(field), field.key]));
            for (const [fieldName, fieldKey] of expectedFields) {
              const fieldMapping = v.fields[fieldName];
              if (!fieldMapping || typeof fieldMapping !== 'object') {
                add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `对象 ${object.key} 的 mapping 缺少字段 key ${fieldName}`);
              } else if (fieldMapping.source !== fieldKey) {
                add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `对象 ${object.key} 字段 ${fieldName} 的 mapping source 应为 ${fieldKey}（当前：${fieldMapping.source ?? '缺失'}）`);
              }
            }
            for (const fieldName of Object.keys(v.fields)) {
              if (!expectedFields.has(fieldName)) {
                add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `对象 ${object.key} 的 mapping 包含对象规格未声明的字段 key ${fieldName}`);
              }
            }
          }
        }
      }
    } catch {
      add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', 'mappings/object-data-mappings.json 不是合法 JSON');
    }
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
