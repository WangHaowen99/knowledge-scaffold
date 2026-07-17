export const CONFIG_CENTER_SCOPES = {
  global: {
    lv: 'L1', badge: 'sb-l1', label: '全局配置',
    hint: '平台级配置，影响所有工作空间；仅平台管理员可修改，其他人可查看',
    sections: [
      { id: 'llm', name: '模型池' },
      { id: 'web_search', name: '联网搜索' },
      { id: 'platform', name: '平台接入' },
      { id: 'agent', name: 'Agent 全局参数' },
      { id: 'wsmgmt', name: '工作空间管理' },
      { id: 'users', name: '用户与授权' },
    ],
  },
  workspace: {
    lv: 'L2', badge: 'sb-l2', label: '工作空间',
    hint: '当前工作空间的资源与运行设置；空间管理员可修改，成员可查看',
    sections: [
      { id: 'info', name: '空间信息' },
      { id: 'members', name: '成员' },
      { id: 'model', name: '模型选用' },
      { id: 'runtime', name: '空间运行参数' },
      { id: 'wsobs', name: '空间观测' },
      { id: 'mcp', name: 'MCP 接入' },
    ],
  },
  personal: {
    lv: 'L3', badge: 'sb-l3', label: '个人设置',
    hint: '仅对你自己生效的个人偏好，随账号同步',
    sections: [
      { id: 'prefs', name: '偏好' },
      { id: 'memory', name: '长期记忆' },
      { id: 'account', name: '账号安全' },
    ],
  },
};
