import { getDb, nowIso } from '../db/index.js';

export function markAcceptanceProject(projectId: string, owner: string, scenarioId: string): void {
  getDb().prepare(`INSERT INTO acceptance_projects (project_id, owner, scenario_id, created_at)
    VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
      owner = excluded.owner, scenario_id = excluded.scenario_id`)
    .run(projectId, owner, scenarioId, nowIso());
}

export function acceptanceProjectScenario(projectId: string, owner: string): string {
  const row = getDb().prepare(`SELECT scenario_id FROM acceptance_projects
    WHERE project_id = ? AND owner = ?`).get(projectId, owner) as { scenario_id: string } | undefined;
  return row?.scenario_id ?? '';
}
