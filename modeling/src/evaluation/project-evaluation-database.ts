import {
  redactEvaluationDatabaseError,
  resolveEvaluationDatabaseTarget,
  type EvaluationDatabaseTarget,
} from './evaluation-database-target.js';

const PROJECT_DATABASE_NAME_MAX_LENGTH = 60;

export interface ProjectDatabaseAdminClient {
  query(sql: string, values?: unknown[]): Promise<unknown>;
  close(): Promise<void>;
}

export type ProjectDatabaseAdminConnector = (
  target: EvaluationDatabaseTarget,
) => Promise<ProjectDatabaseAdminClient>;

export interface ProjectDatabaseCreationDependencies {
  databaseUrl?: string;
  connectAdmin?: ProjectDatabaseAdminConnector;
}

export interface ProjectDatabaseCreationResult {
  database: string;
  created: boolean;
}

export function projectEvaluationDatabaseName(projectId: string): string {
  const normalizedProjectId = String(projectId ?? '').trim()
    .replace(/[^a-zA-Z0-9_]/g, '_');
  if (!normalizedProjectId) throw new Error('项目 ID 不能为空');
  return `mv_shadow_${normalizedProjectId}`.slice(0, PROJECT_DATABASE_NAME_MAX_LENGTH);
}

async function defaultAdminConnector(
  target: EvaluationDatabaseTarget,
): Promise<ProjectDatabaseAdminClient> {
  const mysql = await import('mysql2/promise');
  const connection = await mysql.createConnection({
    uri: target.connectionUrl,
    connectTimeout: 10_000,
  });
  return {
    query: async (sql, values) => {
      const [rows] = await connection.query(sql, values);
      return rows;
    },
    close: async () => { await connection.end(); },
  };
}

function hasRows(result: unknown): boolean {
  return Array.isArray(result) && result.length > 0;
}

function quoteIdentifier(value: string): string {
  return `\`${value.replace(/`/g, '``')}\``;
}

export async function createProjectEvaluationDatabase(
  input: { projectId: string; databaseUrl?: string },
  dependencies: ProjectDatabaseCreationDependencies = {},
): Promise<ProjectDatabaseCreationResult> {
  const database = projectEvaluationDatabaseName(input.projectId);
  const target = resolveEvaluationDatabaseTarget(
    database,
    input.databaseUrl || dependencies.databaseUrl,
  );
  let client: ProjectDatabaseAdminClient | undefined;
  try {
    client = await (dependencies.connectAdmin ?? defaultAdminConnector)(target);
    const exists = hasRows(await client.query(
      'SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = ?',
      [database],
    ));
    if (exists) return { database, created: false };

    await client.query(`CREATE DATABASE IF NOT EXISTS ${quoteIdentifier(database)} CHARACTER SET utf8mb4`);
    return { database, created: true };
  } catch (error) {
    throw new Error(`使用项目专用数据库失败：${redactEvaluationDatabaseError(error, target)}`);
  } finally {
    await client?.close().catch(() => undefined);
  }
}
