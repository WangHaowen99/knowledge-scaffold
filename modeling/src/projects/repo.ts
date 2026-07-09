import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { Project, NodeType } from '../types.js';

function rowToProject(r: any): Project {
  return {
    id: r.id, owner: r.owner, name: r.name, domain: r.domain, background: r.background,
    targetUsers: r.target_users ?? undefined, glossaryPrefs: r.glossary_prefs ?? undefined,
    stage: r.stage, createdAt: r.created_at, updatedAt: r.updated_at,
  };
}

export function createProject(input: {
  owner: string; name: string; domain?: string; background?: string;
  targetUsers?: string; glossaryPrefs?: string;
}): Project {
  const db = getDb();
  const id = `pj_${randomUUID().slice(0, 12)}`;
  const now = nowIso();
  db.prepare(`INSERT INTO projects (id, owner, name, domain, background, target_users, glossary_prefs, stage, created_at, updated_at)
    VALUES (?,?,?,?,?,?,?,?,?,?)`)
    .run(id, input.owner, input.name, input.domain ?? '', input.background ?? '',
      input.targetUsers ?? null, input.glossaryPrefs ?? null, 'project_setup', now, now);
  return getProject(id)!;
}

export function getProject(id: string): Project | null {
  const r = getDb().prepare('SELECT * FROM projects WHERE id = ?').get(id);
  return r ? rowToProject(r as any) : null;
}

export function listProjects(owner: string): Project[] {
  const rows = getDb().prepare('SELECT * FROM projects WHERE owner = ? ORDER BY updated_at DESC').all(owner);
  return rows.map(rowToProject);
}

export function setStage(id: string, stage: NodeType): void {
  getDb().prepare('UPDATE projects SET stage = ?, updated_at = ? WHERE id = ?').run(stage, nowIso(), id);
}
