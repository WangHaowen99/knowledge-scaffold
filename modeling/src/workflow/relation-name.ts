const CARDINALITY_WORDS = new Set([
  '一对一',
  '一对多',
  '多对一',
  '多对多',
  'onetoone',
  'onetomany',
  'manytoone',
  'manytomany',
]);

const CARDINALITY_ENDPOINT = '(?:0|1|n|m|x|\\*|0\\.\\.1|0\\.\\.\\*|1\\.\\.\\*)';
const CARDINALITY_PAIR = new RegExp(
  `^${CARDINALITY_ENDPOINT}(?::|-|~|to|对)${CARDINALITY_ENDPOINT}$`,
  'iu',
);
const CARDINALITY_RANGE = /^(?:0|1)\.\.(?:1|n|m|x|\*)$/iu;

export const RELATION_NAME_GENERATION_GUIDANCE =
  'relations[].relation 由模型根据材料证据生成，不从固定枚举中选择；只填写简洁、有方向的中文谓词（如“属于”“包含”“生成”“引用”“依赖”“适用于”），不得包含源对象或目标对象的 key、标题、别名，也不得只填写 1:1、1:N 等关系基数。提交前将“源对象 -> 关系名称 -> 目标对象”连读为自然、符合证据的业务陈述；有更具体证据时避免只写“关联”“对应”。基数确有证据时可写入 note。';

export interface RelationEndpointIdentity {
  key?: string;
  title?: string;
  aliases?: readonly string[];
}

export interface RelationNameContext {
  source?: RelationEndpointIdentity;
  target?: RelationEndpointIdentity;
}

export function normalizeRelationName(value: unknown): string {
  return String(value ?? '').normalize('NFKC').trim();
}

function semanticToken(value: unknown): string {
  return normalizeRelationName(value)
    .toLocaleLowerCase('en-US')
    .replace(/[\s\p{P}\p{S}]+/gu, '');
}

function endpointNames(endpoint: RelationEndpointIdentity | undefined): Array<{ label: string; token: string }> {
  if (!endpoint) return [];
  const names = [endpoint.key, endpoint.title, ...(endpoint.aliases ?? [])]
    .map((item) => ({ label: normalizeRelationName(item), token: semanticToken(item) }))
    .filter((item) => item.token.length >= 2);
  return [...new Map(names.map((item) => [item.token, item])).values()];
}

export function relationNameProblem(
  value: unknown,
  context: RelationNameContext = {},
): string | undefined {
  const name = normalizeRelationName(value);
  if (!name) return '关系名称不能为空';
  const compact = name.replace(/\s+/gu, '').toLocaleLowerCase('en-US');
  const word = compact.replace(/[-_]/gu, '');
  if (CARDINALITY_WORDS.has(word)
    || CARDINALITY_PAIR.test(compact)
    || CARDINALITY_RANGE.test(compact)) {
    return `关系名称不能只填写关系基数「${name}」；请根据材料填写有方向的中文关系名称，基数可写入说明`;
  }

  const relationToken = semanticToken(name);
  for (const [side, endpoint] of [
    ['目标', context.target],
    ['源', context.source],
  ] as const) {
    const repeated = endpointNames(endpoint).find((item) => relationToken.includes(item.token));
    if (repeated) {
      return `关系名称「${name}」不应包含${side}对象名称「${repeated.label}」；请只填写关系谓词，使“源对象 -> 关系名称 -> 目标对象”连读符合业务逻辑`;
    }
  }
  return undefined;
}
