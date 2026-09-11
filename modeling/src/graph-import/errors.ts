import type {
  GraphImportObjectDiagnostic,
  GraphImportValidation,
} from './types.js';

export class GraphImportInputError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
    readonly statusCode: 400 | 404 | 409 | 422 = 400,
  ) {
    super(message);
    this.name = 'GraphImportInputError';
  }
}

export class GraphImportUpstreamError extends Error {
  readonly code = 'GRAPH_IMPORT_UPSTREAM_UNAVAILABLE';
  readonly statusCode = 502;

  constructor(
    message = '图数据服务暂时不可用，请稍后重试',
    readonly cause?: unknown,
  ) {
    super(message);
    this.name = 'GraphImportUpstreamError';
  }
}

export class GraphImportNoObjectMappingError extends GraphImportInputError {
  constructor(readonly missingObjects: Array<{ key: string; title: string }>) {
    const objectList = missingObjects.map((object) => object.key).join('、');
    super(
      'GRAPH_IMPORT_NO_OBJECT_MAPPING',
      `知识包未生成对象到数据表的运行时映射${objectList ? `：${objectList}` : ''}`,
      { missingObjects },
      422,
    );
    this.name = 'GraphImportNoObjectMappingError';
  }
}

export class GraphImportEmptyPlanError extends GraphImportInputError {
  constructor(readonly reasons: GraphImportObjectDiagnostic[] = []) {
    const reasonText = reasons.map((reason) => reason.message).join('；');
    super(
      'GRAPH_IMPORT_EMPTY_PLAN',
      `Excel 图数据导入计划没有可写入实例${reasonText ? `：${reasonText}` : ''}`,
      { reasons },
      422,
    );
    this.name = 'GraphImportEmptyPlanError';
  }
}

export class GraphImportWriteVerificationError extends Error {
  readonly code = 'GRAPH_IMPORT_WRITE_VERIFICATION_FAILED';

  constructor(
    readonly expected: { nodeCount: number; relationshipCount: number },
    readonly actual: { nodeCount: number; relationshipCount: number },
  ) {
    super(
      `Neo4j 写入结果与导入计划不一致：预计 ${expected.nodeCount} 个实例 / ${expected.relationshipCount} 条关系，`
      + `实际 ${actual.nodeCount} 个实例 / ${actual.relationshipCount} 条关系`,
    );
    this.name = 'GraphImportWriteVerificationError';
  }
}

export function assertGraphImportPlanNotEmpty(plan: GraphImportValidation): void {
  const actualNodeCount = plan.entities.reduce((total, entity) => total + entity.rows.length, 0);
  if (plan.nodeCount > 0 && actualNodeCount > 0) return;
  throw new GraphImportEmptyPlanError(plan.objectDiagnostics ?? []);
}

export function assertGraphImportWriteResult(
  plan: GraphImportValidation,
  actual: { nodeCount: number; relationshipCount: number },
): void {
  const expected = {
    nodeCount: plan.nodeCount,
    relationshipCount: plan.relationshipCount,
  };
  if (actual.nodeCount === expected.nodeCount
    && actual.relationshipCount === expected.relationshipCount
    && actual.nodeCount > 0) return;
  throw new GraphImportWriteVerificationError(expected, actual);
}
