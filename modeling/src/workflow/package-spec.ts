import type { PackageSpec } from '../types.js';

/** Keep executable code in PackageSpec.scripts; operation.script is a binding only. */
export function canonicalizePackageSpec(spec: PackageSpec): PackageSpec {
  return {
    ...spec,
    operations: spec.operations.map((operation) => ({
      ...operation,
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
