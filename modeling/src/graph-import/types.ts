export interface GraphImportPropertyMapping {
  property: string;
  sourceColumn: string;
}

export interface GraphImportObjectSchema {
  key: string;
  title: string;
  label: string;
  defaultSheet: string;
  primaryProperties: string[];
  propertyMappings: GraphImportPropertyMapping[];
}

export interface GraphImportRelationSchema {
  id: string;
  sourceObject: string;
  relationName: string;
  targetObject: string;
  note?: string;
}

export interface GraphImportPackageSchema {
  packageVersion: string;
  objects: GraphImportObjectSchema[];
  relations: GraphImportRelationSchema[];
}

export interface GraphRelationMapping {
  sourceObject: string;
  relationName: string;
  targetObject: string;
  sourceJoinProperty: string;
  targetJoinProperty: string;
}

export interface GraphImportMapping {
  relations: GraphRelationMapping[];
}

export interface GraphImportEntityRow {
  identityValues: string[];
  properties: Record<string, string>;
}

export interface GraphImportEntityPlan {
  objectKey: string;
  label: string;
  sheet: string;
  identityProperties: string[];
  propertyNames: string[];
  rows: GraphImportEntityRow[];
}

export interface GraphImportRelationshipPair {
  sourceIdentityValues: string[];
  targetIdentityValues: string[];
}

export interface GraphImportRelationPlan {
  relationName: string;
  sourceObject: string;
  sourceLabel: string;
  sourceIdentityProperties: string[];
  targetObject: string;
  targetLabel: string;
  targetIdentityProperties: string[];
  pairs: GraphImportRelationshipPair[];
}

export interface GraphImportValidation {
  nodeCount: number;
  relationshipCount: number;
  skippedBlankJoinCount: number;
  skippedUnmatchedJoinCount: number;
  warnings: string[];
  entities: GraphImportEntityPlan[];
  relations: GraphImportRelationPlan[];
}
