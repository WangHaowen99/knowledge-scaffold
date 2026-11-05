// 「历史」页签：迭代与发布版本由后端合并分页（GET /history），这里只管条目文案与分页累积。
const VERSION_KIND_LABEL = { iteration: '迭代', release: '发布版本' };

export const versionTitle = (kind, version) => `${VERSION_KIND_LABEL[kind]} ${version}`;

export const historyEntryKey = (entry) =>
  (entry.kind === 'release' ? `release:${entry.release_id}` : `iteration:${entry.commit}`);

export const EMPTY_HISTORY = { items: [], nextCursor: null, releaseError: '', error: '' };

// 把后端一页结果接到已加载的列表后面；首页传 EMPTY_HISTORY
export function appendHistoryPage(previous, page) {
  return {
    items: [...previous.items, ...page.items],
    nextCursor: page.next_cursor || null,
    releaseError: previous.releaseError || page.release_error || '',
    error: '',
  };
}
