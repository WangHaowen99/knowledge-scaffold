import type { DatabaseSync } from 'node:sqlite';

import { getDb } from '../db/index.js';
import type { Project } from '../types.js';
import { validateKey } from '../workflow/keys.js';
import { ModelingMcpToolError } from './tool-error.js';

export const OBJECT_REVIEW_MCP_TOOLS = new Set([
  'submit_ontology_object_review',
  'prepare_ontology_object_confirmation_groups',
  'preflight_ontology_object_confirmation',
  'confirm_ontology_object_review',
  'reject_ontology_object_review',
]);

interface WorkspaceRow {
  run_id: string;
  candidate_id: string | null;
}

interface ChangeRow {
  path: string;
  group_id: string;
}

export class ObjectReviewScopeService {
  constructor(private readonly db: DatabaseSync = getDb()) {}

  async execute(
    toolName: string,
    project: Project,
    args: Record<string, unknown>,
    invoke: (internalToolName: string, internalArgs: Record<string, unknown>) => Promise<Record<string, unknown>>,
    internalToolName: string,
  ): Promise<Record<string, unknown>> {
    const objectId = requiredObjectId(args.object_id);
    const objectPath = `objects/${objectId.slice('object:'.length)}.md`;
    const scope = this.assertExclusiveObjectScope(project.id, objectId, objectPath);
    this.assertRequestedGroupsScoped(args.change_groups, scope.allowedGroupIds, objectPath);
    const result = await invoke(internalToolName, omitObjectId(args));
    const resultGroupIds = Array.isArray(result.change_groups)
      ? result.change_groups.flatMap((item) => (
        item && typeof item === 'object' && !Array.isArray(item)
          ? [String((item as Record<string, unknown>).id ?? '').trim()].filter(Boolean)
          : []
      ))
      : [];
    return {
      ...result,
      object_id: objectId,
      object_scope: {
        changed_paths: scope.changedPaths,
        change_group_ids: resultGroupIds.length ? resultGroupIds : scope.allowedGroupIds,
      },
      display: result.display ?? { text: `${toolName} 已按对象范围执行` },
    };
  }

  private assertExclusiveObjectScope(projectId: string, objectId: string, objectPath: string): {
    changedPaths: string[];
    allowedGroupIds: string[];
  } {
    const workspace = this.db.prepare(`SELECT run_id, candidate_id
      FROM agent_candidate_workspaces WHERE project_id = ?`).get(projectId) as WorkspaceRow | undefined;
    if (!workspace) throw noObjectCandidate(objectId);

    const scoped = workspace.candidate_id
      ? candidateScope(this.db, projectId, workspace.candidate_id, objectPath)
      : workspaceScope(this.db, projectId, workspace.run_id, objectPath);
    if (!scoped.touchesTarget) throw noObjectCandidate(objectId);
    if (scoped.conflictingBusinessPaths.length) {
      throw new ModelingMcpToolError(
        'OBJECT_REVIEW_SCOPE_CONFLICT',
        '当前项目候选还包含其他对象或其他业务资源，不能作为单对象复核确认',
        409,
        ['先分别完成或放弃其他资源修改，再仅保留当前对象修改后重试'],
        {
          object_id: objectId,
          conflicting_paths: scoped.conflictingBusinessPaths,
          changed_paths: scoped.changedPaths,
        },
      );
    }
    return {
      changedPaths: scoped.changedPaths,
      allowedGroupIds: scoped.allowedGroupIds,
    };
  }

  private assertRequestedGroupsScoped(value: unknown, allowedGroupIds: string[], objectPath: string): void {
    if (value === undefined) return;
    if (!Array.isArray(value)) {
      throw new ModelingMcpToolError('INVALID_INPUT', 'change_groups 必须是数组', 400);
    }
    const allowed = new Set(allowedGroupIds);
    const requested = value.flatMap((item) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
      const group = item as Record<string, unknown>;
      const id = String(group.id ?? '').trim();
      const impact = Array.isArray(group.impact) ? group.impact.map(String) : [];
      return id ? [{ id, impact }] : [];
    });
    const outside = requested.filter((group) => (
      !allowed.has(group.id) && !groupImpactWithinObjectScope(group.impact, objectPath)
    )).map((group) => group.id);
    if (outside.length) {
      throw new ModelingMcpToolError(
        'OBJECT_REVIEW_SCOPE_CONFLICT',
        '确认分组包含当前对象范围之外的变更',
        409,
        ['使用对象级 prepare 工具返回的 change_groups 原样重试'],
        { outside_group_ids: outside, allowed_group_ids: allowedGroupIds },
      );
    }
  }
}

function workspaceScope(
  db: DatabaseSync,
  projectId: string,
  runId: string,
  objectPath: string,
) {
  const rows = db.prepare(`SELECT path, group_id FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ? ORDER BY path`).all(projectId, runId) as unknown as ChangeRow[];
  const changedPaths = [...new Set(rows.map((row) => row.path))].sort();
  const targetGroups = [...new Set(rows.map((row) => row.group_id))].sort();
  return {
    changedPaths,
    allowedGroupIds: targetGroups,
    touchesTarget: changedPaths.includes(objectPath),
    conflictingBusinessPaths: conflictingBusinessPaths(changedPaths, objectPath),
  };
}

function candidateScope(db: DatabaseSync, projectId: string, candidateId: string, objectPath: string) {
  const row = db.prepare(`SELECT groups_json FROM artifact_candidate_revisions
    WHERE project_id = ? AND id = ?`).get(projectId, candidateId) as { groups_json: string } | undefined;
  if (!row) return { changedPaths: [], allowedGroupIds: [], touchesTarget: false, conflictingBusinessPaths: [] };
  const groups = parseGroups(row.groups_json);
  const pathsByGroup = groups.map((group) => ({ id: group.id, paths: group.paths }));
  const changedPaths = [...new Set(pathsByGroup.flatMap((group) => group.paths))].sort();
  const targetGroups = pathsByGroup.map((group) => group.id).filter(Boolean).sort();
  return {
    changedPaths,
    allowedGroupIds: targetGroups,
    touchesTarget: changedPaths.includes(objectPath),
    conflictingBusinessPaths: conflictingBusinessPaths(changedPaths, objectPath),
  };
}

function parseGroups(raw: string): Array<{ id: string; paths: string[] }> {
  try {
    const groups = JSON.parse(raw) as unknown;
    if (!Array.isArray(groups)) return [];
    return groups.flatMap((value) => {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
      const group = value as Record<string, unknown>;
      const changes = Array.isArray(group.changes) ? group.changes : [];
      return [{
        id: String(group.id ?? '').trim(),
        paths: changes.flatMap((change) => {
          if (!change || typeof change !== 'object' || Array.isArray(change)) return [];
          const path = String((change as Record<string, unknown>).path ?? '').replace(/^package\//u, '');
          return path ? [path] : [];
        }),
      }];
    });
  } catch {
    return [];
  }
}

function conflictingBusinessPaths(paths: string[], objectPath: string): string[] {
  return paths.filter((path) => isBusinessDetailPath(path) && path !== objectPath).sort();
}

function groupImpactWithinObjectScope(paths: string[], objectPath: string): boolean {
  if (!paths.length) return false;
  return paths.every((path) => !isBusinessDetailPath(path) || path === objectPath);
}

function isBusinessDetailPath(path: string): boolean {
  return /^(?:objects|operations|rules|metrics|scripts)\/(?!index\.md$).+/u.test(path)
    && !path.startsWith('.modeling/');
}

function requiredObjectId(value: unknown): string {
  const objectId = String(value ?? '').trim();
  if (objectId.startsWith('object:')) {
    try {
      return `object:${validateKey(objectId.slice('object:'.length), '对象')}`;
    } catch {
      // Normalize all invalid IDs to the stable public error below.
    }
  }
  throw new ModelingMcpToolError('INVALID_INPUT', 'object_id 必须使用 object:<key> 格式', 400);
}

function noObjectCandidate(objectId: string): ModelingMcpToolError {
  return new ModelingMcpToolError(
    'OBJECT_REVIEW_NOT_FOUND',
    `对象 ${objectId} 当前没有独立的待处理修改`,
    409,
    ['先修改该对象，或查询 get_ontology_object_review_status 核对状态'],
  );
}

function omitObjectId(args: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(args).filter(([key]) => key !== 'object_id'));
}
