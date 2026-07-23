const MAPPING_FIELDS = [
  'sourceObject',
  'relationName',
  'targetObject',
  'sourceJoinProperty',
  'targetJoinProperty',
];

export const GRAPH_ISOLATION_EXISTS_PROMPT = '当前您所选的隔离字段值在图数据库中已存在数据，是否要清空旧数据，并写入新数据？';

export function isGraphIsolationConflict(error) {
  return error?.status === 409 && error?.data?.code === 'GRAPH_ISOLATION_DATA_EXISTS';
}

export function graphImportContextKey(projectId, dataSource) {
  const config = dataSource?.config || {};
  return JSON.stringify([
    projectId || '',
    dataSource?.updatedAt || '',
    dataSource?.mode || '',
    dataSource?.provider || '',
    config.uri || '',
    config.username || '',
    config.database || '',
    config.workspace_property || '',
    config.workspace_value || '',
    config.ontology_property || '',
    config.ontology_value || '',
  ]);
}

export async function runGraphImportContextRequest({
  contextKey,
  isCurrent,
  request,
  onStale,
}) {
  try {
    const value = await request();
    if (isCurrent(contextKey)) return { kind: 'current', value };
    try { await onStale?.(value); } catch { /* stale cleanup is best-effort */ }
    return { kind: 'stale' };
  } catch (error) {
    if (!isCurrent(contextKey)) return { kind: 'stale' };
    throw error;
  }
}

export async function startGraphImportExecution({ execute, request, isCurrent }) {
  try {
    const result = await execute(
      request.projectId,
      request.uploadId,
      request.payload,
      request.password,
      false,
    );
    return isCurrent(request.contextKey)
      ? { kind: 'started', run: result.run }
      : { kind: 'stale' };
  } catch (error) {
    if (!isGraphIsolationConflict(error)) throw error;
    return isCurrent(request.contextKey)
      ? { kind: 'conflict', request }
      : { kind: 'stale' };
  }
}

export function hasRunningGraphImport(imports) {
  return Array.isArray(imports) && imports.some((item) => item?.status === 'running');
}

export function activeGraphImportRun(imports, activeRunId) {
  if (!activeRunId || !Array.isArray(imports)) return null;
  return imports.find((item) => item?.id === activeRunId) || null;
}

function objectByKey(preview, key) {
  return preview?.schema?.objects?.find((object) => object.key === key);
}

function sheetByName(preview, name) {
  return preview?.sheets?.find((sheet) => sheet.name === name);
}

export function objectImportIssue(preview, objectOrKey) {
  const object = typeof objectOrKey === 'string' ? objectByKey(preview, objectOrKey) : objectOrKey;
  if (!object) return '知识包中不存在该实体';
  const sheet = sheetByName(preview, object.defaultSheet);
  if (!sheet) return `固定 Sheet「${object.defaultSheet}」不存在于 Excel`;
  if (!object.primaryProperties?.length) return '知识包未声明主键';
  for (const property of object.primaryProperties) {
    const mapping = object.propertyMappings?.find((item) => item.property === property);
    if (!mapping) return `主键属性「${property}」未声明字段映射`;
    if (!sheet.columns.includes(mapping.sourceColumn)) {
      return `主键属性「${property}」对应字段「${mapping.sourceColumn}」不存在`;
    }
  }
  return '';
}

export function joinPropertyOptions(preview, objectKey) {
  const object = objectByKey(preview, objectKey);
  const sheet = sheetByName(preview, object?.defaultSheet);
  if (!object || !sheet) return [];
  return (object.propertyMappings || [])
    .filter((mapping) => sheet.columns.includes(mapping.sourceColumn))
    .map((mapping) => ({
      value: mapping.property,
      label: mapping.property === mapping.sourceColumn
        ? mapping.property
        : `${mapping.property}（${mapping.sourceColumn}）`,
    }));
}

function defaultJoinProperty(preview, object) {
  const options = joinPropertyOptions(preview, object?.key);
  return object?.primaryProperties?.find((property) => options.some((option) => option.value === property))
    || options[0]?.value
    || '';
}

export function relationDraftFromKnown(relation) {
  return {
    sourceObject: relation?.sourceObject || '',
    relationName: relation?.relationName || '',
    targetObject: relation?.targetObject || '',
  };
}

export function addRelationMapping(mappings, draft, preview) {
  const sourceObject = objectByKey(preview, draft?.sourceObject);
  const targetObject = objectByKey(preview, draft?.targetObject);
  const relationName = String(draft?.relationName || '').trim();
  if (!sourceObject) throw new Error('请选择源实体');
  const sourceIssue = objectImportIssue(preview, sourceObject);
  if (sourceIssue) throw new Error(`源实体「${sourceObject.title || sourceObject.key}」不可导入：${sourceIssue}`);
  if (!relationName) throw new Error('请填写关系名称');
  if (!targetObject) throw new Error('请选择目标实体');
  const targetIssue = objectImportIssue(preview, targetObject);
  if (targetIssue) throw new Error(`目标实体「${targetObject.title || targetObject.key}」不可导入：${targetIssue}`);
  if (mappings.some((item) => item.sourceObject === sourceObject.key
    && item.relationName === relationName && item.targetObject === targetObject.key)) {
    throw new Error('相同源实体、关系名称和目标实体的关系不能重复添加');
  }
  return [...mappings, {
    sourceObject: sourceObject.key,
    relationName,
    targetObject: targetObject.key,
    sourceJoinProperty: defaultJoinProperty(preview, sourceObject),
    targetJoinProperty: defaultJoinProperty(preview, targetObject),
  }];
}

export function updateRelationMapping(mappings, index, field, value) {
  if (!MAPPING_FIELDS.includes(field)) throw new Error(`未知映射字段：${field}`);
  return mappings.map((mapping, currentIndex) => (
    currentIndex === index ? { ...mapping, [field]: value } : mapping
  ));
}

export function graphImportMappingPayload(mappings) {
  const relations = mappings.map((mapping) => Object.fromEntries(
    MAPPING_FIELDS.map((field) => [field, String(mapping[field] ?? '').trim()]),
  ));
  if (relations.some((relation) => MAPPING_FIELDS.some((field) => !relation[field]))) {
    throw new Error('请完整配置每条关系的实体、关系名称和关联属性');
  }
  return { relations };
}

export function graphRelationLabel(relation, preview) {
  const source = objectByKey(preview, relation.sourceObject);
  const target = objectByKey(preview, relation.targetObject);
  return `${source?.title || relation.sourceObject} -[${relation.relationName}]-> ${target?.title || relation.targetObject}`;
}
