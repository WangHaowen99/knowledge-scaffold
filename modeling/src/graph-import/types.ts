export interface GraphImportPropertyMapping {
  property: string;
  sourceColumn: string;
}

export interface GraphImportObjectSourceSchema {
  sheet: string;
  role: 'primary' | 'extension';
  propertyMappings: GraphImportPropertyMapping[];
}

export interface GraphImportObjectSchema {
  key: string;
  title: string;
  label: string;
  defaultSheet: string;
  propertyMappings: GraphImportPropertyMapping[];
  primaryKey?: string[];
  sources?: GraphImportObjectSourceSchema[];
}

export interface GraphImportRelationSchema {
  id: string;
  sourceObject: string;
  relationName: string;
  targetObject: string;
  note?: string;
}

export interface GraphImportValueBindingSchema {
  key: string;
  label: string;
  defaultSheet: string;
  valueField: string;
  semanticField: string;
  aliasFields: string[];
  propertyMappings: GraphImportPropertyMapping[];
}

export interface GraphImportPackageSchema {
  packageVersion: string;
  packageObjectCount?: number;
  objects: GraphImportObjectSchema[];
  valueBindings: GraphImportValueBindingSchema[];
  relations: GraphImportRelationSchema[];
}

export type GraphRelationMatchMode = 'same_row' | 'field_match';

interface GraphRelationMappingBase {
  sourceObject: string;
  relationName: string;
  targetObject: string;
  matchMode: GraphRelationMatchMode;
}

export interface GraphSameRowRelationMapping extends GraphRelationMappingBase {
  matchMode: 'same_row';
}

export interface GraphFieldMatchRelationMapping extends GraphRelationMappingBase {
  matchMode: 'field_match';
  sourceJoinProperty: string;
  targetJoinProperty: string;
}

export type GraphRelationMapping = GraphSameRowRelationMapping | GraphFieldMatchRelationMapping;

export interface GraphImportMapping {
  relations: GraphRelationMapping[];
}

export type GraphImportPropertyType =
  | 'string'
  | 'integer'
  | 'float'
  | 'boolean'
  | 'date'
  | 'local_datetime';

export interface GraphImportEntityRow {
  recordKey: string;
  properties: Record<string, string>;
}

export interface GraphImportEntityPlan {
  objectKey: string;
  label: string;
  sheet: string;
  propertyNames: string[];
  propertyTypes: Record<string, GraphImportPropertyType>;
  rows: GraphImportEntityRow[];
}

export interface GraphImportRelationshipPair {
  sourceRecordKey: string;
  targetRecordKey: string;
}

export interface GraphImportRelationPlan {
  relationName: string;
  sourceObject: string;
  sourceLabel: string;
  targetObject: string;
  targetLabel: string;
  pairs: GraphImportRelationshipPair[];
}

export interface GraphImportObjectDiagnostic {
  objectKey: string;
  sheet: string;
  code: 'GRAPH_IMPORT_EMPTY_SHEET'
    | 'GRAPH_IMPORT_NO_MAPPED_COLUMNS'
    | 'GRAPH_IMPORT_NO_IMPORTABLE_ROWS';
  message: string;
}

export interface GraphImportValidation {
  nodeCount: number;
  relationshipCount: number;
  skippedBlankJoinCount: number;
  skippedUnmatchedJoinCount: number;
  warnings: string[];
  entities: GraphImportEntityPlan[];
  relations: GraphImportRelationPlan[];
  objectDiagnostics?: GraphImportObjectDiagnostic[];
}
