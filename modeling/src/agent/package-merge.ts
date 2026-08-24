import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { nowIso } from '../db/index.js';
import type { PackageSpec } from '../types.js';
import type { ImportedPackageArchive } from '../workflow/package-import.js';
import { packageFusionSourceFingerprint } from '../workflow/package-fusion.js';

export const MERGE_SCOPES = ['objects', 'operations', 'rules', 'metrics', 'diagrams', 'scripts', 'mappings'] as const;
export type MergeScope = typeof MERGE_SCOPES[number];

export interface PackageMergeSource {
  id: string;
  projectId: string;
  filename: string;
  packageId: string;
  name: string;
  version: string;
  spec: PackageSpec;
  files: Record<string, string>;
  uploadedBy: string;
  createdAt: string;
}

export interface PackageFusionStoredSource extends PackageMergeSource {
  fingerprint: string;
  archive: ImportedPackageArchive;
}

export interface PackageMergeConflict {
  id: string;
  k: string;
  kind: 'path' | 'key' | 'alias';
  mine: string;
  theirs: string;
  currentPath: string;
  sourcePath: string;
  options: string[];
}

export function storePackageMergeSource(
  db: DatabaseSync,
  input: { projectId: string; filename: string; uploadedBy: string; archive: ImportedPackageArchive },
): PackageMergeSource {
  const id = `pkgsrc_${randomUUID().slice(0, 12)}`;
  const createdAt = nowIso();
  const files = Object.fromEntries(Object.entries(input.archive.files)
    .filter(([path]) => MERGE_SCOPES.some((scope) => path.startsWith(`${scope}/`))));
  const fingerprint = packageFusionSourceFingerprint(input.archive);
  const rawFiles = Object.keys(input.archive.rawFiles).length
    ? input.archive.rawFiles
    : Object.fromEntries(Object.entries(input.archive.files).map(([path, content]) => [
      path,
      new TextEncoder().encode(content),
    ]));
  db.exec('SAVEPOINT store_agent_package_source');
  try {
    db.prepare(`INSERT INTO agent_package_sources
      (id, project_id, filename, package_id, package_name, package_version,
        spec_json, files_json, uploaded_by, created_at)
      VALUES (?,?,?,?,?,?,?,?,?,?)`).run(
        id,
        input.projectId,
        input.filename,
        input.archive.spec.id,
        input.archive.spec.name,
        input.archive.spec.version,
        JSON.stringify(input.archive.spec),
        JSON.stringify(files),
        input.uploadedBy,
        createdAt,
      );
    db.prepare(`INSERT INTO agent_package_source_archives
      (source_id, fingerprint, coverage_json, text_files_json, warnings_json) VALUES (?,?,?,?,?)`).run(
        id,
        fingerprint,
        JSON.stringify(input.archive.coverage),
        JSON.stringify(input.archive.files),
        JSON.stringify(input.archive.warnings),
      );
    const insertFile = db.prepare(`INSERT INTO agent_package_source_files
      (source_id, path, content) VALUES (?,?,?)`);
    for (const [path, content] of Object.entries(rawFiles).sort(([left], [right]) => left.localeCompare(right))) {
      insertFile.run(id, path, content);
    }
    db.exec('RELEASE SAVEPOINT store_agent_package_source');
  } catch (error) {
    db.exec('ROLLBACK TO SAVEPOINT store_agent_package_source');
    db.exec('RELEASE SAVEPOINT store_agent_package_source');
    throw error;
  }
  return {
    id,
    projectId: input.projectId,
    filename: input.filename,
    packageId: input.archive.spec.id,
    name: input.archive.spec.name,
    version: input.archive.spec.version,
    spec: input.archive.spec,
    files,
    uploadedBy: input.uploadedBy,
    createdAt,
  };
}

export function getPackageFusionStoredSource(
  db: DatabaseSync,
  projectId: string,
  sourceId: string,
): PackageFusionStoredSource | null {
  const source = getPackageMergeSource(db, projectId, sourceId);
  if (!source) return null;
  const archiveRow = db.prepare(`SELECT fingerprint, coverage_json, text_files_json, warnings_json
    FROM agent_package_source_archives WHERE source_id = ?`).get(sourceId) as any;
  const storedFiles = db.prepare(`SELECT path, content FROM agent_package_source_files
    WHERE source_id = ? ORDER BY path`).all(sourceId) as Array<{ path: string; content: Uint8Array }>;
  const rawFiles = storedFiles.length
    ? Object.fromEntries(storedFiles.map((row) => [row.path, new Uint8Array(row.content)]))
    : Object.fromEntries(Object.entries(source.files).map(([path, content]) => [
      path,
      new TextEncoder().encode(content),
    ]));
  const archive: ImportedPackageArchive = {
    spec: source.spec,
    coverage: archiveRow ? JSON.parse(archiveRow.coverage_json) : { covered: [], uncovered: [] },
    files: archiveRow ? JSON.parse(archiveRow.text_files_json) : source.files,
    rawFiles,
    warnings: archiveRow ? JSON.parse(archiveRow.warnings_json) : ['来源由旧版本保存，可能缺少根文件或二进制资产'],
  };
  return {
    ...source,
    fingerprint: archiveRow?.fingerprint ?? packageFusionSourceFingerprint(archive),
    archive,
  };
}

export function listPackageMergeSources(db: DatabaseSync, projectId: string): PackageMergeSource[] {
  return (db.prepare(`SELECT * FROM agent_package_sources
    WHERE project_id = ? ORDER BY created_at DESC, rowid DESC`).all(projectId) as any[])
    .map(packageSourceFromRow);
}

export function getPackageMergeSource(
  db: DatabaseSync,
  projectId: string,
  sourceId: string,
): PackageMergeSource | null {
  const row = db.prepare(`SELECT * FROM agent_package_sources
    WHERE project_id = ? AND id = ?`).get(projectId, sourceId) as any;
  return row ? packageSourceFromRow(row) : null;
}

export function analyzePackageMerge(input: {
  currentFiles: Record<string, string>;
  currentSpec: PackageSpec;
  source: PackageMergeSource;
  selectedScopes?: string[];
}) {
  const available = MERGE_SCOPES.filter((scope) => (
    Object.keys(input.source.files).some((path) => path.startsWith(`${scope}/`))
  ));
  const requested = input.selectedScopes?.length ? input.selectedScopes : available;
  const selected = new Set(requested.filter((scope): scope is MergeScope => available.includes(scope as MergeScope)));
  const selectedFiles = Object.fromEntries(Object.entries(input.source.files)
    .filter(([path]) => selected.has(path.split('/')[0] as MergeScope)));
  const conflicts = mergeConflicts(input.currentFiles, input.currentSpec, input.source, selectedFiles);
  const brokenReferences = findBrokenReferences(input.currentSpec, input.source.spec, selected);
  const scope = available.map((key) => ({
    k: key,
    label: `${key}/`,
    n: Object.keys(input.source.files).filter((path) => path.startsWith(`${key}/`)).length,
    adds: conflicts.filter((item) => item.sourcePath.startsWith(`${key}/`)).length,
    selected: selected.has(key),
  }));
  return {
    source: `${input.source.name} ${input.source.version}`,
    sourceId: input.source.id,
    provenance: `pkg:${input.source.name}@${input.source.version}`,
    selectedScopes: [...selected],
    selectedPaths: Object.keys(selectedFiles).sort(),
    scope,
    conflicts,
    validation: {
      passed: brokenReferences.length
        ? ''
        : `key 与引用预检通过，选中 ${Object.keys(selectedFiles).length} 个文件`,
      blocker: brokenReferences.length ? `引用断裂：${brokenReferences.join('；')}` : '',
      brokenReferences,
    },
  };
}

function mergeConflicts(
  currentFiles: Record<string, string>,
  currentSpec: PackageSpec,
  source: PackageMergeSource,
  selectedFiles: Record<string, string>,
): PackageMergeConflict[] {
  const result = new Map<string, PackageMergeConflict>();
  const add = (conflict: PackageMergeConflict) => result.set(conflict.id, conflict);
  for (const [path, theirs] of Object.entries(selectedFiles)) {
    const mine = currentFiles[path];
    if (mine === undefined || mine === theirs) continue;
    add({
      id: `path:${path}`, k: path, kind: 'path', currentPath: path, sourcePath: path,
      mine: summarize(mine), theirs: summarize(theirs),
      options: ['保留当前', '采用来源', '重命名共存'],
    });
  }

  for (const kind of ['objects', 'operations', 'rules', 'metrics', 'scripts'] as const) {
    const current = new Map((currentSpec[kind] ?? []).map((item: any) => [String(item.key), item]));
    for (const item of source.spec[kind] ?? []) {
      if (!current.has(item.key)) continue;
      const sourcePath = findResourcePath(selectedFiles, kind, item.key);
      if (!sourcePath) continue;
      const currentPath = findResourcePath(currentFiles, kind, item.key) ?? sourcePath;
      const id = `key:${kind}:${item.key}`;
      if ([...result.values()].some((existing) => (
        existing.sourcePath === sourcePath && existing.currentPath === currentPath
      ))) continue;
      add({
        id, k: item.key, kind: 'key', currentPath, sourcePath,
        mine: resourceSummary(current.get(item.key)), theirs: resourceSummary(item),
        options: ['保留当前', '采用来源', '重命名共存'],
      });
    }
  }

  const currentAliases = new Map<string, { key: string; path: string }>();
  for (const item of currentSpec.objects ?? []) {
    const path = findResourcePath(currentFiles, 'objects', item.key) ?? `objects/${item.key}.md`;
    for (const alias of item.aliases ?? []) currentAliases.set(normalize(alias), { key: item.key, path });
  }
  for (const item of source.spec.objects ?? []) {
    const sourcePath = findResourcePath(selectedFiles, 'objects', item.key);
    if (!sourcePath) continue;
    for (const alias of item.aliases ?? []) {
      const collided = currentAliases.get(normalize(alias));
      if (!collided || collided.key === item.key) continue;
      add({
        id: `alias:${normalize(alias)}:${item.key}`, k: alias, kind: 'alias',
        currentPath: collided.path, sourcePath,
        mine: `别名“${alias}”属于 ${collided.key}`,
        theirs: `别名“${alias}”属于 ${item.key}`,
        options: ['保留当前', '采用来源', '重命名共存'],
      });
    }
  }
  return [...result.values()].sort((left, right) => left.id.localeCompare(right.id));
}

function findBrokenReferences(current: PackageSpec, source: PackageSpec, selected: Set<MergeScope>): string[] {
  const available = {
    objects: new Set([
      ...current.objects.map((item) => item.key),
      ...(selected.has('objects') ? source.objects.map((item) => item.key) : []),
    ]),
    rules: new Set([
      ...(current.rules ?? []).map((item) => item.key),
      ...(selected.has('rules') ? (source.rules ?? []).map((item) => item.key) : []),
    ]),
    metrics: new Set([
      ...(current.metrics ?? []).map((item) => item.key),
      ...(selected.has('metrics') ? (source.metrics ?? []).map((item) => item.key) : []),
    ]),
    operations: new Set([
      ...current.operations.map((item) => item.key),
      ...(selected.has('operations') ? source.operations.map((item) => item.key) : []),
    ]),
  };
  const broken: string[] = [];
  if (selected.has('operations')) {
    for (const operation of source.operations) {
      for (const key of operation.relatedObjects ?? []) {
        if (!available.objects.has(key)) broken.push(`${operation.key} 缺少对象 ${key}`);
      }
      for (const key of operation.usesRules ?? []) {
        if (!available.rules.has(key)) broken.push(`${operation.key} 缺少规则 ${key}`);
      }
      for (const key of operation.usesMetrics ?? []) {
        if (!available.metrics.has(key)) broken.push(`${operation.key} 缺少指标 ${key}`);
      }
    }
  }
  if (selected.has('scripts')) {
    for (const script of source.scripts ?? []) {
      if (!available.operations.has(script.boundOperation)) {
        broken.push(`${script.key} 缺少操作 ${script.boundOperation}`);
      }
    }
  }
  return [...new Set(broken)];
}

function findResourcePath(files: Record<string, string>, kind: string, key: string): string | undefined {
  const prefix = `${kind}/`;
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(`^key:\\s*["']?${escaped}["']?\\s*$`, 'mu');
  return Object.entries(files).find(([path, content]) => path.startsWith(prefix) && pattern.test(content))?.[0];
}

function resourceSummary(item: any): string {
  return String(item?.description || item?.content || item?.caliber || item?.title || item?.key || '').slice(0, 300);
}

function summarize(content: string): string {
  return content.replace(/^---[\s\S]*?---\s*/u, '').replace(/\s+/gu, ' ').trim().slice(0, 300);
}

function normalize(value: string): string {
  return value.normalize('NFKC').trim().toLocaleLowerCase('zh-CN');
}

function packageSourceFromRow(row: any): PackageMergeSource {
  return {
    id: row.id,
    projectId: row.project_id,
    filename: row.filename,
    packageId: row.package_id,
    name: row.package_name,
    version: row.package_version,
    spec: JSON.parse(row.spec_json),
    files: JSON.parse(row.files_json),
    uploadedBy: row.uploaded_by,
    createdAt: row.created_at,
  };
}
