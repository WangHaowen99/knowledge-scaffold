// 知识包共享（develop !590 成员向导里的 Hub 同步部分，ADR-0098）：develop 的「建模者」在本分支是知识包的共享用户。

export const SHARING_NOTE = '共享用户将获得 Hub 全部版本读写、授权管理和整包删除权限。移除会撤销该本体的整体授权，包括其他渠道授予的权限。';
export const SHARING_EMPTY = '尚未共享给其他用户';
export const SAVE_FAILED_WARNING = '共享用户已更新，Hub 授权同步失败；移除的用户可能仍可访问知识包';
export const PUBLISH_FAILED_WARNING = '知识包已发布，共享用户授权同步失败，请在知识包共享中重试';

const USERNAME_RE = /^[A-Za-z0-9_.-]{1,64}$/;

// 添加：按用户名；不合法、是所有者、已在名单里时给原因
export function addProblem(name, users, owner) {
  const value = String(name || '').trim();
  if (!value) return '请输入用户名';
  if (!USERNAME_RE.test(value)) return '用户名只能有字母、数字、点、下划线和短横线';
  if (value === owner) return '所有者已有全部权限';
  if ((users || []).includes(value)) return '已在共享名单里';
  return '';
}

// 某个用户的同步状态：有待同步的授权 / 撤权时给失败原因与重试按钮的说法
export function userSync(hubSync, user) {
  const action = hubSync?.pending?.[user];
  if (!action) return null;
  return {
    action,
    reason: hubSync.results?.[user]?.error || '授权变更尚未同步，请重试',
    retry: action === 'revoke' ? '重试撤权' : '重试授权',
  };
}

// 不在名单里、但还有待同步的撤权：名单下面单列，便于重试
export const pendingRevokes = (hubSync, users) => Object.entries(hubSync?.pending || {})
  .filter(([name, action]) => action === 'revoke' && !(users || []).includes(name))
  .map(([name]) => name);

export const syncFailed = (hubSync) => hubSync?.status === 'failed';
