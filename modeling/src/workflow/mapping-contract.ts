import type { SpecField, SpecObject } from '../types.js';

export const mappingObjectName = (object: SpecObject): string => object.key;

export const mappingFieldName = (field: SpecField): string => field.key;

export function mappingPrimaryNames(object: SpecObject): string[] {
  return object.primaryKey?.length
    ? [...object.primaryKey]
    : [object.fields[0]?.key].filter((key): key is string => !!key);
}
