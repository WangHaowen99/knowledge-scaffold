import type { FlowchartOutput } from '../authoring/workflow-extras.js';
import type { DataDictTable, PackageSpec, Project } from '../types.js';
import { generatePackage } from '../workflow/m2.js';
import {
  runFormatChecks,
  runRuleChecks,
  runSemanticCheck,
  saveCheck,
  type CheckFinding,
} from '../workflow/package-check.js';
import { AiConstructionOrchestrator } from './orchestrator.js';

export interface KnowledgeGenerationContext {
  tables: DataDictTable[];
  documents: Array<{ filename: string; textContent?: string }>;
}

export interface KnowledgeStructureValidation {
  errors: number;
  warnings: number;
  hints: number;
  findings: CheckFinding[];
}

export interface GeneratedKnowledgeStructure {
  spec: PackageSpec;
  coverage: Awaited<ReturnType<typeof generatePackage>>['coverage'];
  files: Record<string, string>;
  version: string;
  revisionId: string;
}

export type KnowledgePackageGenerator = (
  project: Project,
  context: KnowledgeGenerationContext,
  llmOverride: undefined,
  flowchart: FlowchartOutput,
) => Promise<GeneratedKnowledgeStructure>;

export type KnowledgeStructureValidator = (
  project: Project,
  generated: GeneratedKnowledgeStructure,
  context: KnowledgeGenerationContext,
) => Promise<KnowledgeStructureValidation>;

export interface KnowledgeStructureRunnerOptions {
  orchestrator: AiConstructionOrchestrator;
  generator?: KnowledgePackageGenerator;
  validator?: KnowledgeStructureValidator;
}

export function confirmedFlowchartFromProgress(progress: {
  status: string;
  output: Record<string, unknown>;
} | null): FlowchartOutput {
  if (!progress || progress.status !== 'completed') {
    throw new Error('业务流程图尚未完成人工确认，不能生成知识结构');
  }
  const diagrams = progress.output.diagrams;
  if (!Array.isArray(diagrams) || !diagrams.length) {
    throw new Error('已确认流程中没有可用流程，不能生成知识结构');
  }
  if (diagrams.some((diagram) => (
    !diagram || typeof diagram !== 'object'
    || !String((diagram as any).id ?? '').trim()
    || !String((diagram as any).title ?? '').trim()
    || !Array.isArray((diagram as any).nodes)
    || !Array.isArray((diagram as any).edges)
  ))) {
    throw new Error('已确认流程图格式不正确，请重新确认流程图');
  }
  return { diagrams: diagrams as FlowchartOutput['diagrams'] };
}

function findingCounts(findings: CheckFinding[]): Omit<KnowledgeStructureValidation, 'findings'> {
  return {
    errors: findings.filter((item) => item.severity === '错误').length,
    warnings: findings.filter((item) => item.severity === '警告').length,
    hints: findings.filter((item) => item.severity === '提示').length,
  };
}

export async function validateGeneratedKnowledge(
  project: Project,
  generated: GeneratedKnowledgeStructure,
  context: KnowledgeGenerationContext,
): Promise<KnowledgeStructureValidation> {
  const deterministic = [
    ...runFormatChecks(generated.spec, generated.files),
    ...runRuleChecks(generated.spec, context.tables, []),
  ];
  let semantic: CheckFinding[];
  try {
    semantic = await runSemanticCheck(project, generated.spec);
  } catch (error) {
    semantic = [{
      id: `semantic_check_failed_${Date.now()}`,
      source: 'semantic',
      severity: '错误',
      category: '语义校验执行失败',
      target: 'package',
      message: error instanceof Error ? error.message : String(error),
      suggestion: '重新执行知识结构校验；若仍失败，请检查任务模型配置和服务连通性',
    }];
  }
  const findings = [...deterministic, ...semantic]
    .sort((left, right) => ['错误', '警告', '提示'].indexOf(left.severity)
      - ['错误', '警告', '提示'].indexOf(right.severity));
  saveCheck(project.id, generated.version, findings, generated.revisionId);
  return { ...findingCounts(findings), findings };
}

/**
 * Runs the long knowledge-generation step while keeping the deterministic task graph honest.
 * The route owns auth/leases; this class owns task transitions, validation and retry state.
 */
export class KnowledgeStructureRunner {
  private readonly orchestrator: AiConstructionOrchestrator;
  private readonly generator: KnowledgePackageGenerator;
  private readonly validator: KnowledgeStructureValidator;

  constructor(options: KnowledgeStructureRunnerOptions) {
    this.orchestrator = options.orchestrator;
    this.generator = options.generator ?? generatePackage;
    this.validator = options.validator ?? validateGeneratedKnowledge;
  }

  async run(
    project: Project,
    context: KnowledgeGenerationContext,
    flowchart: FlowchartOutput,
  ) {
    this.orchestrator.startKnowledgeStructure(project.id);
    try {
      const generated = await this.generator(project, context, undefined, flowchart);
      const validation = await this.validator(project, generated, context);
      const counts = {
        operations: generated.spec.operations.length,
        objects: generated.spec.objects.length,
        relations: generated.spec.objects.reduce(
          (total, object) => total + (object.relations?.length ?? 0),
          0,
        ),
        rules: generated.spec.rules?.length ?? 0,
        metrics: generated.spec.metrics?.length ?? 0,
      };
      const completed = this.orchestrator.completeKnowledgeStructure(project.id, {
        packageRevisionOid: generated.revisionId,
        messageKey: `knowledge.generated.${generated.version}`,
        version: generated.version,
        counts,
        validation: {
          errors: validation.errors,
          warnings: validation.warnings,
          hints: validation.hints,
          findings: validation.findings.slice(0, 8).map((finding) => ({
            severity: finding.severity === '错误' ? 'error' as const : finding.severity === '警告' ? 'warning' as const : 'hint' as const,
            title: finding.category, summary: finding.message, ...(finding.target ? { target: finding.target } : {}),
          })),
        },
      });
      return { ...generated, validation, ...completed };
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      try { this.orchestrator.failKnowledgeStructure(project.id, reason); } catch { /* 保留原始错误 */ }
      throw error;
    }
  }
}
