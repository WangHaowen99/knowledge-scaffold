import { MAX_NAME_LENGTH, projectNameLengthState } from '../../lib/projectName.js';

export { limitProjectName } from '../../lib/projectName.js';

// 项目名称的字数上限：输入时截到上限（按 Unicode 码点，与后端同一口径），到上限时字数变色并给提示
export const NAME_LIMIT_TEXT = `已达到 ${MAX_NAME_LENGTH} 个字符上限`;

export function nameAtLimit(value) {
  return projectNameLengthState(value).atLimit;
}

export function NameCount({ value }) {
  const { atLimit, label } = projectNameLengthState(value);
  return <span className={atLimit ? 'xs' : 'xs faint'} style={{ flex: 'none', ...(atLimit ? { color: 'var(--amber)' } : null) }}>{label}</span>;
}

export function NameLimitHint() {
  return <span style={{ color: 'var(--amber)' }} aria-live="polite">{NAME_LIMIT_TEXT}</span>;
}
