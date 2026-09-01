import { Type } from '@earendil-works/pi-ai';
import type { DataDictTable, KnowledgeArtifactKind } from '../types.js';
import { KEY_SCHEMA_PATTERN, KEY_SCHEMA_DESCRIPTION } from './keys.js';
import { RELATION_NAME_GENERATION_GUIDANCE } from './relation-name.js';
import { createSourceCatalog } from './source-catalog.js';
import {
  readDocumentPage,
  type ReadableDocument,
} from './document-read-session.js';
import {
  materialInventoryItemSummary,
  type MaterialInventoryItem,
} from './material-inventory.js';

// 建模工具集。M1 用到：读数据字典、读文档、发起访谈（terminate 挂起）、产出初步分析。
// execute 抛错即失败（pi 约定），返回 { content, details, terminate? }。

const text = (t: string) => ({ content: [{ type: 'text' as const, text: t }], details: {} });

export interface ToolContext {
  tables: DataDictTable[];
  documents: ReadableDocument[];
  /** 全部上传资料；documents 仅包含可读取正文的资料。 */
  materials?: MaterialInventoryItem[];
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
    description: ctx.materials
      ? '列出全部已上传资料（包括 Excel/CSV 表格和文本类文档）及解析状态；文本长文档同时返回章节大纲'
      : '列出已上传的文本类文档：文件名、总字符数与章节大纲（长文档先看大纲，再用 search_documents 定位、read_document 分页精读）',
    parameters: Type.Object({}),
    async execute() {
      if (!ctx.materials) {
        if (!ctx.documents.length) return text('（无文本文档）');
        const out = ctx.documents.map((document) => {
          const body = document.textContent ?? '';
          const heads = body.split('\n').filter(isHeading).slice(0, 40);
          return `【${document.filename}】共 ${body.length} 字符${heads.length ? `\n大纲：${heads.map((h) => h.trim()).join(' / ')}` : ''}`;
        }).join('\n\n');
        return text(out);
      }
      const materials = ctx.materials;
      if (!materials.length) return text('（无已上传资料）');
      const out = materials.map((material) => {
        const body = ctx.documents.find((document) => document.filename === material.filename)?.textContent ?? '';
        const heads = body.split('\n').filter(isHeading).slice(0, 40);
        return `${materialInventoryItemSummary(material)}${heads.length ? `\n大纲：${heads.map((h) => h.trim()).join(' / ')}` : ''}`;
      }).join('\n\n');
      return {
        content: [{ type: 'text' as const, text: out }],
        details: {
          count: materials.length,
          parsed: materials.filter((item) => item.parseStatus === 'parsed').length,
          pending: materials.filter((item) => item.parseStatus === 'pending').length,
          failed: materials.filter((item) => item.parseStatus === 'failed').length,
        },
      };
    },
  };

  const readDocument = {
    name: 'read_document',
    label: '读取文档',
    description: [
      '按需分页读取文档正文，每次返回最多 6000 字符和结构化 offset/EOF 信息。',
      '适合在关键词检索后核对具体章节或条款；完整文档分析由独立的并发分段事实管道完成。',
    ].join(''),
    parameters: Type.Object({
      filename: Type.String(),
      offset: Type.Optional(Type.Number({ minimum: 0 })),
    }),
    async execute(_id: string, params: {
      filename: string;
      offset?: number;
    }) {
      const d = ctx.documents.find((x) => x.filename === params.filename);
      if (!d?.textContent) return text(`未找到文档或无正文：${params.filename}`);
      const page = readDocumentPage(d, params.offset);
      return {
        content: [{ type: 'text' as const, text: JSON.stringify(page) }],
        details: {
          filename: page.filename,
          offset: page.offset,
          endOffset: page.endOffset,
          totalChars: page.totalChars,
          nextOffset: page.nextOffset,
          eof: page.eof,
        },
      };
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

  return [listTables, describeTable, readTableRows, listDocuments, readDocument, searchDocuments];
}

/**
 * 只要「翻文档」这三件套：列大纲、按关键词检索、分页精读。
 *
 * 长文档不能靠截断塞进提示词——一份 20 万字的手册前 6000 字符全是目录和前言，
 * 模型看不到任何产品条款，只会得出「资料里没有具体内容」的错误结论。
 * 给它工具让它自己按需翻，比我们替它猜哪 6000 字符重要靠谱得多。
 */
export function buildDocumentReadingTools(
  documents: ToolContext['documents'],
  materials?: MaterialInventoryItem[],
) {
  const all = buildTools({ tables: [], documents, materials, captured: {} });
  return all.filter((tool) => (
    tool.name === 'list_documents'
      || (documents.length > 0 && ['read_document', 'search_documents'].includes(tool.name))
  ));
}

// 初步对象分析完成后，单独判断每份材料适合沉淀为何种知识资源。
// 知识生成阶段按资源类型拆开提交，避免大项目把对象、规则和操作塞进同一个超大 tool call。
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
  aliases: Type.Optional(Type.Array(Type.String({
    description: '用户可能使用的自然语言别名；在整个知识包内不得与其他对象的 alias、key 或 title 重复（英文忽略大小写）',
  }))),
  queryBoundary: Type.Optional(Type.String({ description: '查询边界：查询该对象前的前置条件与不得推断的事项' })),
  relations: Type.Optional(Type.Array(Type.Object({
    relation: Type.String({ description: RELATION_NAME_GENERATION_GUIDANCE }),
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
      role: Type.Optional(Type.Union([Type.Literal('primary'), Type.Literal('extension')])),
      fieldBindings: Type.Optional(Type.Array(Type.Object({
        fieldKey: KeyString(),
        sourceColumn: Type.String(),
      }))),
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
    alias: Type.Optional(Type.String({
      description: '字段的真实业务别名；没有别名时省略该属性，不得填写横线、无或 N/A 等占位符',
    })),
    sourceColumn: Type.Optional(Type.String({ description: '来源 Sheet 中的真实列名（与字段 key 不同时必填）' })),
  })),
});

function literalString(values: string[]) {
  if (values.length === 1) return Type.Literal(values[0]);
  return Type.Union(values.map((value) => Type.Literal(value)));
}

export interface OperationSchemaOptions {
  /** 初始操作提取阶段不允许模型提前绑定尚未生成的脚本。 */
  allowScript?: boolean;
}

export const specOperationSchema = (
  competencyQuestionIds: string[] = [],
  options: OperationSchemaOptions = {},
) => Type.Object({
  key: KeyString(), title: Type.String(), description: Type.String(),
  inputs: Type.Array(Type.Object({ param: Type.String(), cnName: Type.Optional(Type.String()), type: Type.String(), required: Type.Boolean(), note: Type.Optional(Type.String()) })),
  outputs: Type.Array(Type.Object({ field: Type.String(), cnName: Type.Optional(Type.String()), type: Type.String(), note: Type.Optional(Type.String()) })),
  flow: Type.Array(Type.String({
    description: options.allowScript === false
      ? '按顺序提交步骤正文，不要在正文中添加 1.、2. 等序号；本阶段只允许引用 object:<key>，不得引用脚本'
      : '按顺序提交步骤正文，不要在正文中添加 1.、2. 等序号；引用对象写 object:<key>，引用脚本写 script:<key>',
  })),
  relatedObjects: Type.Array(Type.String()),
  usesRules: Type.Optional(Type.Array(Type.String({ description: '引用的规则 key（须在 rules 中声明）' }))),
  usesMetrics: Type.Optional(Type.Array(Type.String({ description: '引用的指标 key（须在 metrics 中声明）' }))),
  notes: Type.Optional(Type.Array(Type.String({ description: '注意事项（兜底、追问、不得推断等）' }))),
  ...(options.allowScript === false ? {} : {
    script: Type.Optional(Type.Object({ key: KeyString(), required: Type.Optional(Type.Boolean()) })),
  }),
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
  appliesToOperations: Type.Optional(Type.Array(Type.String({ description: '适用操作 key' }))),
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
    action: Type.Optional(Type.Union([Type.Literal('store_true'), Type.Literal('store_false')])),
  })),
  outputs: Type.Array(Type.Object({
    key: Type.String(), type: Type.String(), description: Type.Optional(Type.String()),
  })),
  boundaries: Type.Array(Type.String()),
  evidence: Type.Optional(EvidenceList()),
  sourceCode: Type.String({ description: '完整 Python 源码；不得包含答案常量、网络访问或越权文件访问。' }),
});

export interface ObjectSpecCapture { objects?: unknown[]; }
export function buildObjectGenerateTools(
  ctx: ToolContext,
  cap: ObjectSpecCapture,
  candidates: { id: string }[],
) {
  const base = buildTools(ctx);
  const submit = {
    name: 'submit_objects',
    label: '提交业务对象',
    description: '提交当前批次的完整业务对象规格；每个对象必须用 candidateId 认领一个给定候选，只提交对象，不提交规则或操作。',
    parameters: Type.Object({
      objects: Type.Array(specObjectSchema(candidates.map((candidate) => candidate.id)), { minItems: 1, maxItems: 12 }),
    }),
    async execute(_id: string, params: { objects: unknown[] }) {
      cap.objects = params.objects;
      return { content: [{ type: 'text' as const, text: `已提交 ${params.objects.length} 个对象。` }], details: {}, terminate: true };
    },
  };
  return [...base, submit];
}

export interface KnowledgeSpecCapture { rules?: unknown[]; metrics?: unknown[]; dispositions?: unknown[]; }
export function buildKnowledgeGenerateTools(ctx: ToolContext, cap: KnowledgeSpecCapture) {
  const base = buildTools(ctx);
  const submit = {
    name: 'submit_rules_metrics',
    label: '提交规则和指标',
    description: '提交当前批次从材料中提取的规则和指标。没有可靠依据时返回空数组，不得编造阈值或公式。',
    parameters: Type.Object({
      rules: Type.Array(specRuleSchema(), { maxItems: 12 }),
      metrics: Type.Array(specMetricSchema(), { maxItems: 12 }),
      dispositions: Type.Optional(Type.Array(Type.Object({
        candidateId: Type.String(), kind: Type.Union([
          Type.Literal('object'), Type.Literal('rule'), Type.Literal('operation'), Type.Literal('metric'),
        ]), name: Type.String(),
        status: Type.Union([
          Type.Literal('generated'), Type.Literal('merged'), Type.Literal('deferred'),
          Type.Literal('needs_clarification'), Type.Literal('excluded'),
        ]),
        resourceKeys: Type.Optional(Type.Array(Type.String())),
        mergedIntoCandidateId: Type.Optional(Type.String()),
        reason: Type.String(),
      }))),
    }),
    async execute(_id: string, params: {
      rules: unknown[];
      metrics: unknown[];
      dispositions?: Array<{ candidateId: string; kind: KnowledgeArtifactKind; name: string }>;
    }) {
      cap.rules = params.rules;
      cap.metrics = params.metrics;
      cap.dispositions = params.dispositions;
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
  const base = buildTools(ctx);
  const submit = {
    name: 'submit_operations',
    label: '提交操作',
    description: '提交当前批次领域问题对应的完整操作规格；每个 operation 必须用 competencyQuestionId 认领一个给定问题，问题原文由服务端回填。',
    parameters: Type.Object({
      operations: Type.Array(specOperationSchema(questions.map((question) => question.id), { allowScript: false }), { minItems: 1, maxItems: 3 }),
    }),
    async execute(_id: string, params: { operations: unknown[] }) {
      cap.operations = params.operations;
      return { content: [{ type: 'text' as const, text: `已提交 ${params.operations.length} 个操作。` }], details: {}, terminate: true };
    },
  };
  return [...base, submit];
}
