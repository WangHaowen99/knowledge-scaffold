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
    id: r.id, owner: r.owner, kind: r.kind === 'package' ? 'package' : 'modeling',
    name: r.name, domain: r.domain, background: r.background,
    targetUsers: r.target_users ?? undefined, glossaryPrefs: r.glossary_prefs ?? undefined,
    sensitivityLevel,
    workflowProfile: parseWorkflowProfile(r.workflow_profile_json, sensitivityLevel),
    stage: r.stage, createdAt: r.created_at, updatedAt: r.updated_at,
  };
}

export function createProject(input: {
  owner: string; name: string; domain?: string; background?: string;
  kind?: ProjectKind;
  targetUsers?: string; glossaryPrefs?: string;
  sensitivityLevel?: ProjectSensitivity;
  workflowProfile?: Partial<ProjectWorkflowProfile>;
}): Project {
  const db = getDb();
  const id = `pj_${randomUUID().slice(0, 12)}`;
  const now = nowIso();
  const kind: ProjectKind = input.kind === 'package' ? 'package' : 'modeling';
  const sensitivityLevel: ProjectSensitivity = input.sensitivityLevel === 'restricted'
    ? 'restricted'
    : 'standard';
  const workflowProfile = normalizeWorkflowProfile(input.workflowProfile, sensitivityLevel);
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare(`INSERT INTO projects
      (id, owner, kind, name, domain, background, target_users, glossary_prefs,
        sensitivity_level, workflow_profile_json,
        stage, created_at, updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`)
      .run(id, input.owner, kind, input.name, input.domain ?? '', input.background ?? '',
        input.targetUsers ?? null, input.glossaryPrefs ?? null,
        sensitivityLevel, JSON.stringify(workflowProfile),
        kind === 'package' ? 'review' : 'project_setup', now, now);
    new AiConstructionTaskGraph(db).initialize(id, aiConstructionGraphSourceForProjectKind(kind));
    db.exec('COMMIT');
  } catch (error) {
    if (db.isTransaction) db.exec('ROLLBACK');
    throw error;
  }
  return getProject(id)!;
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

export function listProjects(owner: string): Project[] {
  const db = getDb();
  const rows = db.prepare('SELECT * FROM projects WHERE owner = ? ORDER BY updated_at DESC').all(owner);
  return rows.map(rowToProject);
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
