import { createHash } from 'node:crypto';

import { parse as parseYaml, stringify as stringifyYaml } from 'yaml';

import type {
  PackageSpec,
  SpecMetric,
  SpecObject,
  SpecOperation,
  SpecRule,
  SpecScript,
} from '../types.js';
import {
  normalizeFlowchartDocument,
  renderDiagramFiles,
  validateFlowchartDocument,
  type FlowchartDiagram,
  type FlowchartDocument,
} from '../flowchart/index.js';
import { DIAGRAM_TITLE_MAX_LENGTH, diagramFilePath } from '../flowchart/file-path.js';
import { findObjectAliasConflicts, normalizeObjectIdentifier } from './object-alias-validation.js';
import { runFormatChecks, runRuleChecks } from './package-check.js';
import type { ImportedPackageArchive } from './package-import.js';
import { renderPackage } from './package-render.js';
import { validateSpecIntegrity } from './spec-validation.js';
import { validateKnowledgeMarkdownContract } from './knowledge-markdown-contract.js';

const SEMANTIC_KINDS = ['object', 'operation', 'rule', 'metric', 'script'] as const;
const ALL_RESOURCE_KINDS = [...SEMANTIC_KINDS, 'diagram'] as const;

export type PackageFusionSemanticKind = typeof SEMANTIC_KINDS[number];
export type PackageFusionResourceKind = PackageFusionSemanticKind | 'diagram';
export type PackageFusionLayer = 'common' | 'parameterized' | 'scenario';

export interface PackageFusionInput {
  sourceId: string;
  filename: string;
  archive: ImportedPackageArchive;
}

export interface PackageFusionConceptMember {
  sourceId: string;
  kind: PackageFusionResourceKind;
  key: string;
  relation: 'equivalent' | 'broader' | 'narrower' | 'overlap';
}

export interface PackageFusionConcept {
  id: string;
  title: string;
  layer: Exclude<PackageFusionLayer, 'scenario'>;
  members: PackageFusionConceptMember[];
  decision: 'preserve_views';
  rationale: string;
}

export interface PackageFusionOptions {
  id: string;
  name: string;
  description: string;
  version: string;
  language?: string;
  concepts?: PackageFusionConcept[];
  dispositions?: PackageFusionResourceDisposition[];
}

export interface PackageFusionResourceDisposition {
  sourceId: string;
  kind: PackageFusionResourceKind;
  key: string;
  action: 'preserve' | 'canonicalize' | 'exclude';
  targetSourceId?: string;
  targetKey?: string;
  reason?: string;
}

export interface PackageFusionResourceLedgerEntry {
  kind: PackageFusionResourceKind;
  sourceId: string;
  sourceKey: string;
  fusedKey: string;
  disposition: 'preserved' | 'renamed_collision' | 'canonicalized' | 'excluded';
  layer: PackageFusionLayer;
  conceptIds: string[];
  provenance: string;
  decisionReason?: string;
  canonicalTarget?: {
    sourceId: string;
    key: string;
  };
}

export interface PackageFusionGate {
  id: string;
  passed: boolean;
  detail: string;
}

export interface PackageFusionResult {
  spec: PackageSpec;
  files: Record<string, string | Uint8Array>;
  ledger: {
    schemaVersion: '0.1';
    strategy: 'lossless-semantic-registry';
    sources: Array<{
      sourceId: string;
      namespace: string;
      filename: string;
      packageId: string;
      name: string;
      version: string;
      fingerprint: string;
    }>;
    providers: Array<{
      sourceId: string;
      originalAlias: string;
      fusedAlias: string;
    }>;
    resources: PackageFusionResourceLedgerEntry[];
    concepts: Array<PackageFusionConcept & {
      members: Array<PackageFusionConceptMember & { fusedKey: string }>;
    }>;
    aliasRenames: Array<{
      objectKey: string;
      sourceId: string;
      from: string;
      to: string;
    }>;
  };
  quality: {
    passed: boolean;
    gates: PackageFusionGate[];
    warnings: string[];
    counts: Record<PackageFusionResourceKind, number>;
  };
}

interface PreparedSource extends PackageFusionInput {
  namespace: string;
  fingerprint: string;
  provenance: string;
}

interface ResourceRow {
  source: PreparedSource;
  kind: PackageFusionSemanticKind;
  item: SpecObject | SpecOperation | SpecRule | SpecMetric | SpecScript;
  fusedKey: string;
  disposition: PackageFusionResourceLedgerEntry['disposition'];
  decision: PackageFusionResourceDisposition;
}

interface ObjectMappingDocument {
  default_provider_alias?: string;
  objects?: Record<string, Record<string, unknown>>;
}

const collectionFor = (
  spec: PackageSpec,
  kind: PackageFusionSemanticKind,
): Array<SpecObject | SpecOperation | SpecRule | SpecMetric | SpecScript> => {
  if (kind === 'object') return spec.objects ?? [];
  if (kind === 'operation') return spec.operations ?? [];
  if (kind === 'rule') return spec.rules ?? [];
  if (kind === 'metric') return spec.metrics ?? [];
  return spec.scripts ?? [];
};

function normalizedIdentity(value: string): string {
  return String(value ?? '').normalize('NFKC').trim().toLocaleLowerCase('en-US');
}

function namespaceFor(sourceId: string): string {
  const normalized = sourceId.normalize('NFKC').trim().toLocaleLowerCase('en-US')
    .replace(/[^a-z0-9_-]+/gu, '-')
    .replace(/^-+|-+$/gu, '');
  if (!normalized) throw new Error(`融合来源 ID 不能形成稳定命名空间：${sourceId}`);
  return normalized.slice(0, 24);
}

export function packageFusionSourceFingerprint(archive: ImportedPackageArchive): string {
  const hash = createHash('sha256');
  const files = Object.keys(archive.rawFiles).length ? archive.rawFiles : Object.fromEntries(
    Object.entries(archive.files).map(([path, content]) => [path, new TextEncoder().encode(content)]),
  );
  for (const path of Object.keys(files).sort()) {
    hash.update(path);
    hash.update('\0');
    hash.update(files[path]);
    hash.update('\0');
  }
  return hash.digest('hex');
}

function collisionKey(key: string, namespace: string): string {
  const suffix = `__${namespace}`;
  if (key.length + suffix.length <= 80) return `${key}${suffix}`;
  const digest = createHash('sha256').update(key).update('\0').update(namespace).digest('hex').slice(0, 10);
  return `${key.slice(0, 80 - digest.length - 2)}__${digest}`;
}

function resourceIdentity(sourceId: string, kind: PackageFusionResourceKind, key: string): string {
  return `${sourceId}\0${kind}\0${key}`;
}

function prepareSources(inputs: PackageFusionInput[]): PreparedSource[] {
  if (inputs.length < 2) throw new Error('多包融合至少需要两个来源知识包');
  const ids = new Set<string>();
  const namespaces = new Set<string>();
  return inputs.map((input) => {
    const sourceId = input.sourceId.normalize('NFKC').trim();
    if (!sourceId || ids.has(sourceId)) throw new Error(`融合来源 ID 缺失或重复：${input.sourceId}`);
    ids.add(sourceId);
    const namespace = namespaceFor(sourceId);
    if (namespaces.has(namespace)) throw new Error(`融合来源命名空间重复：${namespace}`);
    namespaces.add(namespace);
    return {
      ...input,
      sourceId,
      namespace,
      fingerprint: packageFusionSourceFingerprint(input.archive),
      provenance: `pkg:${input.archive.spec.name}@${input.archive.spec.version}`,
    };
  }).sort((left, right) => left.sourceId.localeCompare(right.sourceId));
}

function buildResourceRows(
  sources: PreparedSource[],
  requested: PackageFusionResourceDisposition[],
): ResourceRow[] {
  const requestedByIdentity = new Map(requested.map((item) => [
    resourceIdentity(item.sourceId, item.kind, item.key),
    item,
  ]));
  const grouped = new Map<string, Array<{
    source: PreparedSource;
    kind: PackageFusionSemanticKind;
    item: ResourceRow['item'];
  }>>();
  for (const source of sources) {
    for (const kind of SEMANTIC_KINDS) {
      for (const item of collectionFor(source.archive.spec, kind)) {
        const identity = `${kind}:${normalizedIdentity(item.key)}`;
        grouped.set(identity, [...(grouped.get(identity) ?? []), { source, kind, item }]);
      }
    }
  }
  const rows: ResourceRow[] = [];
  for (const group of grouped.values()) {
    const emitted = group.filter((entry) => (
      (requestedByIdentity.get(resourceIdentity(entry.source.sourceId, entry.kind, entry.item.key))?.action ?? 'preserve') === 'preserve'
    ));
    const collided = emitted.length > 1;
    for (const entry of group) {
      const decision = requestedByIdentity.get(resourceIdentity(entry.source.sourceId, entry.kind, entry.item.key)) ?? {
        sourceId: entry.source.sourceId,
        kind: entry.kind,
        key: entry.item.key,
        action: 'preserve' as const,
      };
      rows.push({
        ...entry,
        fusedKey: decision.action === 'preserve'
          ? collided ? collisionKey(entry.item.key, entry.source.namespace) : entry.item.key
          : '',
        disposition: decision.action === 'exclude' ? 'excluded'
          : decision.action === 'canonicalize' ? 'canonicalized'
            : collided ? 'renamed_collision' : 'preserved',
        decision,
      });
    }
  }
  const ordered = rows.sort((left, right) => (
    left.source.sourceId.localeCompare(right.source.sourceId)
    || SEMANTIC_KINDS.indexOf(left.kind) - SEMANTIC_KINDS.indexOf(right.kind)
    || left.item.key.localeCompare(right.item.key)
  ));
  const usedByKind = new Map<PackageFusionSemanticKind, Set<string>>();
  for (const row of ordered.filter((item) => item.decision.action === 'preserve')) {
    const used = usedByKind.get(row.kind) ?? new Set<string>();
    let candidate = row.fusedKey;
    let attempt = 0;
    while (used.has(normalizedIdentity(candidate))) {
      const digest = createHash('sha256')
        .update(row.source.sourceId)
        .update('\0')
        .update(row.kind)
        .update('\0')
        .update(row.item.key)
        .update('\0')
        .update(String(attempt))
        .digest('hex')
        .slice(0, 8);
      candidate = collisionKey(row.item.key, `${row.source.namespace}_${digest}`);
      attempt += 1;
    }
    row.fusedKey = candidate;
    if (candidate !== row.item.key) row.disposition = 'renamed_collision';
    used.add(normalizedIdentity(candidate));
    usedByKind.set(row.kind, used);
  }
  const byIdentity = new Map(ordered.map((row) => [
    resourceIdentity(row.source.sourceId, row.kind, row.item.key),
    row,
  ]));
  for (const row of ordered.filter((item) => item.decision.action === 'canonicalize')) {
    const target = byIdentity.get(resourceIdentity(
      String(row.decision.targetSourceId ?? ''),
      row.kind,
      String(row.decision.targetKey ?? ''),
    ));
    if (!target || target.decision.action !== 'preserve') {
      throw new Error(`归并目标必须是同类型且独立保留的来源元素：${row.source.sourceId}/${row.kind}/${row.item.key}`);
    }
    row.fusedKey = target.fusedKey;
  }
  return ordered;
}

function keyMaps(rows: ResourceRow[]): Map<string, string> {
  return new Map(rows.filter((row) => row.fusedKey).map((row) => [
    resourceIdentity(row.source.sourceId, row.kind, row.item.key),
    row.fusedKey,
  ]));
}

function mappedKey(
  maps: Map<string, string>,
  sourceId: string,
  kind: PackageFusionResourceKind,
  key: string,
): string {
  return maps.get(resourceIdentity(sourceId, kind, key)) ?? key;
}

function validateDecisionClosure(
  sources: PreparedSource[],
  rows: ResourceRow[],
  maps: Map<string, string>,
  nodeMaps: Map<string, Map<string, string>>,
): void {
  const rowByIdentity = new Map(rows.map((row) => [
    resourceIdentity(row.source.sourceId, row.kind, row.item.key),
    row,
  ]));
  const requireTargets = (
    source: PreparedSource,
    fromKind: PackageFusionSemanticKind,
    fromKey: string,
    targetKind: PackageFusionSemanticKind,
    keys: string[] | undefined,
  ) => {
    for (const key of keys ?? []) {
      if (maps.has(resourceIdentity(source.sourceId, targetKind, key))) continue;
      throw new Error(`融合范围未闭合：保留的 ${fromKind}/${fromKey} 仍引用已排除的 ${targetKind}/${key}`);
    }
  };
  for (const source of sources) {
    for (const object of source.archive.spec.objects) {
      if (rowByIdentity.get(resourceIdentity(source.sourceId, 'object', object.key))?.decision.action !== 'preserve') continue;
      requireTargets(source, 'object', object.key, 'object', object.relations?.map((relation) => relation.target));
    }
    for (const operation of source.archive.spec.operations) {
      if (rowByIdentity.get(resourceIdentity(source.sourceId, 'operation', operation.key))?.decision.action !== 'preserve') continue;
      requireTargets(source, 'operation', operation.key, 'object', operation.relatedObjects);
      requireTargets(source, 'operation', operation.key, 'rule', operation.usesRules);
      requireTargets(source, 'operation', operation.key, 'metric', operation.usesMetrics);
      requireTargets(source, 'operation', operation.key, 'script', operation.script ? [operation.script.key] : []);
      const flowEvidence = (operation.evidence ?? []).flatMap((evidence) => (
        [...evidence.matchAll(/flow-node:\s*([^\s,;]+)/gu)].map((match) => match[1])
      ));
      for (const nodeId of flowEvidence) {
        if (!nodeMaps.get(source.sourceId)?.has(nodeId)) {
          throw new Error(`融合范围未闭合：保留的 operation/${operation.key} 仍引用已排除流程中的节点 ${nodeId}`);
        }
      }
    }
    for (const rule of source.archive.spec.rules ?? []) {
      if (rowByIdentity.get(resourceIdentity(source.sourceId, 'rule', rule.key))?.decision.action !== 'preserve') continue;
      requireTargets(source, 'rule', rule.key, 'object', rule.appliesToObjects);
      requireTargets(source, 'rule', rule.key, 'operation', rule.appliesToOperations);
    }
    for (const metric of source.archive.spec.metrics ?? []) {
      if (rowByIdentity.get(resourceIdentity(source.sourceId, 'metric', metric.key))?.decision.action !== 'preserve') continue;
      requireTargets(source, 'metric', metric.key, 'object', metric.usesObjects);
    }
    for (const script of source.archive.spec.scripts ?? []) {
      if (rowByIdentity.get(resourceIdentity(source.sourceId, 'script', script.key))?.decision.action !== 'preserve') continue;
      requireTargets(source, 'script', script.key, 'operation', [script.boundOperation]);
    }
  }
}

function rewriteEvidence(values: string[] | undefined, nodeMap: Map<string, string>): string[] | undefined {
  if (!values?.length) return values;
  const entries = [...nodeMap.entries()].sort((left, right) => right[0].length - left[0].length);
  return values.map((value) => {
    let rewritten = value;
    for (const [sourceNodeId, fusedNodeId] of entries) {
      rewritten = rewritten
        .replaceAll(`flow-node:${sourceNodeId}`, `flow-node:${fusedNodeId}`)
        .replaceAll(`flow-node: ${sourceNodeId}`, `flow-node: ${fusedNodeId}`);
    }
    return rewritten;
  });
}

function rewriteSpec(
  source: PreparedSource,
  rows: ResourceRow[],
  maps: Map<string, string>,
  nodeMap: Map<string, string>,
): PackageSpec {
  const sourceRows = rows.filter((row) => row.source.sourceId === source.sourceId);
  const row = (kind: PackageFusionSemanticKind, key: string) => {
    const found = sourceRows.find((item) => item.kind === kind && item.item.key === key);
    if (!found) throw new Error(`融合资源映射缺失：${source.sourceId}/${kind}/${key}`);
    return found;
  };
  const objects = source.archive.spec.objects.filter((item) => row('object', item.key).decision.action === 'preserve').map((item): SpecObject => ({
    ...item,
    key: row('object', item.key).fusedKey,
    relations: item.relations?.map((relation) => ({
      ...relation,
      target: mappedKey(maps, source.sourceId, 'object', relation.target),
      evidence: rewriteEvidence(relation.evidence, nodeMap),
    })),
  }));
  const operations = source.archive.spec.operations.filter((item) => row('operation', item.key).decision.action === 'preserve').map((item): SpecOperation => ({
    ...item,
    key: row('operation', item.key).fusedKey,
    relatedObjects: item.relatedObjects.map((key) => mappedKey(maps, source.sourceId, 'object', key)),
    usesRules: item.usesRules?.map((key) => mappedKey(maps, source.sourceId, 'rule', key)),
    usesMetrics: item.usesMetrics?.map((key) => mappedKey(maps, source.sourceId, 'metric', key)),
    script: item.script ? {
      ...item.script,
      key: mappedKey(maps, source.sourceId, 'script', item.script.key),
    } : undefined,
    evidence: rewriteEvidence(item.evidence, nodeMap),
  }));
  const rules = (source.archive.spec.rules ?? []).filter((item) => row('rule', item.key).decision.action === 'preserve').map((item): SpecRule => ({
    ...item,
    key: row('rule', item.key).fusedKey,
    appliesToObjects: item.appliesToObjects?.map((key) => mappedKey(maps, source.sourceId, 'object', key)),
    appliesToOperations: item.appliesToOperations?.map((key) => mappedKey(maps, source.sourceId, 'operation', key)),
    evidence: rewriteEvidence(item.evidence, nodeMap),
  }));
  const metrics = (source.archive.spec.metrics ?? []).filter((item) => row('metric', item.key).decision.action === 'preserve').map((item): SpecMetric => ({
    ...item,
    key: row('metric', item.key).fusedKey,
    usesObjects: item.usesObjects?.map((key) => mappedKey(maps, source.sourceId, 'object', key)),
    evidence: rewriteEvidence(item.evidence, nodeMap),
  }));
  const scripts = (source.archive.spec.scripts ?? []).filter((item) => row('script', item.key).decision.action === 'preserve').map((item): SpecScript => ({
    ...item,
    key: row('script', item.key).fusedKey,
    boundOperation: mappedKey(maps, source.sourceId, 'operation', item.boundOperation),
    evidence: rewriteEvidence(item.evidence, nodeMap),
  }));
  return { ...source.archive.spec, objects, operations, rules, metrics, scripts };
}

function resolveScriptEntrypointCollisions(
  specs: PackageSpec[],
  sources: PreparedSource[],
): PackageSpec[] {
  const owners = new Map<string, Array<{ sourceId: string; key: string }>>();
  specs.forEach((spec, index) => {
    for (const script of spec.scripts ?? []) {
      const identity = normalizedIdentity(script.entrypoint);
      owners.set(identity, [...(owners.get(identity) ?? []), {
        sourceId: sources[index].sourceId,
        key: script.key,
      }]);
    }
  });
  const collided = new Set([...owners.entries()]
    .filter(([, entries]) => entries.length > 1)
    .map(([identity]) => identity));
  const allocated = new Map<string, string>();
  const used = new Set<string>();
  specs.forEach((spec, index) => {
    for (const script of spec.scripts ?? []) {
      const extension = script.entrypoint.toLocaleLowerCase('en-US').endsWith('.py') ? '.py' : '';
      const stem = extension ? script.entrypoint.slice(0, -extension.length) : script.entrypoint;
      let candidate = collided.has(normalizedIdentity(script.entrypoint))
        ? `${collisionKey(stem, sources[index].namespace).slice(0, 77)}.py`
        : script.entrypoint;
      let attempt = 0;
      while (used.has(normalizedIdentity(candidate))) {
        const digest = createHash('sha256')
          .update(sources[index].sourceId)
          .update('\0')
          .update(script.key)
          .update('\0')
          .update(String(attempt))
          .digest('hex')
          .slice(0, 8);
        candidate = `${collisionKey(stem, `${sources[index].namespace}_${digest}`).slice(0, 77)}.py`;
        attempt += 1;
      }
      allocated.set(resourceIdentity(sources[index].sourceId, 'script', script.key), candidate);
      used.add(normalizedIdentity(candidate));
    }
  });
  return specs.map((spec, index) => ({
    ...spec,
    scripts: (spec.scripts ?? []).map((script) => ({
      ...script,
      entrypoint: allocated.get(resourceIdentity(sources[index].sourceId, 'script', script.key))!,
    })),
  }));
}

function qualifyDuplicateTitles(
  objects: SpecObject[],
  resourceSource: Map<string, PreparedSource>,
): SpecObject[] {
  const groups = new Map<string, SpecObject[]>();
  for (const object of objects) {
    const identity = normalizeObjectIdentifier(object.title);
    groups.set(identity, [...(groups.get(identity) ?? []), object]);
  }
  const duplicated = new Set([...groups.entries()].filter(([, group]) => group.length > 1).map(([key]) => key));
  return objects.map((object) => {
    if (!duplicated.has(normalizeObjectIdentifier(object.title))) return object;
    const source = resourceSource.get(object.key)!;
    return { ...object, title: `${object.title} (${source.sourceId})` };
  });
}

function splitAliases(values: string[] | undefined): string[] {
  return (values ?? []).flatMap((value) => value.split(/[,，、;\/；]/u))
    .map((value) => value.trim())
    .filter(Boolean);
}

function resolveAliasConflicts(
  objects: SpecObject[],
  resourceSource: Map<string, PreparedSource>,
): {
  objects: SpecObject[];
  renames: PackageFusionResult['ledger']['aliasRenames'];
} {
  let resolved = objects.map((object) => ({ ...object, aliases: splitAliases(object.aliases) }));
  const renames: PackageFusionResult['ledger']['aliasRenames'] = [];
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const conflicts = findObjectAliasConflicts(resolved);
    if (!conflicts.length) return { objects: resolved, renames };
    const conflicting = new Map<string, Set<string>>();
    for (const conflict of conflicts) {
      const objectKeys = conflict.kind === 'shared_alias'
        ? conflict.objectKeys
        : conflict.aliasObjectKeys;
      for (const objectKey of objectKeys) {
        const aliases = conflicting.get(objectKey) ?? new Set<string>();
        aliases.add(conflict.alias);
        conflicting.set(objectKey, aliases);
      }
    }
    resolved = resolved.map((object) => {
      const aliases = conflicting.get(object.key);
      if (!aliases?.size) return object;
      const source = resourceSource.get(object.key)!;
      return {
        ...object,
        aliases: (object.aliases ?? []).map((alias) => {
          if (!aliases.has(normalizeObjectIdentifier(alias))) return alias;
          const next = `${alias} (${source.namespace}:${object.key})`;
          renames.push({ objectKey: object.key, sourceId: source.sourceId, from: alias, to: next });
          return next;
        }),
      };
    });
  }
  return { objects: resolved, renames };
}

function prefixedId(namespace: string, diagramId: string, value: string): string {
  return `${namespace}:${diagramId}:${value}`;
}

interface PreparedDiagram {
  source: PreparedSource;
  sourceOrdinal: number;
  diagram: FlowchartDiagram;
  decision: PackageFusionResourceDisposition;
}

function compactDiagramTitlePart(value: string): string {
  return String(value ?? '').normalize('NFKC').replace(/\s+/gu, ' ').trim();
}

function qualifiedDiagramTitle(titleValue: string, qualifierValue: string): string {
  const title = compactDiagramTitlePart(titleValue);
  const qualifier = compactDiagramTitlePart(qualifierValue).slice(0, 30).trim();
  const separator = ' - ';
  const titleLimit = DIAGRAM_TITLE_MAX_LENGTH - separator.length - qualifier.length;
  return `${title.slice(0, Math.max(1, titleLimit)).trimEnd()}${separator}${qualifier}`;
}

function allocateDiagramTitles(rows: PreparedDiagram[]): Map<string, string> {
  const preserved = rows.filter((row) => row.decision.action === 'preserve');
  const basePathCounts = new Map<string, number>();
  for (const row of preserved) {
    const path = diagramFilePath(row.diagram.title).toLocaleLowerCase('en-US');
    basePathCounts.set(path, (basePathCounts.get(path) ?? 0) + 1);
  }

  const allocated = new Map<string, string>();
  const usedPaths = new Set<string>();
  for (const row of preserved) {
    const identity = resourceIdentity(row.source.sourceId, 'diagram', row.diagram.id);
    const baseTitle = compactDiagramTitlePart(row.diagram.title);
    const basePath = diagramFilePath(baseTitle).toLocaleLowerCase('en-US');
    const sourceName = compactDiagramTitlePart(row.source.archive.spec.name);
    const sourceVersion = compactDiagramTitlePart(row.source.archive.spec.version);
    const qualifiers = [...new Set([
      sourceName,
      `${sourceName} ${sourceVersion}`.trim(),
      `来源 ${row.sourceOrdinal + 1}`,
    ].filter(Boolean))];
    const candidates = [
      ...(basePathCounts.get(basePath) === 1 ? [baseTitle] : []),
      ...qualifiers.map((qualifier) => qualifiedDiagramTitle(baseTitle, qualifier)),
    ];
    const title = candidates.find((candidate) => {
      const path = diagramFilePath(candidate).toLocaleLowerCase('en-US');
      if (usedPaths.has(path)) return false;
      usedPaths.add(path);
      return true;
    });
    if (!title) throw new Error(`无法为融合流程图分配唯一业务名称：${baseTitle}`);
    allocated.set(identity, title);
  }
  return allocated;
}

function mergeDiagrams(
  sources: PreparedSource[],
  requested: PackageFusionResourceDisposition[],
): {
  document: FlowchartDocument;
  nodeMaps: Map<string, Map<string, string>>;
  diagramRows: Array<{
    source: PreparedSource;
    sourceKey: string;
    fusedKey: string;
    decision: PackageFusionResourceDisposition;
    disposition: PackageFusionResourceLedgerEntry['disposition'];
  }>;
} {
  const diagrams: FlowchartDiagram[] = [];
  const nodeMaps = new Map<string, Map<string, string>>();
  const diagramRows: Array<{
    source: PreparedSource;
    sourceKey: string;
    fusedKey: string;
    decision: PackageFusionResourceDisposition;
    disposition: PackageFusionResourceLedgerEntry['disposition'];
  }> = [];
  const requestedByIdentity = new Map(requested.map((item) => [
    resourceIdentity(item.sourceId, item.kind, item.key),
    item,
  ]));
  const documents = sources.map((source) => ({
    source,
    document: source.archive.files['diagrams/processes.json']
      ? normalizeFlowchartDocument(JSON.parse(source.archive.files['diagrams/processes.json']))
      : undefined,
  }));
  const preparedDiagrams: PreparedDiagram[] = documents.flatMap(({ source, document }, sourceOrdinal) => (
    (document?.diagrams ?? []).map((diagram) => ({
      source,
      sourceOrdinal,
      diagram,
      decision: requestedByIdentity.get(resourceIdentity(source.sourceId, 'diagram', diagram.id)) ?? {
        sourceId: source.sourceId,
        kind: 'diagram' as const,
        key: diagram.id,
        action: 'preserve' as const,
      },
    }))
  ));
  const decisionByIdentity = new Map(preparedDiagrams.map((row) => [
    resourceIdentity(row.source.sourceId, 'diagram', row.diagram.id),
    row.decision,
  ]));
  const titleByIdentity = allocateDiagramTitles(preparedDiagrams);
  for (const { source, document } of documents) {
    if (!document) continue;
    const sourceNodeMap = new Map<string, string>();
    const duplicateNodeIds = new Set<string>();
    for (const diagram of document.diagrams) {
      for (const node of diagram.nodes) {
        if (sourceNodeMap.has(node.id)) duplicateNodeIds.add(node.id);
        sourceNodeMap.set(node.id, prefixedId(source.namespace, diagram.id, node.id));
      }
    }
    for (const duplicate of duplicateNodeIds) sourceNodeMap.delete(duplicate);
    nodeMaps.set(source.sourceId, sourceNodeMap);
    for (const diagram of document.diagrams) {
      const identity = resourceIdentity(source.sourceId, 'diagram', diagram.id);
      const decision = decisionByIdentity.get(identity)!;
      if (decision.action !== 'preserve') {
        for (const node of diagram.nodes) sourceNodeMap.delete(node.id);
        diagramRows.push({
          source,
          sourceKey: diagram.id,
          fusedKey: '',
          decision,
          disposition: decision.action === 'exclude' ? 'excluded' : 'canonicalized',
        });
        continue;
      }
      const fusedDiagramId = `${source.namespace}:${diagram.id}`;
      const localNodeMap = new Map(diagram.nodes.map((node) => [
        node.id,
        prefixedId(source.namespace, diagram.id, node.id),
      ]));
      const localEdgeMap = new Map(diagram.edges.map((edge) => [
        edge.id,
        prefixedId(source.namespace, diagram.id, edge.id),
      ]));
      diagrams.push({
        ...diagram,
        id: fusedDiagramId,
        title: titleByIdentity.get(identity)!,
        sourceCandidateId: diagram.sourceCandidateId
          ? `${source.namespace}:${diagram.sourceCandidateId}`
          : undefined,
        nodes: diagram.nodes.map((node) => ({ ...node, id: localNodeMap.get(node.id)! })),
        edges: diagram.edges.map((edge) => ({
          ...edge,
          id: localEdgeMap.get(edge.id)!,
          from: localNodeMap.get(edge.from) ?? edge.from,
          to: localNodeMap.get(edge.to) ?? edge.to,
        })),
        layout: diagram.layout ? {
          ...diagram.layout,
          positions: Object.fromEntries(Object.entries(diagram.layout.positions).map(([nodeId, position]) => [
            localNodeMap.get(nodeId) ?? nodeId,
            position,
          ])),
        } : undefined,
      });
      diagramRows.push({
        source,
        sourceKey: diagram.id,
        fusedKey: fusedDiagramId,
        decision,
        disposition: 'renamed_collision',
      });
    }
  }
  const byIdentity = new Map(diagramRows.map((row) => [
    resourceIdentity(row.source.sourceId, 'diagram', row.sourceKey),
    row,
  ]));
  for (const row of diagramRows.filter((item) => item.decision.action === 'canonicalize')) {
    const target = byIdentity.get(resourceIdentity(
      String(row.decision.targetSourceId ?? ''),
      'diagram',
      String(row.decision.targetKey ?? ''),
    ));
    if (!target || target.decision.action !== 'preserve') {
      throw new Error(`流程图归并目标必须是另一份独立保留的流程图：${row.source.sourceId}/${row.sourceKey}`);
    }
    row.fusedKey = target.fusedKey;
  }
  return { document: { diagrams }, nodeMaps, diagramRows };
}

function providerAlias(namespace: string, alias: string): string {
  const safe = alias.normalize('NFKC').trim().toLocaleLowerCase('en-US')
    .replace(/[^a-z0-9_-]+/gu, '-')
    .replace(/^-+|-+$/gu, '') || 'default';
  return `${namespace}__${safe}`;
}

function updateMappingFrontmatter(
  markdown: string,
  mapping: Record<string, unknown>,
): string {
  const match = markdown.match(/^---\n([\s\S]*?)\n---/u);
  if (!match) throw new Error('融合对象详情缺少 frontmatter');
  const frontmatter = parseYaml(match[1]) as Record<string, unknown>;
  frontmatter.mapping = {
    provider_alias: mapping.provider_alias,
    resource: mapping.resource,
  };
  const yaml = stringifyYaml(frontmatter, { lineWidth: 0 }).trimEnd();
  return `---\n${yaml}\n---${markdown.slice(match[0].length)}`;
}

function mergeMappings(
  sources: PreparedSource[],
  maps: Map<string, string>,
  rows: ResourceRow[],
  files: Record<string, string>,
): PackageFusionResult['ledger']['providers'] {
  const objects: Record<string, unknown> = {};
  const providers = new Map<string, PackageFusionResult['ledger']['providers'][number]>();
  for (const source of sources) {
    const raw = source.archive.files['mappings/object-data-mappings.json'];
    if (!raw) continue;
    const mapping = JSON.parse(raw) as ObjectMappingDocument;
    for (const [sourceKey, sourceMapping] of Object.entries(mapping.objects ?? {})) {
      const row = rows.find((item) => item.source.sourceId === source.sourceId
        && item.kind === 'object' && item.item.key === sourceKey);
      if (!row || row.decision.action !== 'preserve') continue;
      const fusedKey = mappedKey(maps, source.sourceId, 'object', sourceKey);
      const originalAlias = String(sourceMapping.provider_alias ?? mapping.default_provider_alias ?? 'default');
      const fusedAlias = providerAlias(source.namespace, originalAlias);
      const merged = structuredClone(sourceMapping);
      merged.provider_alias = fusedAlias;
      // 当前融合包使用 key 作为 mapping resource；发生 key 改名时两者必须同步。
      merged.resource = fusedKey;
      objects[fusedKey] = merged;
      providers.set(`${source.sourceId}\0${originalAlias}`, {
        sourceId: source.sourceId,
        originalAlias,
        fusedAlias,
      });
      const objectPath = `objects/${fusedKey}.md`;
      if (files[objectPath]) files[objectPath] = updateMappingFrontmatter(files[objectPath], merged);
    }
  }
  files['mappings/object-data-mappings.json'] = `${JSON.stringify({
    schema_version: '0.1',
    default_provider_alias: 'fusion',
    objects,
  }, null, 2)}\n`;
  return [...providers.values()].sort((left, right) => (
    left.sourceId.localeCompare(right.sourceId) || left.originalAlias.localeCompare(right.originalAlias)
  ));
}

function markdownCell(value: string): string {
  return value.replaceAll('|', '\\|').replace(/\s+/gu, ' ').trim();
}

function appendFusionNavigation(index: string, sources: PreparedSource[], conceptCount: number): string {
  return `${index.trimEnd()}\n\n# 融合导航\n\n`
    + '| 来源 ID | 来源知识包 | 版本 |\n| --- | --- | --- |\n'
    + sources.map((source) => (
      `| ${markdownCell(source.sourceId)} | ${markdownCell(source.archive.spec.name)} | ${markdownCell(source.archive.spec.version)} |`
    )).join('\n')
    + `\n\n语义关系登记在 \`mappings/package-fusion.json\`；当前共 ${conceptCount} 组已审阅的跨包概念。`
    + ' “有关联”不等于“可直接合并”，未获等价证据的场景视图继续独立保留。\n';
}

function countsFor(
  spec: PackageSpec,
  diagrams: FlowchartDocument,
): Record<PackageFusionResourceKind, number> {
  return {
    object: spec.objects.length,
    operation: spec.operations.length,
    rule: spec.rules?.length ?? 0,
    metric: spec.metrics?.length ?? 0,
    script: spec.scripts?.length ?? 0,
    diagram: diagrams.diagrams.length,
  };
}

function gate(id: string, passed: boolean, detail: string): PackageFusionGate {
  return { id, passed, detail };
}

function mappingObjectCount(raw: string): number {
  return Object.keys((JSON.parse(raw) as ObjectMappingDocument).objects ?? {}).length;
}

export function fuseKnowledgePackages(
  inputs: PackageFusionInput[],
  options: PackageFusionOptions,
): PackageFusionResult {
  const sources = prepareSources(inputs);
  const requestedDispositions = options.dispositions ?? [];
  const rows = buildResourceRows(sources, requestedDispositions);
  const maps = keyMaps(rows);
  const diagramMerge = mergeDiagrams(sources, requestedDispositions);
  validateDecisionClosure(sources, rows, maps, diagramMerge.nodeMaps);
  const rewritten = resolveScriptEntrypointCollisions(sources.map((source) => rewriteSpec(
    source,
    rows,
    maps,
    diagramMerge.nodeMaps.get(source.sourceId) ?? new Map(),
  )), sources);
  const resourceSource = new Map<string, PreparedSource>();
  for (const row of rows) {
    if (row.kind === 'object' && row.decision.action === 'preserve') resourceSource.set(row.fusedKey, row.source);
  }
  const titledObjects = qualifyDuplicateTitles(rewritten.flatMap((spec) => spec.objects), resourceSource);
  const aliasResolution = resolveAliasConflicts(titledObjects, resourceSource);

  const spec: PackageSpec = {
    id: options.id,
    name: options.name,
    description: options.description,
    version: options.version,
    language: options.language ?? 'zh-CN',
    status: 'draft',
    defaultProviderAlias: 'fusion',
    objects: aliasResolution.objects,
    operations: rewritten.flatMap((item) => item.operations),
    rules: rewritten.flatMap((item) => item.rules ?? []),
    metrics: rewritten.flatMap((item) => item.metrics ?? []),
    scripts: rewritten.flatMap((item) => item.scripts ?? []),
  };

  const conceptInputs = options.concepts ?? [];
  const resolvedConcepts = conceptInputs.map((concept) => ({
    ...concept,
    members: concept.members.map((member) => {
      const fusedKey = member.kind === 'diagram'
        ? diagramMerge.diagramRows.find((row) => (
          row.source.sourceId === member.sourceId && row.sourceKey === member.key
        ))?.fusedKey
        : maps.get(resourceIdentity(member.sourceId, member.kind, member.key));
      if (!fusedKey) {
        throw new Error(`融合概念 ${concept.id} 引用了不存在的成员：${member.sourceId}/${member.kind}/${member.key}`);
      }
      return { ...member, fusedKey };
    }),
  }));
  const conceptsByResource = new Map<string, PackageFusionConcept[]>();
  for (const concept of resolvedConcepts) {
    for (const member of concept.members) {
      const identity = resourceIdentity(member.sourceId, member.kind, member.key);
      conceptsByResource.set(identity, [...(conceptsByResource.get(identity) ?? []), concept]);
    }
  }
  const resourceLedger: PackageFusionResourceLedgerEntry[] = [
    ...rows.map((row) => {
      const concepts = conceptsByResource.get(resourceIdentity(row.source.sourceId, row.kind, row.item.key)) ?? [];
      return {
        kind: row.kind,
        sourceId: row.source.sourceId,
        sourceKey: row.item.key,
        fusedKey: row.fusedKey,
        disposition: row.disposition,
        layer: concepts.some((concept) => concept.layer === 'common')
          ? 'common' as const
          : concepts.some((concept) => concept.layer === 'parameterized')
            ? 'parameterized' as const
            : 'scenario' as const,
        conceptIds: concepts.map((concept) => concept.id).sort(),
        provenance: row.source.provenance,
        ...(row.decision.reason ? { decisionReason: row.decision.reason } : {}),
        ...(row.decision.action === 'canonicalize' ? {
          canonicalTarget: {
            sourceId: row.decision.targetSourceId!,
            key: row.decision.targetKey!,
          },
        } : {}),
      };
    }),
    ...diagramMerge.diagramRows.map((row) => {
      const concepts = conceptsByResource.get(resourceIdentity(row.source.sourceId, 'diagram', row.sourceKey)) ?? [];
      return {
        kind: 'diagram' as const,
        sourceId: row.source.sourceId,
        sourceKey: row.sourceKey,
        fusedKey: row.fusedKey,
        disposition: row.disposition,
        layer: concepts.some((concept) => concept.layer === 'common')
          ? 'common' as const
          : concepts.some((concept) => concept.layer === 'parameterized')
            ? 'parameterized' as const
            : 'scenario' as const,
        conceptIds: concepts.map((concept) => concept.id).sort(),
        provenance: row.source.provenance,
        ...(row.decision.reason ? { decisionReason: row.decision.reason } : {}),
        ...(row.decision.action === 'canonicalize' ? {
          canonicalTarget: {
            sourceId: row.decision.targetSourceId!,
            key: row.decision.targetKey!,
          },
        } : {}),
      };
    }),
  ];

  validateSpecIntegrity(spec, [], undefined, { validateScriptExecutionContract: false });
  const textFiles = renderPackage(spec, { validateScripts: false });
  const providers = mergeMappings(sources, maps, rows, textFiles);
  Object.assign(textFiles, renderDiagramFiles(diagramMerge.document));
  textFiles['index.md'] = appendFusionNavigation(textFiles['index.md'], sources, resolvedConcepts.length);

  const ledger: PackageFusionResult['ledger'] = {
    schemaVersion: '0.1',
    strategy: 'lossless-semantic-registry',
    sources: sources.map((source) => ({
      sourceId: source.sourceId,
      namespace: source.namespace,
      filename: source.filename,
      packageId: source.archive.spec.id,
      name: source.archive.spec.name,
      version: source.archive.spec.version,
      fingerprint: source.fingerprint,
    })),
    providers,
    resources: resourceLedger,
    concepts: resolvedConcepts,
    aliasRenames: aliasResolution.renames,
  };
  textFiles['mappings/package-fusion.json'] = `${JSON.stringify({
    schema_version: ledger.schemaVersion,
    strategy: ledger.strategy,
    sources: ledger.sources,
    providers: ledger.providers,
    resources: ledger.resources,
    concepts: ledger.concepts,
    alias_renames: ledger.aliasRenames,
  }, null, 2)}\n`;

  const files: Record<string, string | Uint8Array> = { ...textFiles };
  for (const source of sources) {
    for (const [path, content] of Object.entries(source.archive.rawFiles)) {
      if (source.archive.files[path] !== undefined) continue;
      files[`assets/fusion/${source.namespace}/${path}`] = new Uint8Array(content);
    }
  }

  const inputCounts = countsFor({
    ...spec,
    objects: sources.flatMap((source) => source.archive.spec.objects),
    operations: sources.flatMap((source) => source.archive.spec.operations),
    rules: sources.flatMap((source) => source.archive.spec.rules ?? []),
    metrics: sources.flatMap((source) => source.archive.spec.metrics ?? []),
    scripts: sources.flatMap((source) => source.archive.spec.scripts ?? []),
  }, {
    diagrams: diagramMerge.diagramRows.map((row) => ({
      id: row.sourceKey, title: row.sourceKey, nodes: [], edges: [],
    })),
  });
  const expectedCounts = ALL_RESOURCE_KINDS.reduce((countsByKind, kind) => ({
    ...countsByKind,
    [kind]: kind === 'diagram'
      ? diagramMerge.diagramRows.filter((row) => row.decision.action === 'preserve').length
      : rows.filter((row) => row.kind === kind && row.decision.action === 'preserve').length,
  }), {} as Record<PackageFusionResourceKind, number>);
  const counts = countsFor(spec, diagramMerge.document);
  const formatErrors = runFormatChecks(spec, textFiles).filter((finding) => finding.severity === '错误');
  const ruleErrors = runRuleChecks(spec, [], [], { validateDataMappings: false })
    .filter((finding) => finding.severity === '错误');
  const contractIssues = validateKnowledgeMarkdownContract(textFiles);
  const flowIssues = validateFlowchartDocument(diagramMerge.document);
  const aliasConflicts = findObjectAliasConflicts(spec.objects);
  const uniqueKeys = SEMANTIC_KINDS.every((kind) => {
    const keys = collectionFor(spec, kind).map((item) => normalizedIdentity(item.key));
    return new Set(keys).size === keys.length;
  });
  const mappingCount = mappingObjectCount(textFiles['mappings/object-data-mappings.json']);
  const expectedMappingCount = sources.reduce((total, source) => {
    const raw = source.archive.files['mappings/object-data-mappings.json'];
    if (!raw) return total;
    const mappedObjects = Object.keys((JSON.parse(raw) as ObjectMappingDocument).objects ?? {})
      .filter((sourceKey) => rows.some((row) => row.source.sourceId === source.sourceId
        && row.kind === 'object' && row.item.key === sourceKey && row.decision.action === 'preserve'));
    return total + mappedObjects.length;
  }, 0);
  const qualityGates = [
    gate(
      'lossless-resource-coverage',
      SEMANTIC_KINDS.every((kind) => counts[kind] === expectedCounts[kind])
        && counts.diagram === expectedCounts.diagram,
      `来源/计划输出/实际输出：${ALL_RESOURCE_KINDS.map((kind) => `${kind} ${inputCounts[kind]}/${expectedCounts[kind]}/${counts[kind]}`).join('，')}`,
    ),
    gate(
      'provenance-ledger-complete',
      resourceLedger.length === Object.values(inputCounts).reduce((sum, count) => sum + count, 0)
        && resourceLedger.every((entry) => entry.provenance
          && (entry.disposition === 'excluded' ? !entry.fusedKey : Boolean(entry.fusedKey))),
      `来源 ${Object.values(inputCounts).reduce((sum, count) => sum + count, 0)} 个元素均已登记，实际输出 ${Object.values(counts).reduce((sum, count) => sum + count, 0)} 个元素`,
    ),
    gate('reference-integrity', ruleErrors.length === 0, ruleErrors.map((item) => item.message).join('；') || '结构化引用全部闭合'),
    gate('markdown-contract', contractIssues.length === 0, contractIssues.join('；') || '详情、索引与正文引用一致'),
    gate('package-format', formatErrors.length === 0, formatErrors.map((item) => item.message).join('；') || '标准包格式检查通过'),
    gate('mapping-preservation', mappingCount === expectedMappingCount, `输入映射 ${expectedMappingCount}，输出映射 ${mappingCount}`),
    gate('flow-integrity', flowIssues.every((issue) => issue.severity !== 'error'), flowIssues.filter((issue) => issue.severity === 'error').map((issue) => issue.message).join('；') || '流程图 ID 与连线端点有效'),
    gate('case-insensitive-key-unique', uniqueKeys, uniqueKeys ? '各类型 key 在大小写归一后仍唯一' : '存在仅大小写不同的同类型 key'),
    gate('alias-unambiguous', aliasConflicts.length === 0, aliasConflicts.length ? `${aliasConflicts.length} 个别名冲突` : '对象别名无二义'),
    gate('semantic-plan-resolved', resolvedConcepts.every((concept) => concept.members.length >= 2), `已解析 ${resolvedConcepts.length} 组跨包概念`),
  ];
  return {
    spec,
    files,
    ledger,
    quality: {
      passed: qualityGates.every((item) => item.passed),
      gates: qualityGates,
      warnings: flowIssues.filter((issue) => issue.severity === 'warning').map((issue) => (
        `${issue.diagramId ?? 'diagram'}: ${issue.message}`
      )),
      counts,
    },
  };
}

export function packageFusionDigest(files: Record<string, string | Uint8Array>): string {
  const hash = createHash('sha256');
  for (const path of Object.keys(files).sort()) {
    hash.update(path);
    hash.update('\0');
    hash.update(files[path]);
    hash.update('\0');
  }
  return hash.digest('hex');
}
