import type {
  DataDictTable,
  PackageSpec,
  SpecField,
  SpecObject,
  SpecValueBinding,
} from '../types.js';
import {
  suggestSemanticFieldRefs,
  suggestSemanticBindings,
  type SemanticFieldRefSuggestion,
  type SemanticBindingSuggestion,
} from './semantic-binding.js';

export interface SemanticBindingApplyInput {
  expectedRevision: string;
  bindingKeys: string[];
  fieldRefs: Array<{
    objectKey: string;
    fieldKey: string;
    bindingKey: string;
  }>;
}

export interface SemanticBindingApplyResult {
  spec: PackageSpec;
  appliedBindings: string[];
  appliedFieldRefs: string[];
}

export interface SemanticBindingClearResult {
  spec: PackageSpec;
  clearedBindings: number;
  clearedFieldRefs: number;
}

function cloneSpec(spec: PackageSpec): PackageSpec {
  return JSON.parse(JSON.stringify(spec)) as PackageSpec;
}

function refId(ref: { objectKey: string; fieldKey: string; bindingKey: string }): string {
  return `${ref.objectKey}.${ref.fieldKey}->${ref.bindingKey}`;
}

function normalized(value: string): string {
  return value.trim().normalize('NFC').toLowerCase();
}

function bindingSignature(input: { resource: string; valueField: string; semanticField: string }): string {
  return [input.resource, input.valueField, input.semanticField].map(normalized).join('\u0000');
}

function objectFieldByKey(spec: PackageSpec): Map<string, { object: SpecObject; field: SpecField }> {
  const result = new Map<string, { object: SpecObject; field: SpecField }>();
  for (const object of spec.objects ?? []) {
    for (const field of object.fields ?? []) {
      result.set(`${object.key}.${field.key}`, { object, field });
    }
  }
  return result;
}

function suggestionBinding(suggestion: SemanticBindingSuggestion): SpecValueBinding {
  return {
    key: suggestion.key,
    providerAlias: suggestion.providerAlias,
    resource: suggestion.resource,
    valueField: suggestion.valueField,
    semanticField: suggestion.semanticField,
    ...(suggestion.aliasFields.length ? { aliasFields: suggestion.aliasFields } : {}),
  };
}

function uniqueBindings(bindings: SpecValueBinding[] = []): SpecValueBinding[] {
  const seen = new Set<string>();
  return bindings.filter((binding) => {
    const signature = bindingSignature(binding);
    if (seen.has(signature)) return false;
    seen.add(signature);
    return true;
  });
}

function hasExtractedObjectFields(spec: PackageSpec): boolean {
  return (spec.objects ?? []).some((object) => (object.fields ?? []).length > 0);
}

export function previewSemanticBindingCandidates(
  spec: PackageSpec,
  tables: DataDictTable[],
): {
  suggestions: SemanticBindingSuggestion[];
  fieldSuggestions: SemanticFieldRefSuggestion[];
  confirmedBindings: SpecValueBinding[];
  bindingCount: number;
  fieldRefCount: number;
} {
  const confirmedBindings = uniqueBindings(spec.valueBindings);
  if (!hasExtractedObjectFields(spec)) {
    return {
      suggestions: [],
      fieldSuggestions: [],
      confirmedBindings,
      bindingCount: 0,
      fieldRefCount: 0,
    };
  }
  const suggestions = suggestSemanticBindings(spec, tables);
  const fieldSuggestions = suggestSemanticFieldRefs(spec, tables);
  return {
    suggestions,
    fieldSuggestions,
    confirmedBindings,
    bindingCount: suggestions.length,
    fieldRefCount: fieldSuggestions.length,
  };
}

export function applySemanticBindingSelection(
  baseSpec: PackageSpec,
  tables: DataDictTable[],
  input: Omit<SemanticBindingApplyInput, 'expectedRevision'>,
): SemanticBindingApplyResult {
  const selectedBindingKeys = new Set((input.bindingKeys ?? []).map((item) => String(item).trim()).filter(Boolean));
  for (const ref of input.fieldRefs ?? []) {
    const bindingKey = String(ref.bindingKey ?? '').trim();
    if (bindingKey) selectedBindingKeys.add(bindingKey);
  }
  const selectedFieldRefs = new Set((input.fieldRefs ?? []).map((item) => refId({
    objectKey: String(item.objectKey ?? '').trim(),
    fieldKey: String(item.fieldKey ?? '').trim(),
    bindingKey: String(item.bindingKey ?? '').trim(),
  })));
  const suggestions = suggestSemanticBindings(baseSpec, tables);
  const suggestionsByKey = new Map(suggestions.map((item) => [item.key, item]));
  const spec = cloneSpec(baseSpec);
  const existingBindings = new Set((spec.valueBindings ?? []).map((binding) => binding.key));
  const existingBindingSignatures = new Set((spec.valueBindings ?? []).map(bindingSignature));
  const nextBindings = [...(spec.valueBindings ?? [])];
  const appliedBindings: string[] = [];

  for (const key of selectedBindingKeys) {
    const suggestion = suggestionsByKey.get(key);
    if (!suggestion || existingBindings.has(key)) continue;
    const signature = bindingSignature(suggestion);
    if (existingBindingSignatures.has(signature)) continue;
    nextBindings.push(suggestionBinding(suggestion));
    existingBindings.add(key);
    existingBindingSignatures.add(signature);
    appliedBindings.push(key);
  }
  if (nextBindings.length) spec.valueBindings = nextBindings;
  else delete spec.valueBindings;

  const fields = objectFieldByKey(spec);
  const appliedFieldRefs: string[] = [];
  for (const ref of suggestSemanticFieldRefs(spec, tables)) {
    if (!existingBindings.has(ref.bindingKey)) continue;
    const id = refId(ref);
    if (!selectedFieldRefs.has(id)) continue;
    const target = fields.get(`${ref.objectKey}.${ref.fieldKey}`);
    if (!target || target.field.valueBindingRef === ref.bindingKey) continue;
    target.field.valueBindingRef = ref.bindingKey;
    appliedFieldRefs.push(id);
  }

  return {
    spec,
    appliedBindings,
    appliedFieldRefs,
  };
}

export function clearSemanticBindings(baseSpec: PackageSpec): SemanticBindingClearResult {
  const spec = cloneSpec(baseSpec);
  const clearedBindings = spec.valueBindings?.length ?? 0;
  if (clearedBindings) delete spec.valueBindings;

  let clearedFieldRefs = 0;
  for (const object of spec.objects ?? []) {
    for (const field of object.fields ?? []) {
      if (!field.valueBindingRef) continue;
      delete field.valueBindingRef;
      clearedFieldRefs += 1;
    }
  }

  return {
    spec,
    clearedBindings,
    clearedFieldRefs,
  };
}
