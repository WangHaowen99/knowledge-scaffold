import neo4j from 'neo4j-driver';
import type { GraphImportEntityPlan, GraphImportPropertyType } from './types.js';

function dateParts(value: string): [number, number, number] | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
}

function localDateTimeParts(value: string): [number, number, number, number, number, number, number] | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?$/.exec(value);
  if (!match) return null;
  return [
    Number(match[1]),
    Number(match[2]),
    Number(match[3]),
    Number(match[4]),
    Number(match[5]),
    Number(match[6]),
    0,
  ];
}

function neo4jValue(value: string, type: GraphImportPropertyType, property: string): unknown {
  if (value === '' || type === 'string') return value;
  if (type === 'integer') return neo4j.int(value);
  if (type === 'float') {
    const number = Number(value);
    if (!Number.isFinite(number)) throw new Error(`属性「${property}」无法转换为 Neo4j FLOAT`);
    return number;
  }
  if (type === 'boolean') {
    if (value === 'true') return true;
    if (value === 'false') return false;
    throw new Error(`属性「${property}」无法转换为 Neo4j BOOLEAN`);
  }
  if (type === 'date') {
    const parts = dateParts(value);
    if (!parts) throw new Error(`属性「${property}」无法转换为 Neo4j DATE`);
    return new neo4j.types.Date(...parts);
  }
  const parts = localDateTimeParts(value);
  if (!parts) throw new Error(`属性「${property}」无法转换为 Neo4j LOCAL DATETIME`);
  return new neo4j.types.LocalDateTime(...parts);
}

export function neo4jEntityRows(
  entity: GraphImportEntityPlan,
  rows: GraphImportEntityPlan['rows'],
): Array<{ recordKey: string; properties: Record<string, unknown> }> {
  return rows.map((row) => ({
    recordKey: row.recordKey,
    properties: Object.fromEntries(Object.entries(row.properties).map(([property, value]) => [
      property,
      neo4jValue(value, entity.propertyTypes[property] ?? 'string', property),
    ])),
  }));
}
