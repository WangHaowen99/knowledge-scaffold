// 建模工具核心类型（跨模块共享）。持久化形态见 db/schema.sql。

export interface AuthUser {
  username: string;
  role: string;
}

export type ProjectKind = 'modeling' | 'package';

// ---- workflow ----
// 固定主干 10 节点 + 4 类动态节点（见 docs/design.md §3）。
export type NodeType =
  | 'project_setup' // 1 项目创建
  | 'ingest' // 2 资料上传与解析
  | 'analyze' // 3 初步分析
  | 'interview' // 4 建模访谈
  | 'generate' // 5 知识包生成
  | 'review' // 6 包评审
  | 'data_source' // 7 数据源配置
  | 'testcases' // 8 评测用例录入
  | 'evaluate' // 9 评测运行
  | 'optimize' // 10 优化建议与应用
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
  | 'stale' // 上游变更导致需重规划
  | 'failed'; // 执行失败，可重试且保留原因与轨迹

export type TraceStatus = 'running' | 'done' | 'warning' | 'error';

export type TraceKind =
  | 'phase_started'
  | 'phase_completed'
  | 'tool_started'
  | 'tool_completed'
  | 'capture_attempt'
  | 'model_summary'
  | 'validation_warning'
  | 'analysis_failed'
  | 'analysis_completed'
  | 'generation_failed'
  | 'generation_completed'
  | 'repair_started'
  | 'repair_completed';

export interface WorkflowTraceEvent {
  id: string;
  projectId: string;
  nodeId: string;
  seq: number;
  phase: string;
  kind: TraceKind;
  status: TraceStatus;
  title: string;
  summary?: string;
  details?: Record<string, unknown>;
  createdAt: string;
}

export const FIXED_TRUNK: NodeType[] = [
  'project_setup', 'ingest', 'analyze', 'interview',
  'generate', 'review', 'data_source', 'testcases', 'evaluate', 'optimize',
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
  sourceColumn?: string;
  note?: string;
  inferredType: 'string' | 'number' | 'boolean' | 'date' | 'unknown';
  sampleValues: string[];
  distinctRatio: number; // 唯一值比例，接近 1 提示可能是主键
  nonEmptyRatio?: number;
}

export type DataDictCellKind = 'blank' | 'string' | 'number' | 'boolean' | 'date';

export interface DataDictCell {
  kind: DataDictCellKind;
  /** 未格式化值的稳定文本表示；date 使用 ISO 8601。 */
  value: string;
  /** Excel 展示值，用于识别 0001 这类带格式编码。 */
  formatted: string;
}

export interface DataDictTable {
  name: string; // sheet 名 = 表名
  documentId?: string; // 来源资料修订 ID；用于精确追踪知识证据
  fields: DataDictField[];
  rowCount: number;
  // 数据行（按 fields 顺序的字符串矩阵）；保留数量由具体解析场景决定。
  rows?: string[][];
  // 图导入按需保留的原始单元格类型信息；不改变通用 rows 的字符串契约。
  cellRows?: DataDictCell[][];
}

export interface DocumentRecord {
  id: string;
  projectId: string;
  sourceKey?: string;
  revisionNo?: number;
  filename: string;
  kind: 'markdown' | 'text' | 'pdf' | 'word' | 'excel' | 'csv' | 'unknown';
  purpose?: 'auto' | 'goal' | 'schema' | 'evidence';
  readingMode?: 'on_demand' | 'full';
  directiveRevision?: number;
  parseStatus: 'pending' | 'parsed' | 'failed';
  parseError?: string;
  // 文本类：提取的正文；excel：数据字典
  textContent?: string;
  tables?: DataDictTable[];
  contentHash?: string;
  semanticHash?: string;
  lifecycleStatus?: 'active' | 'superseded' | 'deleted';
  supersedesId?: string;
  createdAt: string;
  retiredAt?: string;
}

// ---- 初步分析产物（节点 3）----
export type KnowledgeArtifactKind = 'object' | 'rule' | 'operation' | 'metric';

export interface MaterialAssessment {
  source: string;
  sourceType: 'table' | 'document';
  suitableFor: KnowledgeArtifactKind[];
  rationale: string;
}

export interface ArtifactCandidate {
  kind: KnowledgeArtifactKind;
  name: string;
  rationale: string;
  sources: string[];
  evidence?: string;
  readiness: 'extractable' | 'needs_clarification';
}

export type CandidateDispositionStatus =
  | 'generated'
  | 'merged'
  | 'deferred'
  | 'needs_clarification'
  | 'excluded';

export interface CandidateDisposition {
  candidateId: string;
  kind: KnowledgeArtifactKind;
  name: string;
  status: CandidateDispositionStatus;
  resourceKeys?: string[];
  mergedIntoCandidateId?: string;
  reason: string;
}

/** 后端全文读取账本的持久化快照；不能由模型自行声明。 */
export interface DocumentReadCoverage {
  documentId?: string;
  filename: string;
  revisionFingerprint?: string;
  totalChars: number;
  ranges: Array<[number, number]>;
  contiguousEnd: number;
  eofSeen: boolean;
  complete: boolean;
}

export interface ModelingBrief {
  problem: string;
  targetUsers: string;
  scenarios: string;
  successCriteria: string;
}

export interface CandidateBusinessGoal {
  title: string;
  problem: string;
  targetUsers: string[];
  scenarios: string[];
  successCriteria: string[];
  rationale: string;
}

export interface CandidateBusinessProcess {
  candidateId?: string;
  title: string;
  trigger: string;
  actors: string[];
  inputs: string[];
  steps: string[];
  exceptions: string[];
  outputs: string[];
  handoffs: string[];
  rationale: string;
}

export interface AnalysisResult {
  domainOverview: string;
  modelingBrief?: ModelingBrief;
  candidateObjects: { name: string; rationale: string; sourceTables: string[] }[];
  openQuestions: { topic: string; why: string }[];
  candidateGoals?: CandidateBusinessGoal[];
  candidateProcesses?: CandidateBusinessProcess[];
  materialAssessments?: MaterialAssessment[];
  candidateArtifacts?: ArtifactCandidate[];
  documentReadCoverage?: DocumentReadCoverage[];
}

export type ProjectSensitivity = 'standard' | 'restricted';

export type ConfigurableWorkflowStep =
  | 'scenario'
  | 'data_source'
  | 'evaluate'
  | 'diagnose'
  | 'optimize';

export interface ProjectWorkflowProfile {
  enabledSteps: ConfigurableWorkflowStep[];
  requiredSteps: ConfigurableWorkflowStep[];
  releaseRequested: boolean;
  externalResearch: boolean;
}

export interface Project {
  id: string;
  owner: string;
  kind: ProjectKind;
  runtime: 'legacy' | 'agent';
  name: string;
  domain: string;
  background: string; // 场景背景（节点 1 一次性表单）
  targetUsers?: string;
  glossaryPrefs?: string;
  sensitivityLevel: ProjectSensitivity;
  workflowProfile: ProjectWorkflowProfile;
  stage: NodeType; // 当前所处主干阶段
  createdAt: string;
  updatedAt: string;
}

export interface InterviewProcessStep {
  id: string;
  label: string;
  kind: 'action' | 'decision';
  detail?: string;
}

export interface InterviewProcessTransition {
  from: string;
  to: string;
  label?: string;
}

export interface InterviewProcess {
  id: string;
  title: string;
  trigger: string;
  actors: string[];
  inputs: string[];
  steps: InterviewProcessStep[];
  transitions: InterviewProcessTransition[];
  exceptions: string[];
  outputs: string[];
  handoffs: string[];
}

export interface InterviewOutcome {
  summary: string;
  processes: InterviewProcess[];
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
  valueBindingRef?: string; // Data Mapping value_bindings key；经用户确认后写入
}
export interface SpecValueBinding {
  key: string;
  resource: string;
  valueField: string;
  semanticField: string;
  providerAlias?: string;
  aliasFields?: string[];
}
export interface SpecRelation {
  relation: string; // 关系名（如 belongs_to / references）
  target: string;   // 目标对象 key
  note?: string;
  evidence?: string[];
}
export interface TableSourceRef {
  kind: 'table';
  documentId?: string;
  table: string;
  columns?: string[];
}
export interface DocumentSourceAnchor {
  heading?: string;
  offset?: number;
  quote?: string;
}
export interface DocumentSourceReadCoverage {
  revisionFingerprint?: string;
  totalChars: number;
  ranges: Array<[number, number]>;
  contiguousEnd: number;
  eofSeen: boolean;
  complete: boolean;
}
export interface DocumentSourceRef {
  kind: 'document';
  documentId?: string;
  filename: string;
  anchors?: DocumentSourceAnchor[];
  /** 与 anchors 分离：anchors 是证据引用点，readCoverage 才是后端验证的阅读范围。 */
  readCoverage?: DocumentSourceReadCoverage;
}
export type SourceRef = TableSourceRef | DocumentSourceRef;
export interface SpecObject {
  candidateId?: string;
  key: string;
  title: string;
  description: string;
  fields: SpecField[];
  aliases?: string[];        // 自然语言别名（frontmatter aliases，规范 §5.1）
  queryBoundary?: string;
  relations?: SpecRelation[]; // 「# 关系」表（规范 §9，推荐）
  sourceRefs?: SourceRef[];
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
  competencyQuestionId?: string;
  competencyQuestion?: string; // 该 operation 覆盖的胜任力问题
  evidence?: string[];
}
// 规则（规范 §12-13）：可复用的业务判断条件
export interface SpecRule {
  key: string;
  title: string;
  description?: string;
  appliesToObjects?: string[]; // frontmatter applies_to.objects
  appliesToOperations?: string[]; // frontmatter applies_to.operations
  content: string;             // 「# 规则内容」（必需）
  conditions?: { condition: string; conclusion: string }[]; // 「# 判断条件」表（推荐）
  exceptions?: string[];       // 「# 例外」列表（推荐）
  evidence?: string[];
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
  evidence?: string[];
}
export interface SpecScript {
  key: string;
  title: string;
  description: string;
  boundOperation: string;
  runtime: 'python';
  entrypoint: string;
  timeoutSeconds: number;
  parameters: {
    name: string;
    flag: string;
    type: string;
    required: boolean;
    defaultValue?: string | number | boolean;
    description?: string;
    multiple?: boolean;
  }[];
  outputs: { key: string; type: string; description?: string }[];
  boundaries: string[];
  evidence?: string[];
  sourceCode: string;
}
export type PackageStatus = 'draft' | 'review' | 'released' | 'deprecated';
export type PackageDataSourceType = 'nl2sql' | 'mindsdb' | 'neo4j';
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
  scripts?: SpecScript[]; // 可选受控 Python 脚本；须绑定包内 operation
  defaultProviderAlias: string;
  dataSourceType?: PackageDataSourceType; // 缺省仅用于兼容历史版本，沿用 object key resource
  /** 全量分析候选的处理账本；用于证明候选已生成、合并、延期或明确排除。 */
  candidateDispositions?: CandidateDisposition[];
  valueBindings?: SpecValueBinding[]; // Data Mapping 顶层 value_bindings，可由批量确认生成
}

export interface CoverageResult {
  covered: { question: string; operation: string }[];
  uncovered: string[]; // 未被任何 operation 覆盖的胜任力问题
}
