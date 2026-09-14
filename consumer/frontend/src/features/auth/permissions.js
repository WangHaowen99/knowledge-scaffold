// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
// 权限点注册表（前端镜像，仅做 UI 门控；后端是唯一强制点）。
// 权限模型：permission = action × resource × scope（全局 / 空间 / 个人）。
// 现阶段角色只有 admin / user；结构上为 workspace 级角色（workspace_admin）预留了 wsRoles 位。
export const ROLES = { ADMIN: 'admin', USER: 'user' };

const RULES = {
  // 全局层（L1）
  'system_config.read': { roles: ['admin', 'user'] },
  'system_config.write': { roles: ['admin'] },
  'workspace.create': { roles: ['admin', 'user'] },
  'workspace.manage': { roles: ['admin'] }, // 全局删除与设默认等集合管理
  'users.manage': { roles: ['admin'] },
  'observability.global': { roles: ['admin'] }, // 全局运行观测只读入口
  // 空间层（L2）——后端仍是唯一强制点；普通用户只能操作服务端确认其可访问的空间。
  'ws.config.read': { roles: ['admin', 'user'] },
  'ws.info.write': { roles: ['admin'], wsRoles: ['admin'] }, // PATCH 改名/描述（owner 可）
  'ws.members.write': { roles: ['admin'], wsRoles: ['admin'] }, // PUT members（owner 可）

  'ws.config.write': { roles: ['admin'] }, // runtime-config PATCH：后端仍 admin-only
  'ws.kb.import': { roles: ['admin'], wsRoles: ['admin'] },
  'ws.kb.activate': { roles: ['admin'], wsRoles: ['admin'] },
  'ws.data_source.write': { roles: ['admin'], wsRoles: ['admin'] },
  'ws.data_source.test': { roles: ['admin'], wsRoles: ['admin'] },
  'ws.kb.write': { roles: ['admin'], wsRoles: ['admin'] }, // 状态、删除、文件编辑等 owner 管理操作
  'ws.observability.write': { roles: ['admin'] },
  // 个人层（L3）
  'self.preferences': { self: true },
  'self.memory': { self: true },
};

export function can(user, perm, { workspaceId } = {}) {
  if (!user) {
    return false;
  }
  const rule = RULES[perm];
  if (!rule) {
    return false;
  }
  if (rule.self) {
    return true;
  }
  if (rule.roles && rule.roles.includes(user.role)) {
    return true;
  }
  if (rule.wsRoles && workspaceId) {
    const wsRole = user.workspace_roles?.[workspaceId];
    return rule.wsRoles.includes(wsRole);
  }
  return false;
}
