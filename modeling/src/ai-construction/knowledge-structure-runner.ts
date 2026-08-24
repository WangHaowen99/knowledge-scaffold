import type { FlowchartOutput } from '../authoring/workflow-extras.js';
import type { DataDictTable, PackageSpec, Project } from '../types.js';
import { generatePackage } from '../workflow/m2.js';
import type { PackageGenerationOptions } from '../workflow/m2.js';
import {
  runFormatChecks,
  runRuleChecks,
  saveCheck,
  type CheckFinding,
} from '../workflow/package-check.js';
import { GenerationWaitingForExpert } from '../workflow/generation-gate.js';
import { AiConstructionOrchestrator } from './orchestrator.js';

export interface KnowledgeGenerationContext {
  tables: DataDictTable[];
  documents: Array<{ id: string; filename: string; textContent?: string }>;
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
  options?: PackageGenerationOptions,
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

function findingCounts(findings: CheckFinding[]): Omit<KnowledgeStructureValidation, 'findings'> {
  return {
    errors: findings.filter((item) => item.severity === '错误').length,
    warnings: findings.filter((item) => item.severity === '警告').length,
    hints: findings.filter((item) => item.severity === '提示').length,
  };
}

export async function inspectGeneratedKnowledge(
  _project: Project,
  generated: GeneratedKnowledgeStructure,
  context: KnowledgeGenerationContext,
): Promise<KnowledgeStructureValidation> {
  const findings = [
    ...runFormatChecks(generated.spec, generated.files),
    ...runRuleChecks(generated.spec, context.tables, []),
  ]
    .sort((left, right) => ['错误', '警告', '提示'].indexOf(left.severity)
      - ['错误', '警告', '提示'].indexOf(right.severity));
  return { ...findingCounts(findings), findings };
}

export async function validateGeneratedKnowledge(
  project: Project,
  generated: GeneratedKnowledgeStructure,
  context: KnowledgeGenerationContext,
): Promise<KnowledgeStructureValidation> {
  const validation = await inspectGeneratedKnowledge(project, generated, context);
  saveCheck(project.id, generated.version, validation.findings, generated.revisionId);
  return validation;
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
    options: PackageGenerationOptions = {},
  ) {
    this.orchestrator.startKnowledgeStructure(project.id);
    try {
      const generated = await this.generator(project, context, undefined, flowchart, options);
      this.orchestrator.startKnowledgeStructureValidation(project.id, {
        packageRevisionOid: generated.revisionId,
        messageKey: `knowledge.validating.${generated.version}`,
        version: generated.version,
      });
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
      if (error instanceof GenerationWaitingForExpert) {
        const waiting = this.orchestrator.waitForKnowledgeStructureDecision(project.id, {
          failure: error.failure,
          interactionId: error.interaction.id,
        });
        return { status: 'waiting_user' as const, interaction: error.interaction, ...waiting };
      }
      const reason = error instanceof Error ? error.message : String(error);
      try { this.orchestrator.failKnowledgeStructure(project.id, reason); } catch { /* 保留原始错误 */ }
      throw error;
    }
  }
}
