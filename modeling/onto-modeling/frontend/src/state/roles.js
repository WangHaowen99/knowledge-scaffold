// 角色与能力（契约 1.3、8.1 #26～#28）：
// - role 单值 user / admin / ops；B2 起 UserProfile 另带 roles（生效的全部角色），本地模式三种都有；
// - 运维管理面：ops 读写，admin 只读；
// - 用户管理（AD-01～AD-05）：admin 读写，ops 只读；回收站：ops 和 admin 都能操作；
// - 系统设置、插件上传与审核、平台安装提供方、服务令牌、指定项目所有者、审核申请：只有 admin。

export const ROLE_LABEL = { user: '建模者', admin: '管理员', ops: '运维' };

export function rolesOf(user) {
  if (!user) return [];
  const set = new Set(['user']);
  if (Array.isArray(user.roles)) user.roles.forEach((r) => set.add(r));
  if (user.role) set.add(user.role);
  if (user.auth_source === 'local') ['admin', 'ops'].forEach((r) => set.add(r));
  return [...set];
}

const has = (user, r) => rolesOf(user).includes(r);

// 能力名 → 是否允许
const RULES = {
  'ops.read': (u) => has(u, 'ops') || has(u, 'admin'),
  'ops.write': (u) => has(u, 'ops'),
  'users.read': (u) => has(u, 'ops') || has(u, 'admin'),
  'users.write': (u) => has(u, 'admin'),
  'trash.write': (u) => has(u, 'ops') || has(u, 'admin'),
  admin: (u) => has(u, 'admin'),
};

export function can(user, ability) {
  const rule = RULES[ability];
  return rule ? !!rule(user) : false;
}

// 只读时按钮旁边的说明（悬浮提示）
export function deniedHint(ability) {
  if (ability === 'ops.write') return '仅运维可操作';
  if (ability === 'users.write') return '只有管理员能管理用户，运维只能查看';
  if (ability === 'admin') return '需要管理员权限';
  return '没有权限';
}

// 运维中心左栏的身份卡：名字、角色标签；管理员另标运维面只读
export function opsRoleCard(user) {
  const rs = rolesOf(user);
  if (rs.includes('ops') && rs.includes('admin')) {
    return { role: 'ops', label: user?.auth_source === 'local' ? '本地模式' : '运维 · 管理员', text: null };
  }
  if (rs.includes('ops')) return { role: 'ops', label: '运维', text: null };
  if (rs.includes('admin')) return { role: 'admin', label: '管理员', text: '运维面只读' };
  return null;
}

// 运维管理面地址里的端口（C-01 ops_base_url；空串表示与业务面同源、经反代转发）
export function opsListenLabel(opsBaseUrl) {
  if (!opsBaseUrl) return null;
  try {
    const u = new URL(opsBaseUrl);
    return u.port ? `:${u.port}` : u.host;
  } catch {
    return null;
  }
}
