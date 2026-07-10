// 权限点注册表（前端镜像，仅做 UI 门控；后端是唯一强制点）。
// 权限模型：permission = action × resource × scope（全局 / 空间 / 个人）。
// 现阶段角色只有 admin / user；结构上为 workspace 级角色（workspace_admin）预留了 wsRoles 位。
export const ROLES = { ADMIN: 'admin', USER: 'user' };

const RULES = {
  // 全局层（L1）
  'system_config.read':   { roles: ['admin', 'user'] },
  'system_config.write':  { roles: ['admin'] },
  'workspace.manage':     { roles: ['admin'] },            // 空间集合的增删/设默认
  'users.manage':         { roles: ['admin'] },
  'observability.global': { roles: ['admin'] },
  // 空间层（L2）——wsRoles 仅用于后端已放开 owner 通道的权限点，
  // 避免 owner 看到可写 UI 但后端 admin-only 必 403；其余待后端放开后再补 wsRoles。
  'ws.config.read':       { roles: ['admin', 'user'] },
  'ws.info.write':        { roles: ['admin'], wsRoles: ['admin'] },   // PATCH 改名/描述（owner 可）
  'ws.members.write':     { roles: ['admin'], wsRoles: ['admin'] },   // PUT members（owner 可）

  'ws.config.write':      { roles: ['admin'] },   // runtime-config PATCH：后端仍 admin-only
  'ws.connections.write': { roles: ['admin'] },   // 数据连接增删改：后端仍 admin-only
  'ws.connections.test':  { roles: ['admin'] },   // 测试连接=写侧
  'ws.kb.write':          { roles: ['admin'] },   // 知识包导入/删除/绑定：后端仍 admin-only
  // 个人层（L3）
  'self.preferences':     { self: true },
};

export function can(user, perm, { workspaceId } = {}) {
  if (!user) return false;
  const rule = RULES[perm];
  if (!rule) return false;
  if (rule.self) return true;
  if (rule.roles && rule.roles.includes(user.role)) return true;
  if (rule.wsRoles && workspaceId) {
    const wsRole = user.workspace_roles?.[workspaceId];
    return rule.wsRoles.includes(wsRole);
  }
  return false;
}
