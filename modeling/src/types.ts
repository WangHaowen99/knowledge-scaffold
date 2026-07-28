// 建模工具核心类型（跨模块共享）。持久化形态见 db/schema.sql。

export interface AuthUser {
  username: string;
  role: string;
}

// ---- workflow ----
// 固定主干 11 节点 + 4 类动态节点（见 docs/design.md §3）。
export type NodeType =
  | 'project_setup' // 1 项目创建
  | 'ingest' // 2 资料上传与解析
  | 'analyze' // 3 初步分析
  | 'interview' // 4 建模访谈
  | 'competency' // 5 胜任力问题
  | 'generate' // 6 知识包生成
  | 'review' // 7 包评审
  | 'data_source' // 8 数据源配置
  | 'testcases' // 9 评测用例录入
  | 'evaluate' // 10 评测运行
  | 'optimize' // 11 优化建议与应用
  // 动态节点（agent 在 3~6 间按需插入，类型封闭）
  | 'dyn_followup' // 追问访谈
  | 'dyn_deepread' // 深读文档
  | 'dyn_probe' // 数据探查
  | 'dyn_conflict'; // 冲突消解

export type NodeStatus =
  | 'pending' // 未开始
  | 'running' // agent 执行中
  | 'waiting_user' // 挂起等待专家应答
  | 'done' // 完成
  | 'stale'; // 上游变更导致需重规划

export const FIXED_TRUNK: NodeType[] = [
  'project_setup', 'ingest', 'analyze', 'interview',
  'competency', 'generate', 'review', 'data_source', 'testcases', 'evaluate', 'optimize',
];

export const DYNAMIC_NODE_TYPES: NodeType[] = [
  'dyn_followup', 'dyn_deepread', 'dyn_probe', 'dyn_conflict',
];

export interface WorkflowNode {
  id: string;
  projectId: string;
  type: NodeType;
  status: NodeStatus;
  ordinal: number; // 在时间线上的顺序
  title: string;
  reason?: string; // 动态节点的插入理由
  // agent 型节点的可恢复状态：pi 的 state.messages 快照（见 spike-notes.md）
  agentMessages?: unknown[];
  // 节点产物（结构随类型而定，如初步分析结果、访谈批次等）
  output?: unknown;
  createdAt: string;
  updatedAt: string;
}

// ---- 数据字典（Excel 解析产物）----
export interface DataDictField {
  name: string;
  note?: string;
  inferredType: 'string' | 'number' | 'boolean' | 'date' | 'unknown';
  sampleValues: string[];
  distinctRatio: number; // 唯一值比例，接近 1 提示可能是主键
}

export interface DataDictTable {
  name: string; // sheet 名 = 表名
  fields: DataDictField[];
  rowCount: number;
  // 数据行（按 fields 顺序的字符串矩阵）；保留数量由具体解析场景决定。
  rows?: string[][];
}

export interface DocumentRecord {
  id: string;
  projectId: string;
  filename: string;
  kind: 'markdown' | 'text' | 'pdf' | 'word' | 'excel' | 'unknown';
  parseStatus: 'pending' | 'parsed' | 'failed';
  parseError?: string;
  // 文本类：提取的正文；excel：数据字典
  textContent?: string;
  tables?: DataDictTable[];
  createdAt: string;
}

// ---- 初步分析产物（节点 3）----
export interface AnalysisResult {
  domainOverview: string;
  candidateObjects: { name: string; rationale: string; sourceTables: string[] }[];
  openQuestions: { topic: string; why: string }[];
}

export interface Project {
  id: string;
  owner: string;
  name: string;
  domain: string;
  background: string; // 场景背景（节点 1 一次性表单）
  targetUsers?: string;
  glossaryPrefs?: string;
  stage: NodeType; // 当前所处主干阶段
  createdAt: string;
  updatedAt: string;
}

// ---- M2：访谈 ----
export type QuestionKind = 'single' | 'multi' | 'text';

export interface InterviewQuestion {
  id: string;
  kind: QuestionKind;
  q: string;
  options: string[]; // text 型可空
  topic: string; // 四类决策点之一：对象划分/字段含义/关系/口径
}

export interface InterviewBatch {
  id: string;
  projectId: string;
  nodeId?: string;
  ordinal: number;
  questions: InterviewQuestion[];
  answered: boolean;
  createdAt: string;
}

// ---- M2：胜任力问题 ----
export interface CompetencyQuestion {
  id: string;
  projectId: string;
  question: string;
  selected: boolean;
  coveredOperation?: string;
  createdAt: string;
}

// ---- M2：知识包规格（agent 产出 → 渲染器生成标准包）----
// 兼容性约定：库里存量 spec JSON 没有后加字段，因此本节所有新增字段一律可选，
// 渲染器对缺省值回落（不得因旧数据报错）。
export interface SpecField {
  key: string;
  cnName?: string;
  type?: string;
  required?: boolean;
  note?: string;
  alias?: string;
  sourceColumn?: string; // Excel 图导入使用的真实列名；缺省回落字段 key
}
export interface SpecRelation {
  relation: string; // 关系名（如 belongs_to / references）
  target: string;   // 目标对象 key
  note?: string;
}
export interface SpecObject {
  key: string;
  title: string;
  description: string;
  fields: SpecField[];
  aliases?: string[];        // 自然语言别名（frontmatter aliases，规范 §5.1）
  queryBoundary?: string;
  relations?: SpecRelation[]; // 「# 关系」表（规范 §9，推荐）
  sourceTable?: string; // 新生成对象必填：对应数据字典的真实 Sheet 名；可选仅为兼容存量 spec
  primaryKey?: string[];
}
export interface SpecOperation {
  key: string;
  title: string;
  description: string;
  inputs: { param: string; cnName?: string; type: string; required: boolean; note?: string }[];
  outputs: { field: string; cnName?: string; type: string; note?: string }[];
  flow: string[];
  relatedObjects: string[];
  usesRules?: string[];   // frontmatter uses.rules（引用 SpecRule.key）
  usesMetrics?: string[]; // frontmatter uses.metrics（引用 SpecMetric.key）
  notes?: string[];       // 「# 规则、指标和注意事项」的注意事项条目
  // 绑定脚本（frontmatter script 块，规范 §11）。生成链暂不产出脚本（脚本必须注册，
  // 规范 §17 边界），此字段仅为渲染兼容保留。
  script?: { key: string; required?: boolean };
  competencyQuestion?: string; // 该 operation 覆盖的胜任力问题
}
// 规则（规范 §12-13）：可复用的业务判断条件
export interface SpecRule {
  key: string;
  title: string;
  description?: string;
  appliesToObjects?: string[]; // frontmatter applies_to.objects
  content: string;             // 「# 规则内容」（必需）
  conditions?: { condition: string; conclusion: string }[]; // 「# 判断条件」表（推荐）
  exceptions?: string[];       // 「# 例外」列表（推荐）
}
// 指标（规范 §14-15）：有明确口径、公式和边界的业务指标
export interface SpecMetric {
  key: string;
  title: string;
  description?: string;
  unit?: string;
  grain?: string;
  usesObjects?: string[];  // frontmatter uses.objects
  caliber: string;         // 「# 业务口径」（必需，含公式）
  boundaries: string[];    // 「# 计算边界」列表（必需）
  nullPolicy?: { field: string; handling: string }[]; // 「# 空值策略」表（推荐）
  dimensions?: { key: string; note: string }[];       // 「# 维度」表（推荐）
}
export type PackageStatus = 'draft' | 'review' | 'released' | 'deprecated';
export interface PackageSpec {
  id: string;
  name: string;
  description: string;
  version: string;
  language: string;
  status?: PackageStatus; // manifest status（规范 §6 枚举）；缺省 draft
  objects: SpecObject[];
  operations: SpecOperation[];
  rules?: SpecRule[];     // 可选目录 rules/；空或缺省则不生成目录
  metrics?: SpecMetric[]; // 可选目录 metrics/；空或缺省则不生成目录
  defaultProviderAlias: string;
}

export interface CoverageResult {
  covered: { question: string; operation: string }[];
  uncovered: string[]; // 未被任何 operation 覆盖的胜任力问题
}
