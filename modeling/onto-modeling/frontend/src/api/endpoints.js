// 按契约编号组织的接口函数（只列 F1 用到的分组，后续阶段按同样写法补）
const enc = encodeURIComponent;
const P = (id) => `/projects/${enc(id)}`;
const C = (id, cid) => `${P(id)}/conversations/${enc(cid)}`;

export function createApi(client) {
  return {
    // 3.1 客户端配置、引擎
    clientConfig: () => client.get('/client-config'), // C-01
    engines: () => client.get('/engines'), // C-06

    // 3.2 认证与个人
    login: (username, password) => client.post('/auth/login', { username, password }), // A-01
    enterLocal: () => client.post('/auth/local', {}), // A-02
    register: (username, password) => client.post('/auth/register', { username, password }), // A-03
    logout: (refresh_token) => client.post('/auth/logout', refresh_token ? { refresh_token } : {}), // A-05
    me: () => client.get('/me'), // A-07
    modelSettings: () => client.get('/me/model-settings'), // A-09
    notifications: (query) => client.get('/me/notifications', { query }), // A-13
    markNotificationsRead: (body) => client.post('/me/notifications/read', body), // A-14
    userEventsUrl: (q) => client.url('/me/events', q), // A-12

    // 3.3 暂存上传
    upload(file, purpose, projectId) { // U-01
      const fd = new FormData();
      fd.append('file', file);
      fd.append('purpose', purpose);
      if (projectId) fd.append('project_id', projectId);
      return client.post('/uploads', fd);
    },
    deleteUpload: (uploadId) => client.del(`/uploads/${enc(uploadId)}`), // U-03

    // 3.4 项目
    projects: (query) => client.get('/projects', { query }), // P-01
    createProject: (body) => client.post('/projects', body), // P-02
    nameCheck: (name, excludeProjectId) => client.get('/projects/name-check', { query: { name, exclude_project_id: excludeProjectId } }), // P-03
    project: (id) => client.get(P(id)), // P-04
    patchProject: (id, body) => client.patch(P(id), body), // P-05
    deleteProject: (id, confirm_name) => client.del(P(id), { confirm_name }), // P-06
    archiveProject: (id, archived) => client.post(`${P(id)}/archive`, { archived }), // P-07
    cloneProject: (id, body) => client.post(`${P(id)}/clone`, body), // P-08
    transferPackageUrl: (id) => client.withToken(client.url(`${P(id)}/transfer-package`)), // P-09
    inspectImport: (body) => client.post('/projects/import/inspect', body), // P-10
    importProject: (body) => client.post('/projects/import', body), // P-11
    buildMode: (id) => client.get(`${P(id)}/build-mode`), // P-13
    setBuildMode: (id, build_mode) => client.put(`${P(id)}/build-mode`, { build_mode }), // P-13
    projectEventsUrl: (id, q) => client.url(`${P(id)}/events`, q), // P-15
    projectUsage: (id) => client.get(`${P(id)}/usage`), // P-16

    // 3.5 检索
    search: (id, q) => client.get(`${P(id)}/search`, { query: { q } }), // F-07

    // 3.10 变更集
    changeset: (id, cs) => client.get(`${P(id)}/changesets/${enc(cs)}`), // CS-02
    changesetDiff: (id, cs, target) => client.get(`${P(id)}/changesets/${enc(cs)}/diff`, { query: { target } }), // CS-03
    mergeChangeset: (id, cs, expected_head) => client.post(`${P(id)}/changesets/${enc(cs)}/merge`, expected_head ? { expected_head } : {}), // CS-04
    discardChangeset: (id, cs) => client.post(`${P(id)}/changesets/${enc(cs)}/discard`, {}), // CS-05
    scope: (id, target) => client.get(`${P(id)}/scope`, { query: { target } }), // CS-06

    // 3.11 会话
    conversations: (id) => client.get(`${P(id)}/conversations`), // S-01
    createConversation: (id, title) => client.post(`${P(id)}/conversations`, title ? { title } : {}), // S-02
    renameConversation: (id, cid, title) => client.patch(C(id, cid), { title }), // S-03
    deleteConversation: (id, cid) => client.del(C(id, cid)), // S-04
    conversationState: (id, cid) => client.get(C(id, cid)), // S-05
    messages: (id, cid, query) => client.get(`${C(id, cid)}/messages`, { query }), // S-06
    sendMessage: (id, cid, body) => client.post(`${C(id, cid)}/messages`, body), // S-07
    interrupt: (id, cid) => client.post(`${C(id, cid)}/interrupt`, {}), // S-08
    restartConversation: (id, cid) => client.post(`${C(id, cid)}/restart`, {}), // S-09
    replyPermission: (id, cid, rid, body) => client.post(`${C(id, cid)}/permissions/${enc(rid)}`, body), // S-10
    answerQuestion: (id, cid, qid, body) => client.post(`${C(id, cid)}/questions/${enc(qid)}/answer`, body), // S-11
    cancelQueue: (id, cid) => client.post(`${C(id, cid)}/queue/cancel`, {}), // S-12
    conversationEventsUrl: (id, cid, q) => client.url(`${C(id, cid)}/events`, q), // S-13
    retryTurn: (id, cid) => client.post(`${C(id, cid)}/retry`, {}), // S-14
    resumeExec: (id, execId, conversation_id) => client.post(`${P(id)}/execs/${enc(execId)}/resume`, conversation_id ? { conversation_id } : {}), // S-16
    abandonExec: (id, execId) => client.post(`${P(id)}/execs/${enc(execId)}/abandon`, {}), // S-17
    commandCatalog: (id) => client.get(`${P(id)}/commands/catalog`), // S-18
    sendCommand: (id, body) => client.post(`${P(id)}/commands`, body), // S-20

    // 3.12 流程
    flow: (id, conversation_id) => client.get(`${P(id)}/flow`, { query: { conversation_id } }), // FL-01
    suggestions: (id, conversation_id) => client.get(`${P(id)}/flow/suggestions`, { query: { conversation_id } }), // FL-02
    skipNode: (id, node, body) => client.post(`${P(id)}/flow/nodes/${enc(node)}/skip`, body || {}), // FL-03
    restoreNode: (id, node, body) => client.post(`${P(id)}/flow/nodes/${enc(node)}/restore`, body || {}), // FL-04
    startNode: (id, node, body) => client.post(`${P(id)}/flow/nodes/${enc(node)}/start`, body), // FL-05

    // 3.13 待处理（外壳只取计数）
    inbox: (id, query) => client.get(`${P(id)}/inbox`, { query }), // IN-01

    // 3.16 插件
    packs: (query) => client.get('/packs', { query }), // PL-01
    extensions: (id) => client.get(`${P(id)}/extensions`), // PL-12

    // 3.19 证据源
    evidenceSources: (query) => client.get('/evidence/sources', { query }), // EV-01
    resolveAnchors: (id, anchors) => client.post(`${P(id)}/anchors/resolve`, { anchors }), // EV-29
  };
}
