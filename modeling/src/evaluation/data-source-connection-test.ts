import neo4j from 'neo4j-driver';
import type {
  ConnectionDataSourceInput,
  EvaluationDatabaseConfig,
} from './data-source.js';
import {
  testEvaluationDatabaseConnection,
  type EvaluationDatabaseConnector,
} from './eval-db.js';

interface Neo4jTestSession {
  run(query: string): Promise<unknown>;
  close(): Promise<void>;
}

interface Neo4jTestDriver {
  session(options: { database: string }): Neo4jTestSession;
  close(): Promise<void>;
}

export type Neo4jDriverFactory = (
  uri: string,
  username: string,
  password: string,
) => Neo4jTestDriver;

export type DataSourceConnectionTester = (
  source: ConnectionDataSourceInput,
) => Promise<void>;

export interface DataSourceTestDependencies {
  neo4jDriverFactory?: Neo4jDriverFactory;
  request?: typeof fetch;
  evaluationDatabaseConnector?: EvaluationDatabaseConnector;
}

function text(value: unknown): string {
  return String(value ?? '').trim();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function redact(message: string, secrets: string[]): string {
  return secrets.filter(Boolean).reduce(
    (safe, secret) => safe.split(secret).join('[REDACTED]'),
    message,
  );
}

const defaultNeo4jDriverFactory: Neo4jDriverFactory = (uri, username, password) => (
  neo4j.driver(uri, neo4j.auth.basic(username, password)) as unknown as Neo4jTestDriver
);

async function testNeo4j(
  source: ConnectionDataSourceInput,
  driverFactory: Neo4jDriverFactory,
): Promise<void> {
  const uri = text(source.config.uri);
  const username = text(source.config.username);
  const password = text(source.config.password);
  const database = text(source.config.database) || 'neo4j';
  if (!password) throw new Error('Neo4j Password 每次保存并测试时都必须填写');

  let driver: Neo4jTestDriver | undefined;
  let session: Neo4jTestSession | undefined;
  try {
    driver = driverFactory(uri, username, password);
    session = driver.session({ database });
    await session.run('RETURN 1 AS ok');
  } catch (error) {
    throw new Error(`Neo4j 连接测试失败：${redact(errorMessage(error), [password])}`);
  } finally {
    await session?.close().catch(() => undefined);
    await driver?.close().catch(() => undefined);
  }
}

async function testNl2Sql(
  source: ConnectionDataSourceInput,
  request: typeof fetch,
): Promise<void> {
  const url = text(source.config.url).replace(/\/+$/, '');
  const token = text(source.config.token);
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;

  try {
    const response = await request(`${url}/api/v1/sql`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        sql: 'SELECT 1 AS ok',
        scene: text(source.config.scene),
      }),
      signal: AbortSignal.timeout(10_000),
    });
    const detail = await response.text().catch(() => '');
    if (!response.ok) {
      throw new Error(`${response.status}${detail ? `：${detail}` : ''}`);
    }
    if (detail) {
      const payload = JSON.parse(detail) as { success?: boolean; error?: unknown; message?: unknown };
      if (payload.success === false) {
        throw new Error(String(payload.error || payload.message || '服务返回失败'));
      }
    }
  } catch (error) {
    throw new Error(`NL2SQL 连接测试失败：${redact(errorMessage(error), [token])}`);
  }
}

export async function testProjectDataSourceConnection(
  source: ConnectionDataSourceInput,
  dependencies: DataSourceTestDependencies = {},
): Promise<void> {
  if (source.provider === 'neo4j') {
    await testNeo4j(source, dependencies.neo4jDriverFactory ?? defaultNeo4jDriverFactory);
    return;
  }
  await testEvaluationDatabaseConnection(
    source.config.evaluation_database as EvaluationDatabaseConfig,
    { connect: dependencies.evaluationDatabaseConnector },
  );
  await testNl2Sql(source, dependencies.request ?? fetch);
}
