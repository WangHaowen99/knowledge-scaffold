import { createHash } from 'node:crypto';

import { getDb, nowIso } from '../db/index.js';
import { findObjectDataMappingProblems } from './data-mapping.js';
import { OBJECT_DATA_MAPPING_PATH, packageNeedsObjectDataMapping } from './schema-object-policy.js';
import { describeFieldNameConflict, findFieldNameConflicts } from './field-alias.js';
import { validateKey } from './keys.js';
import {
  mappingFieldName,
  mappingObjectName,
  mappingPrimaryNames,
} from './mapping-contract.js';
import {
  describeObjectAliasConflict,
  findObjectAliasConflicts,
} from './object-alias-validation.js';
import { parseObjectIndex } from './object-index.js';
import {
  PACKAGE_DETAIL_RESOURCES,
  PACKAGE_INDEX_DETAIL_DIRECTORIES,
  PACKAGE_INDEX_RESOURCES,
  PACKAGE_REQUIRED_FILES,
  packageManifestIssues,
  parsePackageManifest,
} from './package-contract.js';
import { packageMappingResource } from './package-data-source.js';
import { validateMermaidSource } from './mermaid-source-contract.js';
import { sourceRefsFor, tableSourceFor, tableSourcesFor } from './source-references.js';
import { duplicateScriptEntrypoints, normalizeCompetencyQuestion, questionsMatch } from './spec-validation.js';
import type { PackageFiles } from './package-render.js';
import { validateGeneratedScriptSafety } from './script-safety.js';
import type { DataDictTable, PackageSpec } from '../types.js';

// 包体检（知识包完善阶段）只执行确定性检查：
// ① spec 规则层——引用、来源映射、主键、输入使用和领域问题覆盖等代码规则。
// ② 渲染产物层（runFormatChecks）——对最终 files（含 override 后内容）校验包格式：
//    必选文件、frontmatter、索引-详情一致性、manifest/mappings 结构、key 合法性。
//    override 写入与导出前都过这层（routes.ts），人工改坏的包不再原样导出。

export type Severity = '错误' | '警告' | '提示';
export interface CheckFinding {
  id: string;
  /** 跨校验修订稳定的问题身份；旧记录可能没有该字段。 */
  fingerprint?: string;
  source: 'rule' | 'semantic' | 'format';
  severity: Severity;
  category: string;
  target: string;       // 对象/操作 key 或包内路径
  message: string;
  suggestion?: string;  // 可直接交给修缮对话的修复指令
  /** 面向业务专家的通俗说明，不替代内部技术信息。 */
  userMessage?: string;
  /** 面向业务专家的下一步提示。 */
  userSuggestion?: string;
  /** 专家可直接选择的通俗方案；decision 保留系统执行所需细节。 */
  expertOptions?: Array<{ label: string; description: string; decision: string }>;
  /** 必须实际修改的资源。对照引用不得列入，受控修复据此建立显式变更边界。 */
  repairTargets?: Array<{
    targetKind: 'object' | 'operation' | 'rule' | 'metric' | 'script';
    targetKey: string;
    instruction: string;
  }>;
  /** 发布时必须修复，不能作为已知限制带入发布。 */
  releaseBlocking?: boolean;
  /** 稳定错误码和处置类别由校验器直接给出；旧记录缺失时由修复层兼容推断。 */
  code?: string;
  disposition?: 'automatic' | 'approval_required' | 'expert_decision'
    | 'external_data_required' | 'retryable_infrastructure';
  path?: string;
  facts?: Record<string, unknown>;
  impact?: string;
}

function mappingIdentifier(value: unknown): string {
  return String(value ?? '').trim().normalize('NFC');
}

function normalizedMappingIdentifier(value: unknown): string {
  const text = mappingIdentifier(value);
  return /^[\x00-\x7F]*$/.test(text) ? text.toLowerCase() : text;
}

function hasUnsafeMappingText(value: string): boolean {
  return /[\r\n\u2028\u2029\p{Cc}]/u.test(value);
}

function normalizeFindingIdentityPart(value: string): string {
  return value.normalize('NFKC').trim().replace(/\s+/gu, ' ');
}

function normalizeFindingCategory(value: string): string {
  const category = normalizeFindingIdentityPart(value);
  if (/口径/u.test(category)) return '口径';
  if (/兜底/u.test(category)) return '兜底';
  if (/(?:无证据|证据与假设|证据边界)/u.test(category)) return '证据边界';
  if (/(?:输入参数使用|逻辑步骤完整性)/u.test(category)) return '逻辑完整性';
  return category;
}

type FindingResourceKind = 'object' | 'operation' | 'rule' | 'metric' | 'script' | 'package';

function findingResourceKind(value: string): FindingResourceKind | null {
  const normalized = value.toLocaleLowerCase('en-US').replace(/s$/u, '');
  return ['object', 'operation', 'rule', 'metric', 'script', 'package'].includes(normalized)
    ? normalized as FindingResourceKind
    : null;
}

function normalizeFindingTarget(value: string, category: string): string {
  let target = normalizeFindingIdentityPart(value);
  const indexed = target.match(/^(?:objects?|operations?|rules?|metrics?|scripts?)\[\d+\](?:\((.+)\))?$/iu);
  const indexedKind = target.match(/^([a-z]+)\[/iu)?.[1];
  if (indexed?.[1] && indexedKind) {
    return `${findingResourceKind(indexedKind) ?? 'resource'}:${indexed[1].trim()}`;
  }
  target = target.replace(/\.md$/iu, '');
  const typed = target.match(/^(objects?|operations?|rules?|metrics?|scripts?|package)[/:](.+)$/iu);
  if (typed) {
    return `${findingResourceKind(typed[1]) ?? 'resource'}:${typed[2].split('/').filter(Boolean).at(-1)?.trim() ?? typed[2].trim()}`;
  }
  const path = target.split('/').filter(Boolean);
  const key = (path.at(-1) ?? target).trim();
  const normalizedCategory = normalizeFindingCategory(category);
  const inferredKind = normalizedCategory === '口径' || /指标/u.test(normalizedCategory)
    ? 'metric'
    : null;
  return `${inferredKind ?? 'resource'}:${key}`;
}

function normalizeFindingInvariant(
  finding: Pick<CheckFinding, 'category'> & Partial<Pick<CheckFinding, 'message'>>,
): string {
  const text = normalizeFindingIdentityPart([
    finding.category,
    finding.message ?? '',
  ].join(' '));
  const dimensions: string[] = [];
  if (normalizeFindingCategory(finding.category) === '口径'
    || /(?:\bcaliber\b|(?:业务|计算|指标)口径|口径(?:说明|定义|公式))/iu.test(text)) {
    dimensions.push('caliber');
  }
  if (/(?:\bboundar(?:y|ies)\b|计算边界|统计范围|不计入项)/iu.test(text)) dimensions.push('boundaries');
  return dimensions.sort().join('+') || 'general';
}

/**
 * finding 的稳定身份由检查来源、问题类别、资源范围和稳定不变量维度决定。
 * 完整消息、建议、严重级别与临时 id 都是一次检查的呈现结果，不参与身份，
 * 因而轻微改写不会变成新问题，而同一资源的口径与边界仍是两个问题。
 */
export function checkFindingFingerprint(
  finding: Pick<CheckFinding, 'source' | 'category' | 'target'>
    & Partial<Pick<CheckFinding, 'message' | 'suggestion'>>,
): string {
  const digest = createHash('sha256').update(JSON.stringify({
    source: finding.source,
    category: normalizeFindingCategory(finding.category),
    target: normalizeFindingTarget(finding.target, finding.category),
    invariant: normalizeFindingInvariant(finding),
  })).digest('hex');
  return `check:${digest}`;
}

function createCheckFinding(finding: Omit<CheckFinding, 'fingerprint'>): CheckFinding {
  return { ...finding, fingerprint: checkFindingFingerprint(finding) };
}

export function withCheckFindingFingerprint(finding: CheckFinding): CheckFinding {
  return { ...finding, fingerprint: checkFindingFingerprint(finding) };
}

let seq = 0;
const fid = () => `f${++seq}_${Math.abs(Date.now() % 100000)}`;

// ---- 规则检查（确定性） ----
export interface RuleCheckOptions {
  /** 导入型知识包没有建模资料表，映射应在数据源连接中验证。 */
  validateDataMappings?: boolean;
}

export function runRuleChecks(
  spec: PackageSpec,
  tables: DataDictTable[],
  selectedQuestions: string[],
  options: RuleCheckOptions = {},
): CheckFinding[] {
  const out: CheckFinding[] = [];
  const add = (severity: Severity, category: string, target: string, message: string, suggestion?: string) =>
    out.push(createCheckFinding({
      id: fid(), source: 'rule', severity, category, target, message, suggestion,
    }));
  const objKeys = new Set(spec.objects.map((o) => o.key));
  const opKeys = new Set(spec.operations.map((operation) => operation.key));
  const ruleKeys = new Set((spec.rules ?? []).map((r) => r.key));
  const metricKeys = new Set((spec.metrics ?? []).map((m) => m.key));

  for (const conflict of findObjectAliasConflicts(spec.objects)) {
    const objectKeys = conflict.kind === 'shared_alias'
      ? conflict.objectKeys
      : [...conflict.aliasObjectKeys, conflict.identityObjectKey];
    add(
      '错误',
      '对象别名冲突',
      objectKeys.join('、'),
      describeObjectAliasConflict(conflict),
      '为冲突对象保留含义明确且互不重复的别名；若对象语义实际相同，请先合并对象',
    );
  }

  if (options.validateDataMappings !== false) {
    for (const problem of findObjectDataMappingProblems(spec, tables)) {
      add(
        '错误',
        '数据映射失效',
        problem.objectKey,
        problem.message,
        `修正对象 ${problem.objectKey} 的来源表、字段或主键映射`,
      );
    }
  } else if (spec.objects.some((object) => (
    Boolean(object.sourceTable)
    || object.sourceRefs?.some((source) => source.kind === 'table')
  ))) {
    add(
      '警告',
      '数据映射待验证',
      'mappings/object-data-mappings.json',
      '导入包的数据映射未与建模资料表比对，请在数据源页配置并测试实际连接',
    );
  }

  for (const o of spec.objects) {
    if (!sourceRefsFor(o).length) {
      add('警告', '来源待确认', o.key,
        `对象「${o.title || o.key}」没有绑定 Sheet 或文档，可能是外部输入或专家定义对象`,
        `请专家确认对象 ${o.key} 是否为运行时输入；如需查数，再补充来源表或文档`);
    }
    if (tableSourcesFor(o).length > 1) {
      add('警告', '多表映射待确认', o.key,
        `对象「${o.title || o.key}」同时引用 ${tableSourcesFor(o).map((source) => source.table).join('、')}，字段按来源表并集校验`,
        `请专家确认对象 ${o.key} 是否应拆分，或在数据源绑定阶段核对多表关联关系`);
    }
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
      const k = normalizeCompetencyQuestion(op.competencyQuestion);
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
    for (const operationKey of r.appliesToOperations ?? []) {
      if (!opKeys.has(operationKey)) add('错误', '悬空引用', r.key, `规则「${r.title || r.key}」的适用操作 ${operationKey} 不存在`, `修正规则 ${r.key} 的适用操作`);
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
    const covered = spec.operations.some((op) => questionsMatch(op.competencyQuestion, q));
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
  const add = (
    severity: Severity,
    category: string,
    target: string,
    message: string,
    suggestion?: string,
    releaseBlocking = false,
  ) => out.push(createCheckFinding({
    id: fid(), source: 'format', severity, category, target, message, suggestion,
    ...(releaseBlocking ? { releaseBlocking: true } : {}),
  }));

  for (const conflict of findFieldNameConflicts(spec.objects ?? [])) {
    add(
      '错误',
      '字段名称冲突',
      conflict.path,
      describeFieldNameConflict(conflict),
      `修正对象 ${conflict.objectKey} 中冲突字段的真实别名；无别名时留空`,
      true,
    );
  }

  // 存量知识包必须保持可浏览，但无效脚本仍要成为可定位、可受控修复的确定性阻断项。
  for (const conflict of duplicateScriptEntrypoints(spec)) {
    for (const scriptKey of conflict.scriptKeys) {
      add(
        '错误',
        '脚本入口冲突',
        `script:${scriptKey}`,
        `脚本入口文件 ${conflict.entrypoint} 被 ${conflict.scriptKeys.join('、')} 共同使用，渲染和执行时会互相覆盖`,
        `将 script ${scriptKey} 的 entrypoint 改为与脚本职责对应的唯一 Python 文件名，并同步保留其 operation 绑定、参数、输出和实现逻辑；不得修改无关业务知识。`,
        true,
      );
    }
  }
  for (const script of spec.scripts ?? []) {
    try {
      validateGeneratedScriptSafety(script);
    } catch (error) {
      add(
        '错误',
        '脚本执行契约',
        script.key,
        error instanceof Error ? error.message : String(error),
        `修正 script ${script.key} 的 sourceCode，使其符合 DataProviderClient SDK、结构化输入输出和安全执行契约；不得修改无关业务知识。`,
        true,
      );
    }
  }

  // 1) 必选文件存在且非空（规范 §2.1）
  for (const p of PACKAGE_REQUIRED_FILES) {
    if (!(p in files) || !files[p].trim()) add('错误', '必选文件缺失', p, `必选文件 ${p} 缺失或为空，本体包不能被稳定消费`);
  }
  const mappingRequired = packageNeedsObjectDataMapping(spec);
  const mappingContent = files[OBJECT_DATA_MAPPING_PATH];
  if (mappingRequired && !mappingContent?.trim()) {
    add('错误', '必选文件缺失', OBJECT_DATA_MAPPING_PATH, '存在物理对象绑定时必须生成 object data mapping 文件');
  }
  if (!mappingRequired && mappingContent !== undefined) {
    add('错误', 'mapping 非法', OBJECT_DATA_MAPPING_PATH, '没有物理对象绑定时不得生成 object data mapping 文件');
  }
  for (const o of spec.objects ?? []) {
    if (!(`objects/${o.key}.md` in files)) add('错误', '必选文件缺失', `objects/${o.key}.md`, `对象 ${o.key} 的详情文件缺失`);
  }
  for (const op of spec.operations ?? []) {
    if (!(`operations/${op.key}.md` in files)) add('错误', '必选文件缺失', `operations/${op.key}.md`, `操作 ${op.key} 的详情文件缺失`);
  }

  for (const [path, content] of Object.entries(files)) {
    for (const issue of validateMermaidSource(path, content)) {
      out.push(createCheckFinding({
        id: fid(),
        source: 'format',
        severity: '错误',
        category: 'Mermaid 源码格式',
        target: path,
        path,
        code: issue.ruleId,
        message: issue.message,
        suggestion: issue.suggestion,
      }));
    }
  }

  // 2) manifest 可解析且必选字段齐（规范 §6）
  const mani = files['manifest.yaml'];
  if (mani?.trim()) {
    try {
      for (const issue of packageManifestIssues(parsePackageManifest(mani))) {
        add(
          '错误',
          'manifest 非法',
          'manifest.yaml',
          issue.message,
          issue.suggestion,
        );
      }
    } catch (error) {
      add(
        '错误',
        'manifest 非法',
        'manifest.yaml',
        error instanceof Error ? error.message : 'manifest.yaml 不是合法 YAML',
        '按知识包生成契约修正 manifest.yaml 的 YAML 结构',
      );
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
    const expected = PACKAGE_INDEX_RESOURCES[path] ?? PACKAGE_DETAIL_RESOURCES[path.split('/')[0]];
    if (expected && fm.resource && fm.resource !== expected) {
      add('错误', 'resource 声明不符', path, `${path} 的 resource 应为 ${expected}（当前：${fm.resource}）`);
    }
    if (fm.key) {
      try { validateKey(fm.key, `${path} 的`); } catch (e) { add('错误', 'key 非法', path, e instanceof Error ? e.message : String(e)); }
    }
  }

  // 4) 索引声明的详情文件必须存在（规范 §2.2）
  for (const [idxPath, dir] of PACKAGE_INDEX_DETAIL_DIRECTORIES) {
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
          const tableSource = tableSourceFor(object);
          if (tableSource && !Object.hasOwn(j.objects, objectKey)) {
            add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `对象 ${object.key} 声明了来源表「${tableSource.table}」，但最终 mapping 中缺少 object key ${objectKey}`);
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
          if (!tableSourceFor(object)) {
            add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `对象 ${object.key} 没有表格来源，属于概念实体，不得声明数据 mapping`);
            continue;
          }
          const expectedResource = packageMappingResource(spec, object);
          const compatibleResources = new Set([expectedResource]);
          if (!spec.dataSourceType) {
            const legacyTableResource = tableSourceFor(object)?.table;
            if (legacyTableResource) compatibleResources.add(legacyTableResource);
          }
          if (v.resource && !compatibleResources.has(v.resource)) {
            add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `对象 ${object.key} 的 mapping resource 应为 ${expectedResource}（当前：${v.resource}）`);
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

        const valueBindings = j.value_bindings;
        const bindingKeys = new Set<string>();
        if (valueBindings !== undefined) {
          if (!valueBindings || typeof valueBindings !== 'object' || Array.isArray(valueBindings)) {
            add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', 'value_bindings 必须是对象');
          } else {
            const objectIdentifiers = new Set<string>();
            for (const object of spec.objects ?? []) {
              for (const value of [
                object.key,
                object.title,
                ...(object.aliases ?? []),
                `${object.key}.md`,
              ]) objectIdentifiers.add(normalizedMappingIdentifier(value));
            }
            for (const objectKey of Object.keys(j.objects)) objectIdentifiers.add(normalizedMappingIdentifier(objectKey));

            for (const [bindingKey, binding] of Object.entries<any>(valueBindings)) {
              const key = mappingIdentifier(bindingKey);
              const normalizedKey = normalizedMappingIdentifier(key);
              if (!key) add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', 'value_bindings 包含空 binding key');
              if (key.length > 128 || hasUnsafeMappingText(key)) {
                add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `value_bindings.${bindingKey} 的 key 含非法字符或超过 128 字符`);
              }
              if (bindingKeys.has(normalizedKey)) {
                add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `value_bindings.${bindingKey} 与其他 binding key 归一化后重复`);
              }
              bindingKeys.add(normalizedKey);
              if (objectIdentifiers.has(normalizedKey)) {
                add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `value_bindings.${bindingKey} 与 Object 或 mapping key 冲突`);
              }
              if (!binding || typeof binding !== 'object' || Array.isArray(binding)) {
                add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `value_bindings.${bindingKey} 必须是对象`);
                continue;
              }
              for (const [name, max] of [
                ['resource', 256],
                ['value_field', 128],
                ['semantic_field', 128],
              ] as const) {
                const value = mappingIdentifier(binding[name]);
                if (!value) add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `value_bindings.${bindingKey}.${name} 不能为空`);
                if (value.length > max || hasUnsafeMappingText(value)) {
                  add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `value_bindings.${bindingKey}.${name} 含非法字符或超过 ${max} 字符`);
                }
              }
              if (binding.provider_alias !== undefined) {
                const providerAlias = mappingIdentifier(binding.provider_alias);
                if (!providerAlias || providerAlias.length > 128 || hasUnsafeMappingText(providerAlias)) {
                  add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `value_bindings.${bindingKey}.provider_alias 不能为空且不能超过 128 字符`);
                }
              }
              if (binding.alias_fields !== undefined) {
                if (!Array.isArray(binding.alias_fields)) {
                  add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `value_bindings.${bindingKey}.alias_fields 必须是字符串数组`);
                } else {
                  const aliases = new Set<string>();
                  binding.alias_fields.forEach((item: unknown, index: number) => {
                    const value = mappingIdentifier(item);
                    const normalized = normalizedMappingIdentifier(value);
                    if (!value || value.length > 128 || hasUnsafeMappingText(value)) {
                      add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `value_bindings.${bindingKey}.alias_fields[${index}] 不能为空且不能超过 128 字符`);
                    }
                    if (aliases.has(normalized)) {
                      add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `value_bindings.${bindingKey}.alias_fields[${index}] 与其他别名字段重复`);
                    }
                    aliases.add(normalized);
                  });
                }
              }
            }
          }
        }

        const rawBindingKeys = new Set(Object.keys(valueBindings ?? {}));
        for (const [objectKey, v] of Object.entries<any>(j.objects)) {
          if (!v?.fields || typeof v.fields !== 'object' || Array.isArray(v.fields)) continue;
          const objectProvider = mappingIdentifier(v.provider_alias ?? j.default_provider_alias);
          for (const [fieldName, fieldMapping] of Object.entries<any>(v.fields)) {
            const ref = fieldMapping?.value_binding_ref;
            if (ref === undefined) continue;
            const bindingRef = mappingIdentifier(ref);
            if (!bindingRef || bindingRef.length > 128 || hasUnsafeMappingText(bindingRef)) {
              add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `objects.${objectKey}.fields.${fieldName}.value_binding_ref 不能为空且不能超过 128 字符`);
              continue;
            }
            if (!rawBindingKeys.has(bindingRef)) {
              add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `objects.${objectKey}.fields.${fieldName}.value_binding_ref 引用不存在的 binding ${bindingRef}`);
              continue;
            }
            const binding = valueBindings?.[bindingRef];
            const bindingProvider = mappingIdentifier(binding?.provider_alias ?? j.default_provider_alias);
            if (!objectProvider || !bindingProvider || objectProvider !== bindingProvider) {
              add('错误', 'mapping 非法', 'mappings/object-data-mappings.json', `objects.${objectKey}.fields.${fieldName}.value_binding_ref 的 provider_alias 与 binding ${bindingRef} 不一致`);
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

export interface PackageCheckResult {
  version: string;
  revision?: string;
  findings: CheckFinding[];
  createdAt: string;
}

function rowToPackageCheck(row: any): PackageCheckResult {
  return {
    version: row.version,
    revision: row.artifact_revision_oid ?? undefined,
    findings: (JSON.parse(row.findings_json) as CheckFinding[]).map(withCheckFindingFingerprint),
    createdAt: row.created_at,
  };
}

// ---- 结果存取（按知识包修订保留历史；package_checks 仅作旧库兼容缓存） ----
export function saveCheck(projectId: string, version: string, findings: CheckFinding[], revision?: string): void {
  const db = getDb();
  const serialized = JSON.stringify(findings.map(withCheckFindingFingerprint));
  const createdAt = nowIso();
  const ownsTransaction = !db.isTransaction;
  if (ownsTransaction) db.exec('BEGIN IMMEDIATE');
  try {
    if (revision) {
      db.prepare(`INSERT INTO package_check_revisions
        (project_id, artifact_revision_oid, version, findings_json, created_at) VALUES (?,?,?,?,?)
        ON CONFLICT(project_id, artifact_revision_oid) DO UPDATE SET
          version = excluded.version,
          findings_json = excluded.findings_json,
          created_at = excluded.created_at`)
        .run(projectId, revision, version, serialized, createdAt);
    }
    db.prepare(`INSERT INTO package_checks
      (project_id, version, findings_json, artifact_revision_oid, created_at) VALUES (?,?,?,?,?)
      ON CONFLICT(project_id) DO UPDATE SET version = excluded.version,
        findings_json = excluded.findings_json,
        artifact_revision_oid = excluded.artifact_revision_oid,
        created_at = excluded.created_at`)
      .run(
        projectId,
        version,
        serialized,
        revision ?? null,
        createdAt,
      );
    if (ownsTransaction) db.exec('COMMIT');
  } catch (error) {
    if (ownsTransaction && db.isTransaction) db.exec('ROLLBACK');
    throw error;
  }
}

export function getCheck(projectId: string, revision?: string): PackageCheckResult | null {
  const db = getDb();
  if (revision) {
    const historical = db.prepare(`SELECT * FROM package_check_revisions
      WHERE project_id = ? AND artifact_revision_oid = ?`).get(projectId, revision) as any;
    if (historical) return rowToPackageCheck(historical);
    const legacy = db.prepare(`SELECT * FROM package_checks
      WHERE project_id = ? AND artifact_revision_oid = ?`).get(projectId, revision) as any;
    return legacy ? rowToPackageCheck(legacy) : null;
  }

  // 默认读取用于兼容旧调用。若修订索引可用，按知识包修订序号而不是校验完成时间
  // 选取最新结果，避免迟到的旧校验成为项目的“最近校验”。
  const historical = db.prepare(`SELECT checks.* FROM package_check_revisions checks
    LEFT JOIN artifact_revisions revisions
      ON revisions.project_id = checks.project_id
      AND revisions.oid = checks.artifact_revision_oid
    WHERE checks.project_id = ?
    ORDER BY
      CASE WHEN revisions.sequence IS NULL THEN 0 ELSE 1 END DESC,
      revisions.sequence DESC,
      checks.created_at DESC,
      checks.rowid DESC
    LIMIT 1`).get(projectId) as any;
  if (historical) return rowToPackageCheck(historical);
  const legacy = db.prepare('SELECT * FROM package_checks WHERE project_id = ?').get(projectId) as any;
  return legacy ? rowToPackageCheck(legacy) : null;
}

export function currentPackageCheckView(
  check: ReturnType<typeof getCheck>,
  currentRevision: string | null,
) {
  const stale = check ? check.revision !== currentRevision : Boolean(currentRevision);
  return {
    currentRevision,
    stale,
    check: stale ? null : check,
  };
}
