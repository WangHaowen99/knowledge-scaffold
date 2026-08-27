import { Type } from '@earendil-works/pi-ai';
import { buildLlm, type LlmDeps } from '../llm/provider.js';
import { makeAgent, runAgentCaptured } from './agent.js';
import type { DataDictTable, Project } from '../types.js';

// 资料质量评估（领域分析环节）双层：
// ① runDataQualityChecks —— 确定性检查（代码直查）：数据字典与文档的建包就绪度。
//    规则源自两条真实场景验收的实测坑位：字段无注释→口径靠猜、枚举语义不明、
//    外键悬空、日期序列号残留、只有表没有规则文档等。
// ② runGapAnalysis —— LLM 缺口分析：对照领域问题，找"构建知识包还缺什么"，
//    每条缺口带证据与补齐方式，可作为访谈的优先澄清点。

export type QualitySeverity = '错误' | '警告' | '提示';
export interface QualityFinding {
  severity: QualitySeverity;
  category: string;
  target: string;       // 表名 / 表.列 / 文档名 / '整体'
  message: string;
  suggestion?: string;  // 如何补齐
}
export interface QualityGap {
  kind: '缺规则文档' | '缺数据表' | '缺字段' | '口径不明' | '枚举语义不明' | '关联关系不明';
  target: string;
  message: string;      // 缺什么、为什么重要
  how: string;          // 建议补齐方式：补文档 / 补数据 / 访谈确认
}
export interface QualityReport {
  findings: QualityFinding[];
  gaps: QualityGap[];
  summary: { tables: number; docs: number; 错误: number; 警告: number; 提示: number };
}

type Doc = { filename: string; textContent?: string };

// ---- ① 确定性检查 ----
export function runDataQualityChecks(tables: DataDictTable[], documents: Doc[]): QualityFinding[] {
  const out: QualityFinding[] = [];
  const add = (severity: QualitySeverity, category: string, target: string, message: string, suggestion?: string) =>
    out.push({ severity, category, target, message, suggestion });
  const docsText = documents.map((d) => d.textContent ?? '').join('\n');

  // 整体：规则来源
  if (!documents.some((d) => (d.textContent ?? '').trim().length > 0)) {
    add('警告', '规则来源缺失', '整体',
      '只有数据表、没有任何业务文档——业务规则、计算口径、判定条件将完全依赖访谈补齐',
      '上传业务规范/口径说明/流程文档（md/docx/pdf），或在访谈中系统性补齐规则');
  } else {
    const totalDocChars = documents.reduce((n, d) => n + (d.textContent?.length ?? 0), 0);
    if (totalDocChars < 500 && tables.length > 0) {
      add('提示', '规则来源单薄', '整体', `业务文档合计仅 ${totalDocChars} 字符，规则覆盖可能不足`, '补充口径与流程说明文档');
    }
  }
  if (!tables.length && documents.length) {
    add('警告', '数据来源缺失', '整体', '只有文档、没有数据表——对象字段与数据映射无从建立，评测也无数据可查', '上传 Excel 数据表（sheet=表、首行字段名）');
  }

  // 表级
  for (const t of tables) {
    const target = t.name;
    if (t.rowCount === 0) {
      add('警告', '空表', target, `表 ${t.name} 没有数据行——类型推断与评测数据均缺失`, '补充样例数据（哪怕少量典型行）');
      continue;
    }
    if (t.rowCount < 3) add('提示', '样本过少', target, `表 ${t.name} 仅 ${t.rowCount} 行，类型推断与口径验证可信度低`);

    const noted = t.fields.filter((f) => (f.note ?? '').trim()).length;
    if (noted === 0 && t.fields.length > 0) {
      add('警告', '字段无注释', target,
        `表 ${t.name} 的 ${t.fields.length} 个字段均无中文注释——字段业务含义只能靠猜或访谈`,
        '在 Excel 第二行补充中文注释，或在文档中说明各字段含义');
    } else if (noted / t.fields.length < 0.5 && t.fields.length >= 4) {
      add('提示', '注释覆盖低', target, `表 ${t.name} 注释覆盖 ${noted}/${t.fields.length}，未注释字段语义需访谈确认`);
    }

    if (!t.fields.some((f) => f.distinctRatio === 1) && t.rowCount > 1) {
      add('警告', '主键候选缺失', target, `表 ${t.name} 没有唯一值字段——对象唯一标识与关联映射难以建立`, '确认业务主键（可能是复合键），在访谈中说明');
    }

    for (const f of t.fields) {
      // 高空值率（用全量行统计；无 rows 则跳过）
      if (t.rows?.length) {
        const col = t.fields.indexOf(f);
        const empty = t.rows.filter((r) => (r[col] ?? '') === '').length;
        if (empty / t.rows.length > 0.6) {
          add('提示', '高空值列', `${t.name}.${f.name}`, `空值率 ${Math.round(empty / t.rows.length * 100)}%——该字段不可作为可靠依据`);
        }
      }
      // 未解释的枚举列：低多样性 + 无注释（枚举语义之争的高发地）
      const distinctCount = new Set(t.rows?.map((r) => r[t.fields.indexOf(f)]).filter((v) => v) ?? f.sampleValues).size;
      if (!f.note && t.rowCount >= 4 && distinctCount >= 2 && distinctCount <= 8 && f.distinctRatio <= 0.65
        && f.sampleValues.some((v) => /[一-龥]/.test(v) || /^[A-Z_]{2,12}$/.test(v))) {
        add('警告', '枚举语义不明', `${t.name}.${f.name}`,
          `疑似枚举列（${f.sampleValues.slice(0, 4).join('/')}）且无注释——各枚举值的业务口径（含边界与互斥关系）不明`,
          '在文档或访谈中逐值说明口径（例如哪些值参与统计、哪些排除）');
      }
      // 日期序列号残留（cellDates 已处理大部分，防 csv/文本型序列号）
      if (f.inferredType === 'number' && /DT|DATE|TIME|日期|时间/i.test(f.name)
        && f.sampleValues.some((v) => /^4\d{4}$/.test(v))) {
        add('警告', '疑似日期序列号', `${t.name}.${f.name}`, `字段名含日期语义但值形如 ${f.sampleValues[0]}（Excel 序列号）——建模与查询会失真`, '将该列格式化为日期文本后重新上传');
      }
    }

    // 表在文档中零提及（表义无出处）
    if (docsText && !docsText.includes(t.name)) {
      add('提示', '文档未提及', target, `表 ${t.name} 未在任何文档中出现——其业务定位只能靠字段名推断`);
    }
  }

  // 跨表：同名键列取值互证（悬空外键）
  const keyCols = new Map<string, { table: string; values: Set<string> }>();
  for (const t of tables) {
    for (const f of t.fields) {
      if (f.distinctRatio === 1 && t.rows?.length) {
        const col = t.fields.indexOf(f);
        keyCols.set(f.name, { table: t.name, values: new Set(t.rows.map((r) => r[col]).filter((v) => v)) });
      }
    }
  }
  for (const t of tables) {
    for (const f of t.fields) {
      const k = keyCols.get(f.name);
      if (!k || k.table === t.name || !t.rows?.length || f.distinctRatio === 1) continue;
      const col = t.fields.indexOf(f);
      const vals = t.rows.map((r) => r[col]).filter((v) => v);
      if (!vals.length) continue;
      const hit = vals.filter((v) => k.values.has(v)).length;
      if (hit / vals.length < 0.9) {
        add('警告', '外键悬空', `${t.name}.${f.name}`,
          `与 ${k.table}.${f.name}（主键）同名，但仅 ${Math.round(hit / vals.length * 100)}% 的值能在对方表中找到——关联链路会断`,
          '确认两表关联口径，补齐缺失数据或在访谈中说明例外');
      }
    }
  }
  return out;
}

// ---- ② LLM 缺口分析 ----
export async function runGapAnalysis(
  project: Project,
  tables: DataDictTable[],
  documents: Doc[],
  analysis: unknown,
  llmOverride?: LlmDeps,
): Promise<QualityGap[]> {
  const cap: { gaps?: QualityGap[] } = {};
  const submitGaps = {
    name: 'submit_quality_gaps',
    label: '提交资料缺口',
    description: '提交"构建知识包还缺什么"的结构化缺口清单（资料充分时提交空数组）',
    parameters: Type.Object({
      gaps: Type.Array(Type.Object({
        kind: Type.Union([
          Type.Literal('缺规则文档'), Type.Literal('缺数据表'), Type.Literal('缺字段'),
          Type.Literal('口径不明'), Type.Literal('枚举语义不明'), Type.Literal('关联关系不明'),
        ]),
        target: Type.String(),
        message: Type.String(),
        how: Type.String(),
      })),
    }),
    async execute(_id: string, params: any) {
      cap.gaps = params.gaps ?? [];
      return { content: [{ type: 'text' as const, text: `已记录 ${params.gaps?.length ?? 0} 条缺口。` }], details: {}, terminate: true };
    },
  };

  const sys = [
    '你是建模资料评估员（data_quality_gap）。目标：判断"用现有资料构建可回答领域问题的知识包"还缺什么。',
    '从三个层面找缺口（用 list_tables/describe_table/list_documents/search_documents/read_document 取证后再下结论）：',
    '1) 规则层——领域问题涉及的计算口径/判定条件/流程，在文档中是否有明确出处；没有出处的口径列为「口径不明」或「缺规则文档」；',
    '2) 数据层——回答领域问题所需的数据是否有表/字段支撑；缺的列「缺数据表」或「缺字段」；',
    '3) 语义层——关键枚举值口径、表间关联键是否明确；不明的列「枚举语义不明」「关联关系不明」。',
    '每条缺口：target 写清对象（表/字段/问题主题），message 说明缺什么及影响，how 给补齐方式（补文档/补数据/访谈确认）。',
    '只报会实际影响建包与答题的缺口，不要为凑数报告；资料充分就提交空数组。用 submit_quality_gaps 提交。',
    `项目背景：${project.background || project.domain || project.name}`,
    `初步分析结论：${JSON.stringify(analysis ?? {}).slice(0, 1500)}`,
  ].join('\n');

  const { buildTools } = await import('./tools.js');
  const base = buildTools({ tables, documents, captured: {} });
  const agent = makeAgent(llmOverride ?? buildLlm(), sys, [...base, submitGaps] as any);
  await runAgentCaptured(agent, '请评估现有资料对构建知识包的充分性，并用 submit_quality_gaps 提交缺口清单。',
    () => cap.gaps !== undefined, '请调用 submit_quality_gaps 工具提交缺口清单（不要以正文形式输出）。');
  return cap.gaps ?? [];
}

export function buildQualityReport(findings: QualityFinding[], gaps: QualityGap[], tables: number, docs: number): QualityReport {
  return {
    findings, gaps,
    summary: {
      tables, docs,
      错误: findings.filter((f) => f.severity === '错误').length,
      警告: findings.filter((f) => f.severity === '警告').length,
      提示: findings.filter((f) => f.severity === '提示').length,
    },
  };
}
