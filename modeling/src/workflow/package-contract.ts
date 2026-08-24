import { parse as parseYaml, stringify as stringifyYaml } from 'yaml';

import type { PackageSpec } from '../types.js';
import {
  KNOWLEDGE_MARKDOWN_VALIDATOR_VERSION,
  knowledgeMarkdownGenerationContract,
} from './knowledge-markdown-contract.js';

export const PACKAGE_CONTRACT_VERSION = 'package-contract-v2';
export const PACKAGE_FORMAT_VALIDATOR_VERSION = 'package-format-v2';
export const PACKAGE_MANIFEST_KIND = 'ontology-package';
export const PACKAGE_MANIFEST_SCHEMA_VERSION = '0.1';
export const PACKAGE_STATUSES = ['draft', 'review', 'released', 'deprecated'] as const;
export const MANIFEST_REQUIRED_FIELDS = [
  'kind', 'schema_version', 'id', 'name', 'description', 'version', 'status',
] as const;
export const MANIFEST_PLATFORM_MANAGED_FIELDS = [
  'kind', 'schema_version', 'id', 'version', 'status',
] as const;
export const MANIFEST_AGENT_MUTABLE_FIELDS = ['description', 'resources'] as const;
export const PACKAGE_REQUIRED_FILES = ['manifest.yaml', 'index.md', 'objects/index.md', 'operations/index.md'] as const;
export const PACKAGE_INDEX_RESOURCES: Record<string, string> = {
  'index.md': 'root',
  'objects/index.md': 'object_index',
  'operations/index.md': 'operation_index',
  'rules/index.md': 'rule_index',
  'metrics/index.md': 'metric_index',
  'scripts/index.md': 'script_index',
};
export const PACKAGE_DETAIL_RESOURCES: Record<string, string> = {
  objects: 'object',
  operations: 'operation',
  rules: 'rule',
  metrics: 'metric',
  scripts: 'script',
};
export const PACKAGE_INDEX_DETAIL_DIRECTORIES: ReadonlyArray<readonly [string, string]> = [
  ['objects/index.md', 'objects'],
  ['operations/index.md', 'operations'],
  ['rules/index.md', 'rules'],
  ['metrics/index.md', 'metrics'],
  ['scripts/index.md', 'scripts'],
];

type ManifestRecord = Record<string, unknown>;

export interface ManifestContractIssue {
  ruleId: string;
  path: 'manifest.yaml';
  message: string;
  suggestion: string;
}

export function packageGenerationContract() {
  return {
    validatorVersion: PACKAGE_CONTRACT_VERSION,
    validators: {
      packageFormat: PACKAGE_FORMAT_VALIDATOR_VERSION,
      knowledgeMarkdown: KNOWLEDGE_MARKDOWN_VALIDATOR_VERSION,
    },
    manifest: {
      path: 'manifest.yaml',
      requiredFields: [...MANIFEST_REQUIRED_FIELDS],
      fixedFields: {
        kind: PACKAGE_MANIFEST_KIND,
        schema_version: PACKAGE_MANIFEST_SCHEMA_VERSION,
      },
      allowedStatus: [...PACKAGE_STATUSES],
      platformManagedFields: [...MANIFEST_PLATFORM_MANAGED_FIELDS],
      agentMutableFields: [...MANIFEST_AGENT_MUTABLE_FIELDS],
      instructions: [
        'Agent 只提交 description 和 resources；其他字段由平台从当前正式版本保留',
        '版本和状态只由平台在接受候选或发布时更新',
      ],
    },
    packageFormat: {
      requiredFiles: [...PACKAGE_REQUIRED_FILES],
      markdown: {
        requiredFrontmatterFields: ['resource', 'key', 'title'],
        indexResources: { ...PACKAGE_INDEX_RESOURCES },
        detailResources: { ...PACKAGE_DETAIL_RESOURCES },
        indexDetailDirectories: PACKAGE_INDEX_DETAIL_DIRECTORIES.map(([index, directory]) => ({ index, directory })),
      },
      mappings: {
        path: 'mappings/object-data-mappings.json',
        requiredFields: ['schema_version', 'default_provider_alias', 'objects'],
      },
      scripts: {
        uniqueEntrypoints: true,
        sourceMustPassDeterministicSafetyValidation: true,
        changedScriptsRequireCurrentSandboxTestReceipt: true,
      },
    },
    markdown: knowledgeMarkdownGenerationContract(),
    confirmation: {
      maxAutomaticRepairRounds: 3,
      stopOnRepeatedFindingFingerprint: true,
      rule: '候选必须通过完整确定性预检并保存校验回执后才能冻结并展示给用户确认',
    },
  };
}

export function parsePackageManifest(content: string): ManifestRecord {
  let parsed: unknown;
  try {
    parsed = parseYaml(content);
  } catch {
    throw new Error('manifest.yaml 不是合法 YAML');
  }
  if (!isRecord(parsed)) throw new Error('manifest.yaml 顶层必须是对象');
  return parsed;
}

export function packageManifestIssues(manifest: ManifestRecord): ManifestContractIssue[] {
  const issues: ManifestContractIssue[] = [];
  if (manifest.kind !== PACKAGE_MANIFEST_KIND) {
    issues.push({
      ruleId: 'manifest.kind.ontology-package',
      path: 'manifest.yaml',
      message: `manifest 的 kind 必须为 ${PACKAGE_MANIFEST_KIND}（当前：${String(manifest.kind ?? '缺失')}）`,
      suggestion: `保留 kind: ${PACKAGE_MANIFEST_KIND}`,
    });
  }
  for (const field of MANIFEST_REQUIRED_FIELDS) {
    if (!hasManifestValue(manifest[field])) {
      issues.push({
        ruleId: `manifest.required.${field}`,
        path: 'manifest.yaml',
        message: `manifest 缺少必选字段 ${field}`,
        suggestion: `以当前正式 manifest.yaml 为基线，补回并保留 ${field} 字段`,
      });
    }
  }
  const status = String(manifest.status ?? '');
  if (status && !PACKAGE_STATUSES.includes(status as typeof PACKAGE_STATUSES[number])) {
    issues.push({
      ruleId: 'manifest.status.allowed-value',
      path: 'manifest.yaml',
      message: `manifest 的 status 不正确（当前：${status}）`,
      suggestion: `status 只能是 ${PACKAGE_STATUSES.join('、')}；候选状态由平台统一更新`,
    });
  }
  return issues;
}

/** Agent 只能改业务描述和资源清单；其余字段始终取当前正式基线。 */
export function mergeAgentManifest(
  baselineContent: string,
  currentCandidateContent: string,
  proposedContent: string,
): { content: string; ignoredFields: string[] } {
  const baseline = parsePackageManifest(baselineContent);
  const current = safeManifest(currentCandidateContent, baseline);
  const proposed = parsePackageManifest(proposedContent);
  const merged: ManifestRecord = { ...baseline };
  for (const field of MANIFEST_AGENT_MUTABLE_FIELDS) {
    if (Object.hasOwn(current, field)) merged[field] = current[field];
    if (Object.hasOwn(proposed, field)) merged[field] = proposed[field];
  }
  const ignoredFields = Object.keys(proposed)
    .filter((field) => !MANIFEST_AGENT_MUTABLE_FIELDS.includes(field as typeof MANIFEST_AGENT_MUTABLE_FIELDS[number]))
    .filter((field) => !deepEqual(proposed[field], baseline[field]));
  const issues = packageManifestIssues(merged);
  if (issues.length) throw new Error(issues.map((issue) => issue.message).join('；'));
  return { content: stringifyManifest(merged), ignoredFields: ignoredFields.sort() };
}

/** 接受候选时只更新平台拥有的元数据，并保留已经过预检的业务字段与扩展。 */
export function updateManifestPlatformMetadata(content: string, spec: PackageSpec): string {
  const current = parsePackageManifest(content);
  const updated: ManifestRecord = {
    ...current,
    kind: PACKAGE_MANIFEST_KIND,
    schema_version: PACKAGE_MANIFEST_SCHEMA_VERSION,
    id: spec.id,
    version: spec.version,
    status: spec.status ?? 'draft',
  };
  const issues = packageManifestIssues(updated);
  if (issues.length) throw new Error(issues.map((issue) => issue.message).join('；'));
  return stringifyManifest(updated);
}

function safeManifest(content: string, fallback: ManifestRecord): ManifestRecord {
  try {
    return parsePackageManifest(content);
  } catch {
    return fallback;
  }
}

function stringifyManifest(manifest: ManifestRecord): string {
  return stringifyYaml(manifest, { lineWidth: 0 }).replaceAll('\r\n', '\n');
}

function hasManifestValue(value: unknown): boolean {
  return value !== undefined && value !== null && String(value).trim() !== '';
}

function isRecord(value: unknown): value is ManifestRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function deepEqual(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}
