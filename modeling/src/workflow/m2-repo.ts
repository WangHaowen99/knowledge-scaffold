import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { PackageSpec, CoverageResult } from '../types.js';

const uid = (p: string) => `${p}_${randomUUID().slice(0, 12)}`;

// ---- 包版本 ----
export function insertPackageVersion(projectId: string, version: string, spec: PackageSpec, coverage: CoverageResult, note?: string): string {
  const db = getDb();
  const id = uid('pv');
  db.prepare(`INSERT INTO package_versions (id, project_id, version, spec_json, coverage_json, note, created_at)
    VALUES (?,?,?,?,?,?,?)`).run(id, projectId, version, JSON.stringify(spec), JSON.stringify(coverage), note ?? null, nowIso());
  return id;
}
export function latestPackageVersion(projectId: string): { id: string; version: string; spec: PackageSpec; coverage: CoverageResult } | null {
  // Git 修订时间只有秒级精度，同秒内的连续发布不能靠 created_at 排序。
  // package_versions 是只追加索引，rowid 才能稳定表示版本生成顺序。
  const r = getDb().prepare('SELECT * FROM package_versions WHERE project_id = ? ORDER BY rowid DESC LIMIT 1').get(projectId) as any;
  if (!r) return null;
  return { id: r.id, version: r.version, spec: JSON.parse(r.spec_json), coverage: r.coverage_json ? JSON.parse(r.coverage_json) : { covered: [], uncovered: [] } };
}
export function listPackageVersions(projectId: string): {
  id: string;
  version: string;
  revision?: string;
  note?: string;
  createdAt: string;
}[] {
  return (getDb().prepare(`SELECT id, version, revision_oid, note, created_at
    FROM package_versions WHERE project_id = ? ORDER BY rowid`).all(projectId) as any[])
    .map((r) => ({
      id: r.id,
      version: r.version,
      revision: r.revision_oid ?? undefined,
      note: r.note ?? undefined,
      createdAt: r.created_at,
    }));
}
export function getPackageVersion(projectId: string, version: string): { id: string; version: string; spec: PackageSpec; coverage: CoverageResult } | null {
  const r = getDb().prepare('SELECT * FROM package_versions WHERE project_id = ? AND version = ? ORDER BY rowid DESC LIMIT 1').get(projectId, version) as any;
  if (!r) return null;
  return { id: r.id, version: r.version, spec: JSON.parse(r.spec_json), coverage: r.coverage_json ? JSON.parse(r.coverage_json) : { covered: [], uncovered: [] } };
}

export function getPackageVersionById(projectId: string, id: string): { id: string; version: string; spec: PackageSpec; coverage: CoverageResult } | null {
  const r = getDb().prepare('SELECT * FROM package_versions WHERE project_id = ? AND id = ?').get(projectId, id) as any;
  if (!r) return null;
  return { id: r.id, version: r.version, spec: JSON.parse(r.spec_json), coverage: r.coverage_json ? JSON.parse(r.coverage_json) : { covered: [], uncovered: [] } };
}
