import neo4j from 'neo4j-driver';
import { assertGraphIdentifier } from '../evaluation/data-source.js';
import type { GraphImportValidation } from './types.js';
import { quoteGraphLabel } from './graph-label.js';
import { neo4jEntityRows } from './neo4j-values.js';

const BATCH_SIZE = 500;
const USER_ID_PROPERTY = 'd2o_user_id';
const RECORD_KEY_PROPERTY = '_d2o_record_key';

export interface Neo4jGraphImportConnection {
  uri: string;
  username: string;
  password: string;
  database: string;
  workspaceProperty: string;
  workspaceValue: string;
  ontologyProperty: string;
  ontologyValue: string;
}

interface TransactionLike {
  run(query: string, params: Record<string, unknown>): Promise<any>;
}

interface SessionLike {
  executeWrite<T>(work: (transaction: TransactionLike) => Promise<T>): Promise<T>;
  close(): Promise<void> | void;
}

interface DriverLike {
  session(options: { database: string }): SessionLike;
  close(): Promise<void> | void;
}

export type Neo4jDriverFactory = (
  connection: Neo4jGraphImportConnection,
) => DriverLike | Promise<DriverLike>;

export interface Neo4jGraphImportWriteOptions {
  replaceExisting?: boolean;
  userName?: string;
}

export interface GraphIsolationCounts {
  nodeCount: number;
  relationshipCount: number;
}

export const GRAPH_ISOLATION_DATA_EXISTS_MESSAGE = '当前您所选的隔离字段值在图数据库中已存在数据，是否要清空旧数据，并写入新数据？';

export class GraphIsolationDataExistsError extends Error {
  readonly code = 'GRAPH_ISOLATION_DATA_EXISTS';
  constructor(readonly existing: { nodeCount: number; relationshipCount: number }) {
    super(GRAPH_ISOLATION_DATA_EXISTS_MESSAGE);
    this.name = 'GraphIsolationDataExistsError';
  }
}

function quoteIdentifier(value: string, label: string): string {
  assertGraphIdentifier(value, label);
  return `\`${value}\``;
}

function batches<T>(rows: T[]): T[][] {
  const result: T[][] = [];
  for (let index = 0; index < rows.length; index += BATCH_SIZE) {
    result.push(rows.slice(index, index + BATCH_SIZE));
  }
  return result;
}

function validateConnection(connection: Neo4jGraphImportConnection): void {
  if (!connection.uri.trim()) throw new Error('Neo4j URL 不能为空');
  if (!connection.username.trim()) throw new Error('Neo4j User 不能为空');
  if (!connection.password) throw new Error('本次导入的 Neo4j Password 不能为空');
  if (!connection.database.trim()) throw new Error('Neo4j Database 不能为空');
  if (!connection.workspaceValue.trim()) throw new Error('Workspace 隔离值不能为空');
  if (!connection.ontologyValue.trim()) throw new Error('Ontology 隔离值不能为空');
  assertGraphIdentifier(connection.workspaceProperty, 'Workspace 隔离字段');
  assertGraphIdentifier(connection.ontologyProperty, 'Ontology 隔离字段');
  if (connection.workspaceProperty === connection.ontologyProperty) {
    throw new Error('Workspace 与 Ontology 隔离字段不能相同');
  }
  const reservedCollision = [connection.workspaceProperty, connection.ontologyProperty]
    .find((property) => property === USER_ID_PROPERTY || property === RECORD_KEY_PROPERTY);
  if (reservedCollision) {
    throw new Error(`隔离字段「${reservedCollision}」不能使用系统保留字段`);
  }
}

function integerValue(value: any): number {
  if (typeof value === 'number') return value;
  if (value && typeof value.toNumber === 'function') return value.toNumber();
  return Number(value ?? 0) || 0;
}

const defaultDriverFactory: Neo4jDriverFactory = (connection) => neo4j.driver(
  connection.uri,
  neo4j.auth.basic(connection.username, connection.password),
);

async function readGraphIsolationCounts(
  transaction: TransactionLike,
  workspaceProperty: string,
  ontologyProperty: string,
  connection: Neo4jGraphImportConnection,
): Promise<GraphIsolationCounts> {
  const result = await transaction.run(`
    CALL {
      MATCH (node)
      WHERE node.${workspaceProperty} = $workspaceValue
        AND node.${ontologyProperty} = $ontologyValue
      RETURN count(node) AS nodeCount
    }
    CALL {
      MATCH ()-[relationship]->()
      WHERE relationship.${workspaceProperty} = $workspaceValue
        AND relationship.${ontologyProperty} = $ontologyValue
      RETURN count(relationship) AS relationshipCount
    }
    RETURN nodeCount, relationshipCount
  `, {
    workspaceValue: connection.workspaceValue,
    ontologyValue: connection.ontologyValue,
  });
  const record = result?.records?.[0];
  return {
    nodeCount: integerValue(record?.get?.('nodeCount')),
    relationshipCount: integerValue(record?.get?.('relationshipCount')),
  };
}

export async function inspectGraphIsolation(
  connection: Neo4jGraphImportConnection,
  driverFactory: Neo4jDriverFactory = defaultDriverFactory,
): Promise<GraphIsolationCounts> {
  validateConnection(connection);
  const workspaceProperty = quoteIdentifier(connection.workspaceProperty, 'Workspace 隔离字段');
  const ontologyProperty = quoteIdentifier(connection.ontologyProperty, 'Ontology 隔离字段');
  const driver = await driverFactory(connection);
  let session: SessionLike | undefined;
  try {
    session = driver.session({ database: connection.database });
    return await session.executeWrite((transaction) => readGraphIsolationCounts(
      transaction,
      workspaceProperty,
      ontologyProperty,
      connection,
    ));
  } finally {
    try {
      await session?.close();
    } finally {
      await driver.close();
    }
  }
}

export async function writeGraphImport(
  plan: GraphImportValidation,
  connection: Neo4jGraphImportConnection,
  options: Neo4jGraphImportWriteOptions = {},
  driverFactory: Neo4jDriverFactory = defaultDriverFactory,
): Promise<{ nodeCount: number; relationshipCount: number }> {
  validateConnection(connection);

  const workspaceProperty = quoteIdentifier(connection.workspaceProperty, 'Workspace 隔离字段');
  const ontologyProperty = quoteIdentifier(connection.ontologyProperty, 'Ontology 隔离字段');
  const userIdProperty = quoteIdentifier(USER_ID_PROPERTY, '用户标识字段');
  const recordKeyProperty = quoteIdentifier(RECORD_KEY_PROPERTY, '行实例标识字段');
  const userName = String(options.userName ?? '').trim();
  if (!userName) throw new Error('执行导入的用户名称不能为空');

  for (const entity of plan.entities) {
    quoteGraphLabel(entity.label, `${entity.objectKey} Label`);
    const reserved = new Set([
      connection.workspaceProperty,
      connection.ontologyProperty,
      USER_ID_PROPERTY,
      RECORD_KEY_PROPERTY,
    ]);
    const collision = entity.propertyNames.find((property) => reserved.has(property));
    if (collision) {
      throw new Error(`实体「${entity.objectKey}」属性「${collision}」不能覆盖系统属性`);
    }
  }
  for (const relation of plan.relations) {
    quoteIdentifier(relation.relationName, '关系名称');
    quoteGraphLabel(relation.sourceLabel, `${relation.sourceObject} Label`);
    quoteGraphLabel(relation.targetLabel, `${relation.targetObject} Label`);
  }

  const driver = await driverFactory(connection);
  let session: SessionLike | undefined;
  try {
    session = driver.session({ database: connection.database });
    await session.executeWrite(async (transaction) => {
      const isolationParams = {
        workspaceValue: connection.workspaceValue,
        ontologyValue: connection.ontologyValue,
      };
      const existing = await readGraphIsolationCounts(
        transaction,
        workspaceProperty,
        ontologyProperty,
        connection,
      );
      if ((existing.nodeCount || existing.relationshipCount) && !options.replaceExisting) {
        throw new GraphIsolationDataExistsError(existing);
      }
      if (existing.nodeCount || existing.relationshipCount) {
        await transaction.run(`
          MATCH ()-[relationship]->()
          WHERE relationship.${workspaceProperty} = $workspaceValue
            AND relationship.${ontologyProperty} = $ontologyValue
          DELETE relationship
        `, isolationParams);
        // Intentionally avoid DETACH DELETE: a cross-isolation relationship must make the
        // transaction fail instead of being removed as a side effect.
        await transaction.run(`
          MATCH (node)
          WHERE node.${workspaceProperty} = $workspaceValue
            AND node.${ontologyProperty} = $ontologyValue
          DELETE node
        `, isolationParams);
      }

      for (const entity of plan.entities) {
        const label = quoteGraphLabel(entity.label, `${entity.objectKey} Label`);
        const query = `
          UNWIND $rows AS row
          MERGE (node:${label} {
            ${workspaceProperty}: $workspaceValue,
            ${ontologyProperty}: $ontologyValue,
            ${recordKeyProperty}: row.recordKey
          })
          SET node += row.properties,
              node.${workspaceProperty} = $workspaceValue,
              node.${ontologyProperty} = $ontologyValue,
              node.${recordKeyProperty} = row.recordKey,
              node.${userIdProperty} = $userName
        `;
        for (const rows of batches(entity.rows)) {
          await transaction.run(query, {
            rows: neo4jEntityRows(entity, rows),
            workspaceValue: connection.workspaceValue,
            ontologyValue: connection.ontologyValue,
            userName,
          });
        }
      }

      for (const relation of plan.relations) {
        const relationName = quoteIdentifier(relation.relationName, '关系名称');
        const sourceLabel = quoteGraphLabel(relation.sourceLabel, `${relation.sourceObject} Label`);
        const targetLabel = quoteGraphLabel(relation.targetLabel, `${relation.targetObject} Label`);
        const query = `
          UNWIND $rows AS row
          MATCH (source:${sourceLabel} {
            ${workspaceProperty}: $workspaceValue,
            ${ontologyProperty}: $ontologyValue,
            ${recordKeyProperty}: row.sourceRecordKey
          })
          MATCH (target:${targetLabel} {
            ${workspaceProperty}: $workspaceValue,
            ${ontologyProperty}: $ontologyValue,
            ${recordKeyProperty}: row.targetRecordKey
          })
          MERGE (source)-[relationship:${relationName} {
            ${workspaceProperty}: $workspaceValue,
            ${ontologyProperty}: $ontologyValue,
            ${userIdProperty}: $userName
          }]->(target)
          SET relationship.${workspaceProperty} = $workspaceValue,
              relationship.${ontologyProperty} = $ontologyValue,
              relationship.${userIdProperty} = $userName
        `;
        for (const rows of batches(relation.pairs)) {
          await transaction.run(query, {
            rows,
            workspaceValue: connection.workspaceValue,
            ontologyValue: connection.ontologyValue,
            userName,
          });
        }
      }
    });
  } finally {
    try {
      await session?.close();
    } finally {
      await driver.close();
    }
  }

  return {
    nodeCount: plan.entities.reduce((total, entity) => total + entity.rows.length, 0),
    relationshipCount: plan.relations.reduce((total, relation) => total + relation.pairs.length, 0),
  };
}
