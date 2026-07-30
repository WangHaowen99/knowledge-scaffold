import type { PackageDataSourceType, PackageSpec, SpecObject } from '../types.js';
import { tableSourceFor } from './source-references.js';

const DATA_SOURCE_TYPES = new Set<PackageDataSourceType>(['nl2sql', 'mindsdb', 'neo4j']);

export function parsePackageDataSourceType(value: unknown): PackageDataSourceType {
  const normalized = String(value ?? '').trim().toLowerCase() as PackageDataSourceType;
  if (!DATA_SOURCE_TYPES.has(normalized)) {
    throw new Error('请选择后续使用的数据源：NL2SQL、MindsDB 或 Neo4j');
  }
  return normalized;
}

export function packageMappingResource(spec: PackageSpec, object: SpecObject): string {
  if (spec.dataSourceType === 'nl2sql' || spec.dataSourceType === 'mindsdb') {
    return tableSourceFor(object)?.table || object.key;
  }
  return object.key;
}
