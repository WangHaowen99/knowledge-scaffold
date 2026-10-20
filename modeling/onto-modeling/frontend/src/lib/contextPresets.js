// 模型上下文推荐表的页面编辑：模型名规范化、重名检查、自定义条目增删与表单转换。
// 规范化与校验规则须与后端 model_context_presets 一致，后端保存时会再按同一套规则校验。

// 网关常在模型名后追加 6 或 8 位日期版本
const DATE_SUFFIX = /-(\d{8}|\d{6})$/;

export const VERIFIED_AT_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

// 小写、去掉 provider/ 前缀、把 . 和 _ 统一成 -、去掉网关日期后缀
export function normalizeModelName(name) {
  const base = (name || '').trim().toLowerCase().split('/').pop();
  return base.replace(/[._]/g, '-').replace(DATE_SUFFIX, '');
}

const sameModel = (a, b) => normalizeModelName(a) === normalizeModelName(b);

// 表中每行可做的操作：内置条目可覆盖；覆盖了内置的自定义条目可编辑、恢复内置；新增的自定义条目可编辑、删除
export function rowActions(row) {
  if (row.origin !== 'custom') return ['override'];
  return row.overrides_builtin ? ['edit', 'restore'] : ['edit', 'delete'];
}

// 推荐表中与 name 规范化后重名的条目（replacedId 是正在编辑或覆盖、保存后会被替换的那条，不算）；没有返回 null
export function nameOwner(name, presets, replacedId) {
  const key = normalizeModelName(name);
  return (
    presets.find(
      (preset) =>
        !(replacedId && sameModel(preset.id, replacedId)) &&
        [preset.id, ...preset.aliases].some((other) => normalizeModelName(other) === key),
    ) || null
  );
}

// 按规范化 id 替换自定义条目（保持原位置），没有就追加
export function upsertEntry(entries, entry) {
  const index = entries.findIndex((item) => sameModel(item.id, entry.id));
  if (index < 0) return [...entries, entry];
  return entries.map((item, position) => (position === index ? entry : item));
}

export function removeEntry(entries, id) {
  return entries.filter((item) => !sameModel(item.id, id));
}

export const splitLines = (text) =>
  (text || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);

export const isSourceUrl = (url) => /^https:\/\//.test(url);

// 条目 → 表单值：别名用标签输入，来源链接每行一个
export function entryFormValues(entry) {
  return {
    id: entry?.id ?? '',
    vendor: entry?.vendor ?? '',
    aliases: entry?.aliases ?? [],
    max_context_tokens: entry?.max_context_tokens ?? null,
    max_output_tokens: entry?.max_output_tokens ?? null,
    note: entry?.note ?? '',
    sources: (entry?.sources ?? []).join('\n'),
  };
}

export function entryFromForm(values) {
  return {
    id: values.id.trim(),
    vendor: (values.vendor || '').trim(),
    aliases: (values.aliases || []).map((alias) => alias.trim()).filter(Boolean),
    max_context_tokens: values.max_context_tokens,
    max_output_tokens: values.max_output_tokens,
    note: (values.note || '').trim(),
    sources: splitLines(values.sources),
  };
}
