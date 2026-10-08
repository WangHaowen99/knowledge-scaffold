import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import { FIXED_TRUNK } from '../types.js';
import {
  AiConstructionTaskGraph,
  aiConstructionGraphSourceForProjectKind,
} from '../ai-construction/task-graph.js';
import type {
  ConfigurableWorkflowStep,
  NodeType,
  Project,
  ProjectKind,
  ProjectSensitivity,
  ProjectWorkflowProfile,
} from '../types.js';

const CONFIGURABLE_STEPS = new Set<ConfigurableWorkflowStep>([
  'scenario', 'data_source', 'evaluate', 'diagnose', 'optimize',
]);

export function canSyncProjectMembers(
  project: Pick<Project, 'owner'> | null,
  username: string,
  role?: string,
): boolean {
  return Boolean(project) && (project!.owner === username || role === 'admin');
}

export const DEFAULT_WORKFLOW_PROFILE: ProjectWorkflowProfile = {
  enabledSteps: [],
  requiredSteps: ['scenario', 'data_source'],
  releaseRequested: true,
  externalResearch: false,
};

function workflowSteps(value: unknown): ConfigurableWorkflowStep[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((step): step is ConfigurableWorkflowStep => (
    typeof step === 'string' && CONFIGURABLE_STEPS.has(step as ConfigurableWorkflowStep)
  )))];
}

export function normalizeWorkflowProfile(
  value: Partial<ProjectWorkflowProfile> | null | undefined,
  sensitivityLevel: ProjectSensitivity,
): ProjectWorkflowProfile {
  return {
    enabledSteps: workflowSteps(value?.enabledSteps),
    requiredSteps: value?.requiredSteps === undefined
      ? [...DEFAULT_WORKFLOW_PROFILE.requiredSteps]
      : workflowSteps(value.requiredSteps),
    releaseRequested: value?.releaseRequested ?? DEFAULT_WORKFLOW_PROFILE.releaseRequested,
    externalResearch: sensitivityLevel === 'restricted'
      ? false
      : value?.externalResearch ?? DEFAULT_WORKFLOW_PROFILE.externalResearch,
  };
}

function parseWorkflowProfile(raw: unknown, sensitivityLevel: ProjectSensitivity): ProjectWorkflowProfile {
  if (typeof raw !== 'string' || !raw) return normalizeWorkflowProfile(undefined, sensitivityLevel);
  try {
    return normalizeWorkflowProfile(JSON.parse(raw) as Partial<ProjectWorkflowProfile>, sensitivityLevel);
  } catch {
    return normalizeWorkflowProfile(undefined, sensitivityLevel);
  }
}

function rowToProject(r: any): Project {
  const sensitivityLevel: ProjectSensitivity = r.sensitivity_level === 'restricted'
    ? 'restricted'
    : 'standard';
  return {
    id: r.id, owner: r.owner, externalId: r.external_id ?? undefined,
    kind: r.kind === 'package' ? 'package' : 'modeling',
    runtime: r.runtime === 'agent' ? 'agent' : 'legacy',
    name: r.name, domain: r.domain, background: r.background,
    targetUsers: r.target_users ?? undefined, glossaryPrefs: r.glossary_prefs ?? undefined,
    sensitivityLevel,
    workflowProfile: parseWorkflowProfile(r.workflow_profile_json, sensitivityLevel),
    stage: r.stage, createdAt: r.created_at, updatedAt: r.updated_at,
  };
}

export function createProject(input: {
  owner: string; name: string; externalId?: string; domain?: string; background?: string;
  kind?: ProjectKind;
  runtime?: Project['runtime'];
  targetUsers?: string; glossaryPrefs?: string;
  sensitivityLevel?: ProjectSensitivity;
  workflowProfile?: Partial<ProjectWorkflowProfile>;
}): Project {
  const db = getDb();
  const id = `pj_${randomUUID().slice(0, 12)}`;
  const now = nowIso();
  const kind: ProjectKind = input.kind === 'package' ? 'package' : 'modeling';
  const runtime: Project['runtime'] = input.runtime === 'agent' ? 'agent' : 'legacy';
  const sensitivityLevel: ProjectSensitivity = input.sensitivityLevel === 'restricted'
    ? 'restricted'
    : 'standard';
  const workflowProfile = normalizeWorkflowProfile(input.workflowProfile, sensitivityLevel);
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare(`INSERT INTO projects
      (id, owner, external_id, kind, runtime, name, domain, background, target_users, glossary_prefs,
        sensitivity_level, workflow_profile_json,
        stage, created_at, updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`)
      .run(id, input.owner, input.externalId ?? null, kind, runtime, input.name, input.domain ?? '', input.background ?? '',
        input.targetUsers ?? null, input.glossaryPrefs ?? null,
        sensitivityLevel, JSON.stringify(workflowProfile),
        kind === 'package' ? 'review' : 'project_setup', now, now);
    if (runtime === 'legacy') {
      new AiConstructionTaskGraph(db).initialize(id, aiConstructionGraphSourceForProjectKind(kind));
    }
    db.exec('COMMIT');
  } catch (error) {
    if (db.isTransaction) db.exec('ROLLBACK');
    throw error;
  }
  return getProject(id)!;
}

export function claimAgentRuntime(id: string): Project | null {
  const project = getProject(id);
  if (!project || project.runtime === 'agent') return project;
  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare(`UPDATE projects SET runtime = 'agent', updated_at = ? WHERE id = ?`).run(nowIso(), id);
    db.prepare('DELETE FROM ai_construction_messages WHERE project_id = ?').run(id);
    db.prepare('DELETE FROM ai_construction_graphs WHERE project_id = ?').run(id);
    db.prepare('DELETE FROM workflow_step_progress WHERE project_id = ?').run(id);
    db.prepare('DELETE FROM wf_nodes WHERE project_id = ?').run(id);
    db.exec('COMMIT');
  } catch (error) {
    if (db.isTransaction) db.exec('ROLLBACK');
    throw error;
  }
  return getProject(id);
}

export interface UpdateProjectInput {
  name?: string;
  domain?: string;
  background?: string;
  targetUsers?: string;
  glossaryPrefs?: string;
  sensitivityLevel?: ProjectSensitivity;
  workflowProfile?: Partial<ProjectWorkflowProfile>;
}

function editableText(value: string | undefined, current: string): string {
  return value === undefined ? current : String(value ?? '').trim();
}

export function updateProject(id: string, input: UpdateProjectInput): Project | null {
  const current = getProject(id);
  if (!current) return null;
  const name = editableText(input.name, current.name);
  if (!name) throw new Error('项目名必填');
  const sensitivityLevel: ProjectSensitivity = input.sensitivityLevel === undefined
    ? current.sensitivityLevel
    : input.sensitivityLevel === 'restricted' ? 'restricted' : 'standard';
  const workflowProfile = normalizeWorkflowProfile(
    input.workflowProfile === undefined
      ? current.workflowProfile
      : { ...current.workflowProfile, ...input.workflowProfile },
    sensitivityLevel,
  );
  const domain = editableText(input.domain, current.domain);
  const background = editableText(input.background, current.background);
  const targetUsers = editableText(input.targetUsers, current.targetUsers ?? '');
  const glossaryPrefs = editableText(input.glossaryPrefs, current.glossaryPrefs ?? '');
  getDb().prepare(`UPDATE projects SET
    name = ?, domain = ?, background = ?, target_users = ?, glossary_prefs = ?,
    sensitivity_level = ?, workflow_profile_json = ?, updated_at = ?
    WHERE id = ?`)
    .run(name, domain, background, targetUsers || null, glossaryPrefs || null,
      sensitivityLevel, JSON.stringify(workflowProfile), nowIso(), id);
  return getProject(id)!;
}

export function getProject(id: string): Project | null {
  const db = getDb();
  const r = db.prepare('SELECT * FROM projects WHERE id = ?').get(id);
  if (!r) return null;
  return rowToProject(r as any);
}

export function getProjectByExternalId(externalId: string): Project | null {
  const row = getDb().prepare('SELECT * FROM projects WHERE external_id = ?').get(externalId);
  return row ? rowToProject(row as any) : null;
}

export function listProjects(owner: string): Project[] {
  const db = getDb();
  const rows = db.prepare(`SELECT p.* FROM projects p
    LEFT JOIN project_members m ON m.project_id = p.id AND m.username = ?
    WHERE p.owner = ? OR m.username IS NOT NULL
    ORDER BY p.updated_at DESC`).all(owner, owner);
  return rows.map(rowToProject);
}

export type ProjectMemberRole = 'owner' | 'modeler' | 'viewer';

export interface ProjectMember {
  username: string;
  role: ProjectMemberRole;
  addedBy: string;
  createdAt: string;
  updatedAt: string;
}

export function projectMemberRole(projectId: string, username: string): ProjectMemberRole | null {
  const project = getProject(projectId);
  if (!project) return null;
  if (project.owner === username) return 'owner';
  const row = getDb().prepare(`SELECT role FROM project_members
    WHERE project_id = ? AND username = ?`).get(projectId, username) as { role?: string } | undefined;
  return row?.role === 'modeler' ? 'modeler' : row?.role === 'viewer' ? 'viewer' : null;
}

export function canReadProject(projectId: string, username: string): boolean {
  return projectMemberRole(projectId, username) !== null;
}

export function canWriteProject(projectId: string, username: string): boolean {
  return ['owner', 'modeler'].includes(projectMemberRole(projectId, username) ?? '');
}

export function listProjectMembers(projectId: string): ProjectMember[] {
  const project = getProject(projectId);
  if (!project) return [];
  const rows = getDb().prepare(`SELECT username, role, added_by, created_at, updated_at
    FROM project_members WHERE project_id = ? ORDER BY created_at, username`).all(projectId) as Array<any>;
  return [{
    username: project.owner,
    role: 'owner',
    addedBy: project.owner,
    createdAt: project.createdAt,
    updatedAt: project.updatedAt,
  }, ...rows.map((row) => ({
    username: row.username,
    role: row.role,
    addedBy: row.added_by,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  }))];
}

export function setProjectMember(
  projectId: string,
  owner: string,
  username: string,
  role: Exclude<ProjectMemberRole, 'owner'>,
): ProjectMember[] {
  const project = getProject(projectId);
  if (!project || project.owner !== owner) throw new Error('只有项目负责人可以管理成员');
  const normalized = String(username ?? '').trim();
  if (!normalized || normalized.length > 128) throw new Error('请输入有效的成员用户名');
  if (normalized === project.owner) throw new Error('项目负责人不需要重复添加');
  if (!['viewer', 'modeler'].includes(role)) throw new Error('成员角色无效');
  const now = nowIso();
  getDb().prepare(`INSERT INTO project_members
    (project_id, username, role, added_by, created_at, updated_at)
    VALUES (?,?,?,?,?,?)
    ON CONFLICT(project_id, username) DO UPDATE SET
      role = excluded.role, added_by = excluded.added_by, updated_at = excluded.updated_at`)
    .run(projectId, normalized, role, owner, now, now);
  return listProjectMembers(projectId);
}

export function replaceProjectMembers(
  projectId: string,
  owner: string,
  members: Array<{ username: string; role: Exclude<ProjectMemberRole, 'owner'> }>,
): ProjectMember[] {
  const project = getProject(projectId);
  if (!project || project.owner !== owner) throw new Error('只有项目负责人可以管理成员');
  const normalized = members.map((member) => {
    const username = String(member.username ?? '').trim();
    if (!username || username.length > 128) throw new Error('请输入有效的成员用户名');
    if (username === project.owner) throw new Error('项目负责人不需要重复添加');
    if (!['viewer', 'modeler'].includes(member.role)) throw new Error('成员角色无效');
    return { username, role: member.role };
  });
  const db = getDb();
  const now = nowIso();
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare('DELETE FROM project_members WHERE project_id = ?').run(projectId);
    const insert = db.prepare(`INSERT INTO project_members
      (project_id, username, role, added_by, created_at, updated_at)
      VALUES (?,?,?,?,?,?)`);
    for (const member of normalized) {
      insert.run(projectId, member.username, member.role, owner, now, now);
    }
    db.exec('COMMIT');
  } catch (error) {
    if (db.isTransaction) db.exec('ROLLBACK');
    throw error;
  }
  return listProjectMembers(projectId);
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
