const DETERMINISTIC_PACK = 'deterministic-execution';
const CODEGEN_PACK = 'codegen';

const labelOf = (name, packs) => packs.find((pack) => pack.name === name)?.label || name;

export function deterministicDependency(packs = []) {
  const pack = packs.find((item) => item.name === DETERMINISTIC_PACK);
  const range = pack?.requires?.depends?.[CODEGEN_PACK];
  if (!pack || !range) return null;
  return { pack, name: CODEGEN_PACK, label: labelOf(CODEGEN_PACK, packs), range };
}

export const deterministicDependencyMissing = (caps = {}) => (
  !!caps[DETERMINISTIC_PACK] && !caps[CODEGEN_PACK]
);
