import { Type } from '@earendil-works/pi-ai';
import type { DataDictTable } from '../types.js';

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
      const t = ctx.tables.find((x) => x.name === params.table);
      if (!t) return text(`未找到表 ${params.table}`);
      const lines = t.fields.map(
        (f) => `- ${f.name}${f.note ? `（${f.note}）` : ''} | ${f.inferredType} | 唯一比 ${f.distinctRatio.toFixed(2)} | 样例: ${f.sampleValues.join('/')}`,
      );
      return { content: [{ type: 'text' as const, text: lines.join('\n') }], details: { fields: t.fields } };
    },
  };

  const listDocuments = {
    name: 'list_documents',
    label: '列出文档',
    description: '列出已上传的文本类文档（非 Excel）',
    parameters: Type.Object({}),
    async execute() {
      const names = ctx.documents.map((d) => d.filename).join(', ');
      return { content: [{ type: 'text' as const, text: names || '（无文本文档）' }], details: {} };
    },
  };

  const readDocument = {
    name: 'read_document',
    label: '读取文档',
    description: '读取某个文本文档的正文（截断到前 8000 字符）',
    parameters: Type.Object({ filename: Type.String() }),
    async execute(_id: string, params: { filename: string }) {
      const d = ctx.documents.find((x) => x.filename === params.filename);
      if (!d?.textContent) return text(`未找到文档或无正文：${params.filename}`);
      return text(d.textContent.slice(0, 8000));
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
        sourceTables: Type.Array(Type.String()),
      })),
      openQuestions: Type.Array(Type.Object({ topic: Type.String(), why: Type.String() })),
    }),
    async execute(_id: string, params: unknown) {
      ctx.captured.analysis = params;
      return {
        content: [{ type: 'text' as const, text: '初步分析已提交。' }],
        details: {},
        terminate: true,
      };
    },
  };

  return [listTables, describeTable, listDocuments, readDocument, submitAnalysis];
}

// ---- M2 访谈工具：ask_user（terminate 挂起）+ finish_interview ----
export interface InterviewCapture {
  pendingBatch?: { questions: unknown[] };
  finished?: boolean;
}

export function buildInterviewTools(ctx: ToolContext, cap: InterviewCapture) {
  const base = buildTools(ctx).filter((t) => t.name !== 'submit_analysis');
  const askUser = {
    name: 'ask_user',
    label: '向专家提问',
    description: '提出一批（1~5 道）访谈选择题，等待专家作答。题型 kind: single/multi/text；topic 属于 对象划分/字段含义/关系/口径 之一。',
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
    description: '当关键决策点（对象划分/字段含义/关系/口径）已澄清充分时调用，结束建模访谈。',
    parameters: Type.Object({ summary: Type.String() }),
    async execute(_id: string, params: { summary: string }) {
      cap.finished = true;
      cap.pendingBatch = undefined;
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
export interface SpecCapture { spec?: unknown; }
export function buildGenerateTools(ctx: ToolContext, cap: SpecCapture) {
  const base = buildTools(ctx).filter((t) => t.name !== 'submit_analysis');
  const submitSpec = {
    name: 'submit_package_spec',
    label: '提交知识包规格',
    description: '提交结构化的知识包规格（对象、字段、操作、映射来源表）。系统据此渲染标准包文件。',
    parameters: Type.Object({
      objects: Type.Array(Type.Object({
        key: Type.String(), title: Type.String(), description: Type.String(),
        sourceTable: Type.Optional(Type.String()),
        primaryKey: Type.Optional(Type.Array(Type.String())),
        fields: Type.Array(Type.Object({
          key: Type.String(),
          cnName: Type.Optional(Type.String()),
          type: Type.Optional(Type.String()),
          required: Type.Optional(Type.Boolean()),
          note: Type.Optional(Type.String()),
        })),
      })),
      operations: Type.Array(Type.Object({
        key: Type.String(), title: Type.String(), description: Type.String(),
        inputs: Type.Array(Type.Object({ param: Type.String(), type: Type.String(), required: Type.Boolean(), note: Type.Optional(Type.String()) })),
        outputs: Type.Array(Type.Object({ field: Type.String(), type: Type.String(), note: Type.Optional(Type.String()) })),
        flow: Type.Array(Type.String()),
        relatedObjects: Type.Array(Type.String()),
        competencyQuestion: Type.Optional(Type.String()),
      })),
    }),
    async execute(_id: string, params: unknown) {
      cap.spec = params;
      return { content: [{ type: 'text' as const, text: '知识包规格已提交。' }], details: {}, terminate: true };
    },
  };
  return [...base, submitSpec];
}
