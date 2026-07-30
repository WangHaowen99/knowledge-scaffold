import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import { FIXED_TRUNK } from '../types.js';
import { AiConstructionTaskGraph } from '../ai-construction/task-graph.js';
import type { Project, ProjectKind, NodeType } from '../types.js';

function rowToProject(r: any): Project {
  return {
    id: r.id, owner: r.owner, kind: r.kind === 'package' ? 'package' : 'modeling',
    name: r.name, domain: r.domain, background: r.background,
    targetUsers: r.target_users ?? undefined, glossaryPrefs: r.glossary_prefs ?? undefined,
    stage: r.stage, createdAt: r.created_at, updatedAt: r.updated_at,
  };
}

export function createProject(input: {
  owner: string; name: string; domain?: string; background?: string;
  kind?: ProjectKind;
  targetUsers?: string; glossaryPrefs?: string;
}): Project {
  const db = getDb();
  const id = `pj_${randomUUID().slice(0, 12)}`;
  const now = nowIso();
  const kind: ProjectKind = input.kind === 'package' ? 'package' : 'modeling';
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare(`INSERT INTO projects
      (id, owner, kind, name, domain, background, target_users, glossary_prefs,
        stage, created_at, updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?)`)
      .run(id, input.owner, kind, input.name, input.domain ?? '', input.background ?? '',
        input.targetUsers ?? null, input.glossaryPrefs ?? null,
        kind === 'package' ? 'review' : 'project_setup', now, now);
    new AiConstructionTaskGraph(db).initialize(id, kind === 'package' ? 'package_import' : 'new');
    db.exec('COMMIT');
  } catch (error) {
    if (db.isTransaction) db.exec('ROLLBACK');
    throw error;
  }
  return getProject(id)!;
}

export function getProject(id: string): Project | null {
  const db = getDb();
  const r = db.prepare('SELECT * FROM projects WHERE id = ?').get(id);
  if (!r) return null;
  const project = rowToProject(r as any);
  new AiConstructionTaskGraph(db).initialize(
    project.id,
    project.kind === 'package' ? 'package_import' : 'legacy_projection',
  );
  return project;
}

export function listProjects(owner: string): Project[] {
  const db = getDb();
  const rows = db.prepare('SELECT * FROM projects WHERE owner = ? ORDER BY updated_at DESC').all(owner);
  const projects = rows.map(rowToProject);
  const graph = new AiConstructionTaskGraph(db);
  for (const project of projects) {
    graph.initialize(project.id, project.kind === 'package' ? 'package_import' : 'legacy_projection');
  }
  return projects;
}

export function deleteProject(id: string, owner: string): boolean {
  const result = getDb().prepare('DELETE FROM projects WHERE id = ? AND owner = ?').run(id, owner);
  return Number(result.changes ?? 0) === 1;
}

export function setStage(id: string, stage: NodeType): void {
  getDb().prepare('UPDATE projects SET stage = ?, updated_at = ? WHERE id = ?').run(stage, nowIso(), id);
}

// stage 语义 = 「最远到达的主干节点」：只前进不回退（重访早期节点不应降级进度标记）。
export function advanceStage(id: string, stage: NodeType): void {
  const p = getProject(id);
  if (!p) return;
  if (FIXED_TRUNK.indexOf(stage) <= FIXED_TRUNK.indexOf(p.stage as NodeType)) return;
  setStage(id, stage);
}
