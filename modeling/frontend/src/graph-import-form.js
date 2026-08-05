const RELATION_FIELDS = [
  'sourceObject',
  'relationName',
  'targetObject',
];

const MAPPING_FIELDS = [
  ...RELATION_FIELDS,
  'matchMode',
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
  if (!(object.propertyMappings || []).some((mapping) => sheet.columns.includes(mapping.sourceColumn))) {
    return `固定 Sheet「${object.defaultSheet}」不包含该实体任何映射字段`;
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
  return options[0]?.value || '';
}

export function relationCanMatchSameRow(preview, relation) {
  const source = objectByKey(preview, relation?.sourceObject);
  const target = objectByKey(preview, relation?.targetObject);
  return Boolean(source?.defaultSheet && source.defaultSheet === target?.defaultSheet);
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
  const relation = {
    sourceObject: sourceObject.key,
    relationName,
    targetObject: targetObject.key,
  };
  if (relationCanMatchSameRow(preview, relation)) {
    return [...mappings, { ...relation, matchMode: 'same_row' }];
  }
  return [...mappings, {
    ...relation,
    matchMode: 'field_match',
    sourceJoinProperty: defaultJoinProperty(preview, sourceObject),
    targetJoinProperty: defaultJoinProperty(preview, targetObject),
  }];
}

export function addKnownRelationMappings(mappings, preview) {
  return (preview?.schema?.relations || []).reduce((current, relation) => {
    const draft = relationDraftFromKnown(relation);
    const relationName = String(draft.relationName || '').trim();
    const alreadyAdded = current.some((mapping) => mapping.sourceObject === draft.sourceObject
      && mapping.relationName === relationName && mapping.targetObject === draft.targetObject);
    return alreadyAdded ? current : addRelationMapping(current, draft, preview);
  }, mappings);
}

export function updateRelationMapping(mappings, index, field, value, preview) {
  if (!MAPPING_FIELDS.includes(field)) throw new Error(`未知映射字段：${field}`);
  return mappings.map((mapping, currentIndex) => {
    if (currentIndex !== index) return mapping;
    if (field !== 'matchMode') return { ...mapping, [field]: value };
    if (value === 'same_row') {
      if (!relationCanMatchSameRow(preview, mapping)) {
        throw new Error('仅同一 Sheet 的实体支持同一数据行关联');
      }
      return {
        sourceObject: mapping.sourceObject,
        relationName: mapping.relationName,
        targetObject: mapping.targetObject,
        matchMode: 'same_row',
      };
    }
    if (value !== 'field_match') throw new Error(`未知的关系关联方式：${value}`);
    const sourceObject = objectByKey(preview, mapping.sourceObject);
    const targetObject = objectByKey(preview, mapping.targetObject);
    return {
      ...mapping,
      matchMode: 'field_match',
      sourceJoinProperty: mapping.sourceJoinProperty || defaultJoinProperty(preview, sourceObject),
      targetJoinProperty: mapping.targetJoinProperty || defaultJoinProperty(preview, targetObject),
    };
  });
}

export function graphImportMappingPayload(mappings) {
  const relations = mappings.map((mapping) => {
    const relation = Object.fromEntries(
      RELATION_FIELDS.map((field) => [field, String(mapping[field] ?? '').trim()]),
    );
    const matchMode = String(mapping.matchMode ?? '').trim() || 'field_match';
    if (matchMode === 'same_row') return { ...relation, matchMode };
    if (matchMode !== 'field_match') throw new Error(`未知的关系关联方式：${matchMode}`);
    return {
      ...relation,
      matchMode,
      sourceJoinProperty: String(mapping.sourceJoinProperty ?? '').trim(),
      targetJoinProperty: String(mapping.targetJoinProperty ?? '').trim(),
    };
  });
  if (relations.some((relation) => RELATION_FIELDS.some((field) => !relation[field]))) {
    throw new Error('请完整配置每条关系的源实体、关系名称和目标实体');
  }
  if (relations.some((relation) => relation.matchMode === 'field_match'
    && (!relation.sourceJoinProperty || !relation.targetJoinProperty))) {
    throw new Error('按字段值匹配时，请完整配置源和目标关联属性');
  }
  return { relations };
}

export function graphRelationLabel(relation, preview) {
  const source = objectByKey(preview, relation.sourceObject);
  const target = objectByKey(preview, relation.targetObject);
  const suffix = relation.matchMode === 'same_row' ? '（同 Sheet 同行）' : '';
  return `${source?.title || relation.sourceObject} -[${relation.relationName}]-> ${target?.title || relation.targetObject}${suffix}`;
}
