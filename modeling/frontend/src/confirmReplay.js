/**
 * 修订冲突后要不要替用户重发那次确认。
 *
 * expectedRevision 的作用是保证「你确认的是你看到的那一版」。任务图的 revision
 * 会因为很多与你无关的事情往前走（后台某个任务状态变了、进度更新），这类冲突
 * 重发是安全的；但如果你要确认的知识包内容本身变了，重发就等于让你确认了一份
 * 自己从没看过的东西——那是这道锁存在的唯一理由，不能绕。
 *
 * 所以判据不是「revision 变没变」，而是「你看到的那份内容还在不在」。
 */
export const CONFIRM_REPLAY_CONTENT_CHANGED = '知识包内容在你确认期间被改动过，已经刷新到最新版本。'
  + '请重新看一遍再确认——原来那次确认针对的是旧内容，不能直接沿用。';

export const CONFIRM_REPLAY_UNKNOWN_BASELINE = '无法确认你刚才看到的是哪一版知识包，已经刷新到最新版本，请重新确认。';

/**
 * @param {{seenPackageRevision?: string, currentPackageRevision?: string}} input
 * @returns {boolean} 内容未变、可以安全重发
 */
export function shouldReplayConfirm({ seenPackageRevision, currentPackageRevision } = {}) {
  if (!seenPackageRevision || !currentPackageRevision) return false;
  return seenPackageRevision === currentPackageRevision;
}

/** 不能重发时该告诉用户什么 */
export function confirmReplayBlockedMessage({ seenPackageRevision, currentPackageRevision } = {}) {
  if (!seenPackageRevision || !currentPackageRevision) return CONFIRM_REPLAY_UNKNOWN_BASELINE;
  return CONFIRM_REPLAY_CONTENT_CHANGED;
}
