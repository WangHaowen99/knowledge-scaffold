import type { PackageSpec } from '../types.js';
import { normalizeObjectFieldAliases } from './field-alias.js';

const FLOW_STEP_ORDINAL = /^(?:(?:第\s*\d+\s*步(?:骤)?\s*)|(?:[（(]\s*\d+\s*[)）]\s*)|(?:\d+\s*、\s*)|(?:\d+\s*[.)）:：-]\s+))/u;

/** operation.flow 只保存步骤正文，Markdown 渲染器是序号的唯一生成者。 */
export function normalizeOperationFlowStep(value: string): string {
  const original = String(value ?? '').trim();
  let normalized = original;
  while (FLOW_STEP_ORDINAL.test(normalized)) {
    normalized = normalized.replace(FLOW_STEP_ORDINAL, '').trimStart();
  }
  return normalized || original;
}

/** Keep executable code in PackageSpec.scripts; operation.script is a binding only. */
export function canonicalizePackageSpec(spec: PackageSpec): PackageSpec {
  return {
    ...spec,
    objects: spec.objects.map(normalizeObjectFieldAliases),
    operations: spec.operations.map((operation) => ({
      ...operation,
      flow: (operation.flow ?? []).map(normalizeOperationFlowStep),
      ...(operation.script?.key
        ? {
          script: {
            key: operation.script.key,
            ...(operation.script.required === undefined
              ? {}
              : { required: operation.script.required }),
          },
        }
        : { script: undefined }),
    })),
  };
}
