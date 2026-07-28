import {
  normalizeEvaluationDatabaseServerConfig,
  type EvaluationDatabaseEngine,
  type EvaluationDatabaseServerConfig,
} from './data-source.js';

const PROJECT_DATABASE_NAME_MAX_LENGTH = 60;

export interface ProjectDatabaseAdminClient {
  query(sql: string, values?: unknown[]): Promise<unknown>;
  close(): Promise<void>;
}

export type ProjectDatabaseAdminConnector = (
  config: EvaluationDatabaseServerConfig,
) => Promise<ProjectDatabaseAdminClient>;

export interface ProjectDatabaseCreationDependencies {
  connectAdmin?: ProjectDatabaseAdminConnector;
}

export interface ProjectDatabaseCreationResult {
  engine: EvaluationDatabaseEngine;
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
  config: EvaluationDatabaseServerConfig,
): Promise<ProjectDatabaseAdminClient> {
  if (config.engine === 'mysql') {
    const mysql = await import('mysql2/promise');
    const connection = await mysql.createConnection({
      host: config.host,
      port: config.port,
      user: config.username,
      password: config.password,
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

  const { Client } = await import('pg');
  const client = new Client({
    host: config.host,
    port: config.port,
    user: config.username,
    password: config.password,
    database: 'postgres',
    connectionTimeoutMillis: 10_000,
  });
  await client.connect();
  return {
    query: async (sql, values) => (await client.query(sql, values)).rows,
    close: async () => { await client.end(); },
  };
}

function hasRows(result: unknown): boolean {
  return Array.isArray(result) && result.length > 0;
}

function quoteIdentifier(engine: EvaluationDatabaseEngine, value: string): string {
  return engine === 'mysql'
    ? `\`${value.replace(/`/g, '``')}\``
    : `"${value.replace(/"/g, '""')}"`;
}

function safeError(error: unknown, password: string): string {
  const message = error instanceof Error ? error.message : String(error);
  return password ? message.split(password).join('[REDACTED]') : message;
}

export async function createProjectEvaluationDatabase(
  input: { projectId: string; config: unknown },
  dependencies: ProjectDatabaseCreationDependencies = {},
): Promise<ProjectDatabaseCreationResult> {
  const config = normalizeEvaluationDatabaseServerConfig(input.config);
  const database = projectEvaluationDatabaseName(input.projectId);
  let client: ProjectDatabaseAdminClient | undefined;
  try {
    client = await (dependencies.connectAdmin ?? defaultAdminConnector)(config);
    const exists = config.engine === 'mysql'
      ? hasRows(await client.query(
        'SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = ?',
        [database],
      ))
      : hasRows(await client.query('SELECT 1 FROM pg_database WHERE datname = $1', [database]));
    if (exists) return { engine: config.engine, database, created: false };

    const identifier = quoteIdentifier(config.engine, database);
    if (config.engine === 'mysql') {
      await client.query(`CREATE DATABASE IF NOT EXISTS ${identifier} CHARACTER SET utf8mb4`);
    } else {
      try {
        await client.query(`CREATE DATABASE ${identifier}`);
      } catch (error) {
        if ((error as { code?: string })?.code !== '42P04') throw error;
        return { engine: config.engine, database, created: false };
      }
    }
    return { engine: config.engine, database, created: true };
  } catch (error) {
    throw new Error(`新建项目数据库失败：${safeError(error, config.password)}`);
  } finally {
    await client?.close().catch(() => undefined);
  }
}
