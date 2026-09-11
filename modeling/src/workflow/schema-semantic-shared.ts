const normalize = (value: unknown) => String(value ?? '').trim().normalize('NFKC');

export function schemaFallbackDescription(tableName: string): string {
  return `来自 Schema Sheet「${tableName}」的物理数据对象`;
}

export function isSchemaFallbackDescription(value: unknown, tableName: string): boolean {
  const text = normalize(value);
  return !text || text === schemaFallbackDescription(tableName);
}
