import { Type } from '@earendil-works/pi-ai';
import type { AnalysisResult, DataDictTable } from '../types.js';
import { KEY_SCHEMA_PATTERN, KEY_SCHEMA_DESCRIPTION } from './keys.js';
import { createSourceCatalog } from './source-catalog.js';

// 建模工具集。M1 用到：读数据字典、读文档、发起访谈（terminate 挂起）、产出初步分析。
// execute 抛错即失败（pi 约定），返回 { content, details, terminate? }。

const text = (t: string) => ({ content: [{ type: 'text' as const, text: t }], details: {} });

export interface ToolContext {
  tables: DataDictTable[];
  documents: { filename: string; textContent?: string }[];
  // 收集节点产物：analyze 工具把结构化结果写这里
  captured: Record<string, unknown>;
}

export function buildTools(ctx: ToolContext) {
  const sourceCatalog = createSourceCatalog(ctx.tables);
  const availableTables = ctx.tables.map((table) => table.name.trim() || table.name);
  const resolveTable = (reference: string): { table?: DataDictTable; error?: string } => {
    const resolution = sourceCatalog.resolveTable(reference);
    if (resolution.status === 'resolved') {
      const table = ctx.tables.find((item) => item.name === resolution.tableName);
      return table
        ? { table }
        : { error: `表 ${reference} 的来源目录已失效，请重新上传资料` };
    }
    if (resolution.status === 'ambiguous') {
      return {
        error: `表名 ${reference} 存在歧义，候选原始 Sheet：${resolution.candidates.map((item) => JSON.stringify(item)).join('、')}`,
      };
    }
    const suffix = availableTables.length ? `。可用表：${availableTables.join('、')}` : '';
    return { error: `未找到表 ${reference}${suffix}` };
  };

  const listTables = {
    name: 'list_tables',
    label: '列出数据表',
    description: '列出已解析的所有数据库表及其字段概览',
    parameters: Type.Object({}),
    async execute() {
      const summary = ctx.tables.map(
        (t) => `${t.name}（${t.rowCount} 行）: ${t.fields.map((f) => f.name).join(', ')}`,
      ).join('\n');
      return { content: [{ type: 'text' as const, text: summary || '（无数据表）' }], details: { count: ctx.tables.length } };
    },
  };

  const describeTable = {
    name: 'describe_table',
    label: '查看表字段',
    description: '返回某张表的字段明细（名称、注释、推断类型、样例、唯一值比例）',
    parameters: Type.Object({ table: Type.String() }),
    async execute(_id: string, params: { table: string }) {
      const resolved = resolveTable(params.table);
      if (!resolved.table) return text(resolved.error ?? `未找到表 ${params.table}`);
      const t = resolved.table;
      const lines = t.fields.map(
        (f) => `- ${f.name}${f.note ? `（${f.note}）` : ''} | ${f.inferredType} | 唯一比 ${f.distinctRatio.toFixed(2)} | 样例: ${f.sampleValues.join('/')}`,
      );
      return { content: [{ type: 'text' as const, text: lines.join('\n') }], details: { fields: t.fields } };
    },
  };

  const readTableRows = {
    name: 'read_table_rows',
    label: '读取表格数据行',
    description: '按页返回上传表格的原始数据行，用于核对字段组合、关系和计算口径。单次最多 50 行。',
    parameters: Type.Object({
      table: Type.String(),
      offset: Type.Optional(Type.Number()),
      limit: Type.Optional(Type.Number()),
    }),
    async execute(_id: string, params: { table: string; offset?: number; limit?: number }) {
      const resolved = resolveTable(params.table);
      if (!resolved.table) return text(resolved.error ?? `未找到表 ${params.table}`);
      const table = resolved.table;
      const offset = Math.max(0, Math.floor(params.offset ?? 0));
      const limit = Math.max(1, Math.min(50, Math.floor(params.limit ?? 20)));
      const sourceRows = table.rows ?? [];
      const rows = sourceRows.slice(offset, offset + limit).map((row) => Object.fromEntries(
        table.fields.map((field, index) => [field.name, row[index] ?? '']),
      ));
      const total = sourceRows.length || table.rowCount;
      const nextOffset = offset + rows.length < total ? offset + rows.length : null;
      return text(JSON.stringify({
        table: table.name,
        columns: table.fields.map((field) => field.name),
        rows,
        page: { offset, limit, returned: rows.length, total, nextOffset },
      }));
    },
  };

  // 标题行启发式：编号/章节样式的短行（长文档大纲用）
  const isHeading = (line: string) => {
    const t = line.trim();
    return t.length > 0 && t.length <= 40
      && /^(第?[一二三四五六七八九十百0-9]{1,4}\s*[、．.章节条）)]|[（(][一二三四五六七八九十0-9]{1,3}[）)]|#{1,4}\s)/.test(t);
  };

  const listDocuments = {
    name: 'list_documents',
    label: '列出文档',
    description: '列出已上传的文本类文档：文件名、总字符数与章节大纲（长文档先看大纲，再用 search_documents 定位、read_document 分页精读）',
    parameters: Type.Object({}),
    async execute() {
      if (!ctx.documents.length) return text('（无文本文档）');
      const out = ctx.documents.map((d) => {
        const body = d.textContent ?? '';
        const heads = body.split('\n').filter(isHeading).slice(0, 40);
        return `【${d.filename}】共 ${body.length} 字符${heads.length ? `\n大纲：${heads.map((h) => h.trim()).join(' / ')}` : ''}`;
      }).join('\n\n');
      return text(out);
    },
  };

  const readDocument = {
    name: 'read_document',
    label: '读取文档',
    description: '分页读取文档正文：每次返回约 6000 字符，长文档用 offset 翻页（返回中含下一页偏移提示）',
    parameters: Type.Object({ filename: Type.String(), offset: Type.Optional(Type.Number()) }),
    async execute(_id: string, params: { filename: string; offset?: number }) {
      const d = ctx.documents.find((x) => x.filename === params.filename);
      if (!d?.textContent) return text(`未找到文档或无正文：${params.filename}`);
      const body = d.textContent;
      const off = Math.max(0, Math.floor(params.offset ?? 0));
      const chunk = body.slice(off, off + 6000);
      const tail = off + 6000 < body.length ? `\n\n……（未完，全文 ${body.length} 字符，续读请传 offset=${off + 6000}）` : '\n\n（已到文末）';
      return text(`[${d.filename} @${off}]\n${chunk}${tail}`);
    },
  };

  const searchDocuments = {
    name: 'search_documents',
    label: '检索文档',
    description: '按关键词（空格分隔多个，OR 命中）在全部文档中检索，返回命中段落及其偏移（可据此 read_document 精读上下文）。长文档找产品/规则/口径先用这个。',
    parameters: Type.Object({ query: Type.String() }),
    async execute(_id: string, params: { query: string }) {
      const kws = params.query.split(/\s+/).map((k) => k.trim()).filter((k) => k.length >= 2);
      if (!kws.length) return text('请提供至少一个长度 ≥2 的关键词');
      const hits: string[] = [];
      for (const d of ctx.documents) {
        const body = d.textContent ?? '';
        if (!body) continue;
        const lines = body.split('\n');
        let offset = 0;
        for (let i = 0; i < lines.length && hits.length < 8; i++) {
          const line = lines[i];
          if (kws.some((k) => line.includes(k))) {
            const ctxText = lines.slice(Math.max(0, i - 1), i + 3).join('\n');
            hits.push(`【${d.filename} @${offset}】\n${ctxText.slice(0, 500)}`);
            i += 2; // 避免相邻行重复命中
          }
          offset += line.length + 1;
        }
        if (hits.length >= 8) break;
      }
      return text(hits.length ? hits.join('\n---\n') : `未命中：${params.query}（可换近义词或拆词重试）`);
    },
  };

  // 节点 3 的产出工具：agent 分析完调用它提交结构化结果，并 terminate。
  const submitAnalysis = {
    name: 'submit_analysis',
    label: '提交初步分析',
    description: '提交领域概览、候选业务对象、以及需要向专家澄清的开放问题',
    parameters: Type.Object({
      domainOverview: Type.String(),
      candidateObjects: Type.Array(Type.Object({
        name: Type.String(),
        rationale: Type.String(),
        sourceTables: Type.Array(Type.String({ description: '必须引用 list_tables 返回的已有表名，不得自行创建表名' })),
      })),
      candidateGoals: Type.Optional(Type.Array(Type.Object({
        title: Type.String(),
        problem: Type.String(),
        targetUsers: Type.Array(Type.String()),
        scenarios: Type.Array(Type.String()),
        successCriteria: Type.Array(Type.String()),
        rationale: Type.String(),
      }), { maxItems: 3 })),
      candidateProcesses: Type.Optional(Type.Array(Type.Object({
        title: Type.String(),
        trigger: Type.String(),
        actors: Type.Array(Type.String()),
        inputs: Type.Array(Type.String()),
        steps: Type.Array(Type.String()),
        exceptions: Type.Array(Type.String()),
        outputs: Type.Array(Type.String()),
        handoffs: Type.Array(Type.String()),
        rationale: Type.String(),
      }), { maxItems: 3 })),
      openQuestions: Type.Array(Type.Object({ topic: Type.String(), why: Type.String() })),
    }),
    async execute(_id: string, params: unknown) {
      const canonical = sourceCatalog.canonicalizeAnalysis(params as AnalysisResult);
      if (canonical.problems.length) {
        return text([
          '初步分析未提交，来源表校验失败：',
          ...canonical.problems.map((problem) => `- ${problem.message}`),
          availableTables.length ? `可用表：${availableTables.join('、')}` : '当前没有可用表',
          '请修正 candidateObjects.sourceTables 后重新调用 submit_analysis。',
        ].join('\n'));
      }
      ctx.captured.analysis = canonical.value;
      return {
        content: [{ type: 'text' as const, text: '初步分析已提交。' }],
        details: {},
        terminate: true,
      };
    },
  };

  return [listTables, describeTable, readTableRows, listDocuments, readDocument, searchDocuments, submitAnalysis];
}

// 初步对象分析完成后，单独判断每份材料适合沉淀为何种知识资源。
// 与 submit_analysis 分开，避免大项目把对象和知识候选塞进一个超大 tool call 后被截断。
export function buildArtifactAnalysisTools(ctx: ToolContext) {
  const base = buildTools(ctx).filter((tool) => tool.name !== 'submit_analysis');
  const artifactKind = Type.Union([
    Type.Literal('object'), Type.Literal('rule'), Type.Literal('operation'), Type.Literal('metric'),
  ]);
  const submit = {
    name: 'submit_artifact_analysis',
    label: '提交材料知识类型分析',
    description: '逐份材料说明适合生成对象、规则、操作或指标，并列出有材料依据的知识候选。',
    parameters: Type.Object({
      materialAssessments: Type.Array(Type.Object({
        source: Type.String({ description: 'list_tables/list_documents 返回的精确表名或文件名' }),
        sourceType: Type.Union([Type.Literal('table'), Type.Literal('document')]),
        suitableFor: Type.Array(artifactKind),
        rationale: Type.String(),
      })),
      candidateArtifacts: Type.Array(Type.Object({
        kind: artifactKind,
        name: Type.String(),
        rationale: Type.String(),
        sources: Type.Array(Type.String({ description: '精确表名或文件名' })),
        evidence: Type.Optional(Type.String({ description: '材料中的短证据或章节定位，不要大段复制原文' })),
        readiness: Type.Union([Type.Literal('extractable'), Type.Literal('needs_clarification')]),
      })),
    }),
    async execute(_id: string, params: unknown) {
      ctx.captured.artifactAnalysis = params;
      return {
        content: [{ type: 'text' as const, text: '材料知识类型分析已提交。' }],
        details: {},
        terminate: true,
      };
    },
  };
  return [...base, submit];
}

// ---- M2 访谈工具：ask_user（terminate 挂起）+ finish_interview ----
export interface InterviewCapture {
  pendingBatch?: { questions: unknown[] };
  finished?: boolean;
  outcome?: import('../types.js').InterviewOutcome;
}

export function buildInterviewTools(ctx: ToolContext, cap: InterviewCapture) {
  const base = buildTools(ctx).filter((t) => t.name !== 'submit_analysis');
  const askUser = {
    name: 'ask_user',
    label: '向专家提问',
    description: '提出一批（1~5 道）流程访谈题，等待专家作答。题型 kind: single/multi/text；topic 属于 触发/角色/输入/步骤/判断/异常/输出/交接 之一。',
    parameters: Type.Object({
      questions: Type.Array(Type.Object({
        kind: Type.Union([Type.Literal('single'), Type.Literal('multi'), Type.Literal('text')]),
        q: Type.String(),
        options: Type.Array(Type.String()),
        topic: Type.String(),
      })),
    }),
    async execute(_id: string, params: { questions: unknown[] }) {
      cap.pendingBatch = { questions: params.questions };
      return {
        content: [{ type: 'text' as const, text: '[已向专家发送一批访谈题，等待作答]' }],
        details: {},
        terminate: true,
      };
    },
  };
  const finishInterview = {
    name: 'finish_interview',
    label: '结束访谈',
    description: '当业务流程的触发、参与角色、输入、步骤、判断分支、异常、输出和交接均已澄清时调用，并提交可绘图的结构化流程。',
    parameters: Type.Object({
      summary: Type.String(),
      processes: Type.Array(Type.Object({
        id: Type.String(),
        title: Type.String(),
        trigger: Type.String(),
        actors: Type.Array(Type.String()),
        inputs: Type.Array(Type.String()),
        steps: Type.Array(Type.Object({
          id: Type.String(),
          label: Type.String(),
          kind: Type.Union([Type.Literal('action'), Type.Literal('decision')]),
          detail: Type.Optional(Type.String()),
        }), { minItems: 1 }),
        transitions: Type.Array(Type.Object({
          from: Type.String(), to: Type.String(), label: Type.Optional(Type.String()),
        })),
        exceptions: Type.Array(Type.String()),
        outputs: Type.Array(Type.String()),
        handoffs: Type.Array(Type.String()),
      }), { minItems: 1 }),
    }),
    async execute(_id: string, params: import('../types.js').InterviewOutcome) {
      cap.finished = true;
      cap.pendingBatch = undefined;
      cap.outcome = params;
      return { content: [{ type: 'text' as const, text: '访谈已结束。' }], details: { summary: params.summary }, terminate: true };
    },
  };
  return [...base, askUser, finishInterview];
}

// ---- M2 胜任力工具 ----
export interface CompetencyCapture { questions?: string[]; }
export function buildCompetencyTools(ctx: ToolContext, cap: CompetencyCapture) {
  const base = buildTools(ctx).filter((t) => t.name !== 'submit_analysis');
  const propose = {
    name: 'propose_competency_questions',
    label: '提出胜任力问题',
    description: '提出 10~20 条该知识包建成后必须能回答的业务问题（胜任力问题），供专家勾选。',
    parameters: Type.Object({ questions: Type.Array(Type.String()) }),
    async execute(_id: string, params: { questions: string[] }) {
      cap.questions = params.questions;
      return { content: [{ type: 'text' as const, text: `已提出 ${params.questions.length} 条候选。` }], details: {}, terminate: true };
    },
  };
  return [...base, propose];
}

// ---- M2 包生成工具 ----
// key 一律带 pattern/描述引导（keys.ts 统一规则）；渲染入包前 renderPackage 还会强制校验。
const KeyString = () => Type.String({ pattern: KEY_SCHEMA_PATTERN, maxLength: 80, description: KEY_SCHEMA_DESCRIPTION });
const EvidenceList = () => Type.Array(Type.String({
  description: '简要知识证据引用，如 material:<文件>#<章节>、diagram:<流程>、flow-node:<节点> 或 user-decision:<确认项>',
}));

// spec 的 objects/operations/rules/metrics schema——submit_package_spec 与
// optimizer 的 submit_revised_spec 共用，保证两处捕获的字段一致。
export const specObjectSchema = (candidateIds: string[] = []) => Type.Object({
  candidateId: candidateIds.length
    ? literalString(candidateIds)
    : Type.Optional(Type.String()),
  key: KeyString(), title: Type.String(), description: Type.String(),
  aliases: Type.Optional(Type.Array(Type.String({ description: '用户可能使用的自然语言别名' }))),
  queryBoundary: Type.Optional(Type.String({ description: '查询边界：查询该对象前的前置条件与不得推断的事项' })),
  relations: Type.Optional(Type.Array(Type.Object({
    relation: Type.String({ description: '关系名，如 belongs_to / references' }),
    target: KeyString(),
    note: Type.Optional(Type.String()),
    evidence: Type.Optional(EvidenceList()),
  }, { description: '对象间关系（目标对象须是包内已声明的对象 key）' }))),
  sourceRefs: Type.Optional(Type.Array(Type.Union([
    Type.Object({
      kind: Type.Literal('table'),
      documentId: Type.Optional(Type.String()),
      table: Type.String({ description: '数据字典中的真实 Sheet 名' }),
      columns: Type.Optional(Type.Array(Type.String())),
    }),
    Type.Object({
      kind: Type.Literal('document'),
      documentId: Type.Optional(Type.String()),
      filename: Type.String({ description: 'list_documents 返回的原始文件名' }),
      anchors: Type.Optional(Type.Array(Type.Object({
        heading: Type.Optional(Type.String()),
        offset: Type.Optional(Type.Number({ minimum: 0 })),
        quote: Type.Optional(Type.String()),
      }))),
    }),
  ]), { minItems: 1, description: '对象的资料来源。数据对象绑定表格，产品、场景等语义对象绑定文档证据。' })),
  sourceTable: Type.Optional(Type.String({ description: '旧格式兼容：数据字典真实 Sheet 名' })),
  primaryKey: Type.Optional(Type.Array(Type.String())),
  fields: Type.Array(Type.Object({
    key: KeyString(),
    cnName: Type.String({ description: '字段中文业务名，用于语义展示，不作为 mapping 字段 key' }),
    type: Type.Optional(Type.String()),
    required: Type.Optional(Type.Boolean()),
    note: Type.Optional(Type.String()),
    alias: Type.Optional(Type.String({ description: '字段的业务别名' })),
    sourceColumn: Type.Optional(Type.String({ description: '来源 Sheet 中的真实列名（与字段 key 不同时必填）' })),
  })),
});

function literalString(values: string[]) {
  if (values.length === 1) return Type.Literal(values[0]);
  return Type.Union(values.map((value) => Type.Literal(value)));
}

export const specOperationSchema = (competencyQuestionIds: string[] = []) => Type.Object({
  key: KeyString(), title: Type.String(), description: Type.String(),
  inputs: Type.Array(Type.Object({ param: Type.String(), cnName: Type.Optional(Type.String()), type: Type.String(), required: Type.Boolean(), note: Type.Optional(Type.String()) })),
  outputs: Type.Array(Type.Object({ field: Type.String(), cnName: Type.Optional(Type.String()), type: Type.String(), note: Type.Optional(Type.String()) })),
  flow: Type.Array(Type.String({ description: '流程步骤；引用对象写 object:<key>，引用脚本写 script:<key>' })),
  relatedObjects: Type.Array(Type.String()),
  usesRules: Type.Optional(Type.Array(Type.String({ description: '引用的规则 key（须在 rules 中声明）' }))),
  usesMetrics: Type.Optional(Type.Array(Type.String({ description: '引用的指标 key（须在 metrics 中声明）' }))),
  notes: Type.Optional(Type.Array(Type.String({ description: '注意事项（兜底、追问、不得推断等）' }))),
  script: Type.Optional(Type.Object({ key: KeyString(), required: Type.Optional(Type.Boolean()) })),
  competencyQuestionId: competencyQuestionIds.length
    ? literalString(competencyQuestionIds)
    : Type.Optional(Type.String()),
  competencyQuestion: Type.Optional(Type.String()),
  evidence: Type.Optional(EvidenceList()),
});

export const specRuleSchema = () => Type.Object({
  key: KeyString(), title: Type.String(),
  description: Type.Optional(Type.String()),
  appliesToObjects: Type.Optional(Type.Array(Type.String({ description: '适用对象 key' }))),
  content: Type.String({ description: '规则内容：完整的业务判断口径' }),
  conditions: Type.Optional(Type.Array(Type.Object({ condition: Type.String(), conclusion: Type.String() }))),
  exceptions: Type.Optional(Type.Array(Type.String())),
  evidence: Type.Optional(EvidenceList()),
}, { description: '可复用的业务判断条件（判断类问题优先沉淀为 rule）' });

export const specMetricSchema = () => Type.Object({
  key: KeyString(), title: Type.String(),
  description: Type.Optional(Type.String()),
  unit: Type.Optional(Type.String()),
  grain: Type.Optional(Type.String({ description: '统计粒度，如 material + warehouse + batch' })),
  usesObjects: Type.Optional(Type.Array(Type.String({ description: '口径涉及的对象 key' }))),
  caliber: Type.String({ description: '业务口径（含公式）' }),
  boundaries: Type.Array(Type.String({ description: '计算边界：统计范围、不计入项' })),
  nullPolicy: Type.Optional(Type.Array(Type.Object({ field: Type.String(), handling: Type.String() }))),
  dimensions: Type.Optional(Type.Array(Type.Object({ key: Type.String(), note: Type.String() }))),
  evidence: Type.Optional(EvidenceList()),
}, { description: '有明确口径、公式和边界的业务指标（计算类口径优先沉淀为 metric）' });

export const specScriptSchema = () => Type.Object({
  key: KeyString(),
  title: Type.String(),
  description: Type.String(),
  boundOperation: KeyString(),
  runtime: Type.Literal('python'),
  entrypoint: Type.String({ pattern: '^[A-Za-z0-9_.-]+\\.py$' }),
  timeoutSeconds: Type.Number({ minimum: 1, maximum: 300 }),
  parameters: Type.Array(Type.Object({
    name: Type.String(), flag: Type.String(), type: Type.String(), required: Type.Boolean(),
    defaultValue: Type.Optional(Type.Union([Type.String(), Type.Number(), Type.Boolean()])),
    description: Type.Optional(Type.String()), multiple: Type.Optional(Type.Boolean()),
  })),
  outputs: Type.Array(Type.Object({
    key: Type.String(), type: Type.String(), description: Type.Optional(Type.String()),
  })),
  boundaries: Type.Array(Type.String()),
  evidence: Type.Optional(EvidenceList()),
  sourceCode: Type.String({ description: '完整 Python 源码；不得包含答案常量、网络访问或越权文件访问。' }),
});

export interface SpecCapture { spec?: unknown; }
export function buildGenerateTools(ctx: ToolContext, cap: SpecCapture) {
  const base = buildTools(ctx).filter((t) => t.name !== 'submit_analysis');
  const submitSpec = {
    name: 'submit_package_spec',
    label: '提交知识包规格',
    description: '提交结构化的知识包规格（对象、字段、操作、规则、指标、映射来源表）。系统据此渲染标准包文件。',
    parameters: Type.Object({
      objects: Type.Array(specObjectSchema()),
      operations: Type.Array(specOperationSchema()),
      rules: Type.Optional(Type.Array(specRuleSchema())),
      metrics: Type.Optional(Type.Array(specMetricSchema())),
    }),
    async execute(_id: string, params: unknown) {
      cap.spec = params;
      return { content: [{ type: 'text' as const, text: '知识包规格已提交。' }], details: {}, terminate: true };
    },
  };
  return [...base, submitSpec];
}

export interface ObjectSpecCapture { objects?: unknown[]; }
export function buildObjectGenerateTools(
  ctx: ToolContext,
  cap: ObjectSpecCapture,
  candidates: { id: string }[],
) {
  const base = buildTools(ctx).filter((tool) => tool.name !== 'submit_analysis');
  const submit = {
    name: 'submit_objects',
    label: '提交业务对象',
    description: '提交当前批次的完整业务对象规格；每个对象必须用 candidateId 认领一个给定候选，只提交对象，不提交规则或操作。',
    parameters: Type.Object({
      objects: Type.Array(specObjectSchema(candidates.map((candidate) => candidate.id)), { minItems: 1, maxItems: 4 }),
    }),
    async execute(_id: string, params: { objects: unknown[] }) {
      cap.objects = params.objects;
      return { content: [{ type: 'text' as const, text: `已提交 ${params.objects.length} 个对象。` }], details: {}, terminate: true };
    },
  };
  return [...base, submit];
}

export interface KnowledgeSpecCapture { rules?: unknown[]; metrics?: unknown[]; }
export function buildKnowledgeGenerateTools(ctx: ToolContext, cap: KnowledgeSpecCapture) {
  const base = buildTools(ctx).filter((tool) => tool.name !== 'submit_analysis');
  const submit = {
    name: 'submit_rules_metrics',
    label: '提交规则和指标',
    description: '提交当前批次从材料中提取的规则和指标。没有可靠依据时返回空数组，不得编造阈值或公式。',
    parameters: Type.Object({
      rules: Type.Array(specRuleSchema(), { maxItems: 5 }),
      metrics: Type.Array(specMetricSchema(), { maxItems: 5 }),
    }),
    async execute(_id: string, params: { rules: unknown[]; metrics: unknown[] }) {
      cap.rules = params.rules;
      cap.metrics = params.metrics;
      return {
        content: [{ type: 'text' as const, text: `已提交 ${params.rules.length} 条规则和 ${params.metrics.length} 个指标。` }],
        details: {}, terminate: true,
      };
    },
  };
  return [...base, submit];
}

export interface OperationSpecCapture { operations?: unknown[]; }
export function buildOperationGenerateTools(
  ctx: ToolContext,
  cap: OperationSpecCapture,
  questions: { id: string; question: string }[],
) {
  const base = buildTools(ctx).filter((tool) => tool.name !== 'submit_analysis');
  const submit = {
    name: 'submit_operations',
    label: '提交操作',
    description: '提交当前批次领域问题对应的完整操作规格；每个 operation 必须用 competencyQuestionId 认领一个给定问题，问题原文由服务端回填。',
    parameters: Type.Object({
      operations: Type.Array(specOperationSchema(questions.map((question) => question.id)), { minItems: 1, maxItems: 4 }),
    }),
    async execute(_id: string, params: { operations: unknown[] }) {
      cap.operations = params.operations;
      return { content: [{ type: 'text' as const, text: `已提交 ${params.operations.length} 个操作。` }], details: {}, terminate: true };
    },
  };
  return [...base, submit];
}
