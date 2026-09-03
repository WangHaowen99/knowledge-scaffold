export function studioDraftEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function studioChangedFields(baseline, draft) {
  return Object.fromEntries(
    Object.keys(draft)
      .filter((key) => !studioDraftEqual(draft[key], baseline[key]))
      .map((key) => [key, draft[key]]),
  );
}

export function studioDraftFieldValue(draft, values, field) {
  if (draft && Object.prototype.hasOwnProperty.call(draft, field)) return draft[field];
  return values?.[field];
}

export function studioSyncedSaveState() {
  return { phase: 'idle', message: '', failure: null, fieldErrors: {} };
}

export function studioSaveStateAfterDraftChange(
  current,
  { baseline, draft, field, saving = false, incompleteField = '' },
) {
  if (!saving && studioDraftEqual(draft, baseline)) return studioSyncedSaveState();
  const fieldErrors = clearStudioFieldErrors(current?.fieldErrors, field);
  if (incompleteField) {
    return {
      phase: 'error',
      message: '请先补完整必填内容',
      failure: null,
      fieldErrors: {
        ...fieldErrors,
        [`changes.${incompleteField}`]: '必填内容不能为空',
      },
    };
  }
  return {
    phase: 'dirty',
    message: '有未保存修改',
    failure: null,
    fieldErrors,
  };
}

export function isStudioNoChangesFailure(failure) {
  return failure?.code === 'NO_CHANGES';
}

export function studioIncompleteDraftField(draft, schema) {
  const requiredColumns = {
    field_table: ['key', 'type'],
    parameter_table: ['param', 'type'],
    output_table: ['field', 'type'],
    condition_table: ['condition', 'conclusion'],
  };
  for (const field of schema?.fields || []) {
    if (field.readOnly) continue;
    const value = draft?.[field.key];
    if (field.required && ['text', 'long_text'].includes(field.type)
      && !String(value ?? '').trim()) return field.key;
    if (['text_list', 'ordered_text_list'].includes(field.type)
      && Array.isArray(value) && value.some((item) => !String(item ?? '').trim())) return field.key;
    const columns = field.type === 'key_value_table'
      ? (field.key === 'nullPolicy' ? ['field', 'handling'] : ['key'])
      : requiredColumns[field.type];
    if (!columns || !Array.isArray(value)) continue;
    if (value.some((row) => columns.some((column) => !String(row?.[column] ?? '').trim()))) return field.key;
  }
  return '';
}

export function studioRelationElements(elements = [], graph = null) {
  const result = new Map(elements.map((element) => [element.id, element]));
  for (const node of graph?.nodes || []) {
    if (result.has(node.id) || node.nodeType !== 'element') continue;
    result.set(node.id, {
      id: node.id,
      kind: node.kind,
      key: String(node.id).split(':').slice(1).join(':'),
      title: node.label || node.id,
      description: '',
      relationCount: 0,
    });
  }
  return [...result.values()];
}

export function studioDeleteCopy(kind, title, count = 1) {
  if (count !== 1) {
    return {
      button: `删除所选 ${count} 个知识条目`,
      heading: `确认删除 ${count} 个知识条目`,
      clarification: `这会删除所选的 ${count} 个完整知识条目，不是只撤销字段修改。`,
    };
  }
  const label = { object: '对象', operation: '操作', rule: '规则', metric: '指标' }[kind] || '知识条目';
  return {
    button: `删除整个${label}`,
    heading: `确认删除整个${label}`,
    clarification: `这会删除整个${label}《${title || '未命名'}》，不是只撤销字段修改。`,
  };
}

function clearStudioFieldErrors(fieldErrors = {}, field = '') {
  if (!field) return {};
  return Object.fromEntries(Object.entries(fieldErrors).filter(([path]) => (
    path !== `changes.${field}`
    && !path.startsWith(`changes.${field}[`)
    && !path.startsWith(`changes.${field}.`)
  )));
}
