export const RUNTIME_GROUP = 'runtime_context';
export const LEGACY_RUNTIME_GROUP = '__runtime_context';
export const COMPILED_RESOURCES_GROUP = 'compiled_resources';

export function isHiddenCandidateGroup(groupId: string): boolean {
  return groupId === RUNTIME_GROUP
    || groupId === LEGACY_RUNTIME_GROUP
    || groupId === COMPILED_RESOURCES_GROUP;
}
