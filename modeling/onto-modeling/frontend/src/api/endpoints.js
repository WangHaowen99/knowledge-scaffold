// 按契约编号组织的接口函数（F1、F2 用到的分组，后续阶段按同样写法补）
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

    // 3.5 文件与检索
    fileContent: (id, path) => client.get(`${P(id)}/files/content`, { query: { path } }), // F-02
    saveFileContent: (id, body) => client.put(`${P(id)}/files/content`, body), // F-03
    fileRawUrl: (id, path) => client.withToken(client.url(`${P(id)}/files/raw`, { path })), // F-04
    search: (id, q) => client.get(`${P(id)}/search`, { query: { q } }), // F-07

    // 3.6 资料与资料分析
    resources: (id, query) => client.get(`${P(id)}/resources`, { query }), // R-01
    addResources: (id, body) => client.post(`${P(id)}/resources`, body), // R-02
    setResourcePurpose: (id, body) => client.patch(`${P(id)}/resources/purpose`, body), // R-03
    resourcePreview: (id, path, around) => client.get(`${P(id)}/resources/preview`, { query: { path, around } }), // R-06
    materialMap: (id) => client.get(`${P(id)}/materials/map`), // R-07
    materialCandidates: (id, query) => client.get(`${P(id)}/materials/candidates`, { query }), // R-08
    materialVerification: (id) => client.get(`${P(id)}/materials/verification`), // R-09
    promoteMaterialProblem: (id, problemId, body) => client.post(`${P(id)}/materials/problems/${enc(problemId)}/promote`, body || {}), // R-10

    // 3.7 Studio 条目
    studioStats: (id) => client.get(`${P(id)}/studio/stats`), // ST-01
    studioGraph: (id, query) => client.get(`${P(id)}/studio/graph`, { query }), // ST-02
    studioEntries: (id, query) => client.get(`${P(id)}/studio/entries`, { query }), // ST-03
    studioEntry: (id, entryId) => client.get(`${P(id)}/studio/entries/${enc(entryId)}`), // ST-04
    studioEntryFlow: (id, entryId, query) => client.get(`${P(id)}/studio/entries/${enc(entryId)}/flow`, { query }), // ST-05
    studioEntryEvidence: (id, entryId) => client.get(`${P(id)}/studio/entries/${enc(entryId)}/evidence`), // ST-06
    studioDraft: (id, entryId) => client.get(`${P(id)}/studio/entries/${enc(entryId)}/draft`), // ST-07
    saveStudioDraft: (id, entryId, body) => client.put(`${P(id)}/studio/entries/${enc(entryId)}/draft`, body), // ST-07
    discardStudioDraft: (id, entryId) => client.del(`${P(id)}/studio/entries/${enc(entryId)}/draft`), // ST-07
    submitStudioEntry: (id, entryId, body) => client.post(`${P(id)}/studio/entries/${enc(entryId)}/submit`, body || {}), // ST-08
    createStudioEntry: (id, body) => client.post(`${P(id)}/studio/entries`, body), // ST-09
    deleteStudioEntry: (id, entryId, base_revision) => client.del(`${P(id)}/studio/entries/${enc(entryId)}`, { base_revision }), // ST-10

    // 3.9 版本（F2 只用到详情与对比）
    version: (id, commit) => client.get(`${P(id)}/versions/${enc(commit)}`), // V-02
    versionDiff: (id, query) => client.get(`${P(id)}/versions/diff`, { query }), // V-04

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
    blueprint: (id) => client.get(`${P(id)}/blueprint`), // BP-01
    saveBlueprint: (id, body) => client.put(`${P(id)}/blueprint`, body), // BP-02
    confirmBlueprint: (id, body) => client.post(`${P(id)}/blueprint/confirm`, body), // BP-03

    // 3.13 待处理
    inbox: (id, query) => client.get(`${P(id)}/inbox`, { query }), // IN-01
    inboxItem: (id, itemId) => client.get(`${P(id)}/inbox/${enc(itemId)}`), // IN-02
    inboxAction: (id, itemId, body) => client.post(`${P(id)}/inbox/${enc(itemId)}/actions`, body), // IN-03
    inboxBatch: (id, body) => client.post(`${P(id)}/inbox/batch`, body), // IN-04

    // 3.15 报告
    reports: (id, query) => client.get(`${P(id)}/reports`, { query }), // RP-01
    report: (id, reportId) => client.get(`${P(id)}/reports/${enc(reportId)}`), // RP-02
    reportItems: (id, reportId, query) => client.get(`${P(id)}/reports/${enc(reportId)}/items`, { query }), // RP-03
    reportRaw: (id, reportId) => client.get(`${P(id)}/reports/${enc(reportId)}/raw`), // RP-04
    fixReport: (id, reportId, body) => client.post(`${P(id)}/reports/${enc(reportId)}/fix`, body || {}), // RP-05
    recheckReports: (id, body) => client.post(`${P(id)}/reports/recheck`, body || {}), // RP-06

    // 3.16 插件
    packs: (query) => client.get('/packs', { query }), // PL-01
    extensions: (id) => client.get(`${P(id)}/extensions`), // PL-12

    // 3.17 插件产物
    artifactTypes: (id) => client.get(`${P(id)}/artifact-types`), // AR-01
    artifacts: (id, type, query) => client.get(`${P(id)}/artifacts/${enc(type)}`, { query }), // AR-02
    artifact: (id, type, key) => client.get(`${P(id)}/artifacts/${enc(type)}/${enc(key)}`), // AR-03
    artifactHistory: (id, type, key) => client.get(`${P(id)}/artifacts/${enc(type)}/${enc(key)}/history`), // AR-04
    saveArtifact: (id, type, key, body) => client.put(`${P(id)}/artifacts/${enc(type)}/${enc(key)}`, body), // AR-05
    createArtifact: (id, type, body) => client.post(`${P(id)}/artifacts/${enc(type)}`, body), // AR-06
    deleteArtifact: (id, type, key, base_revision) => client.del(`${P(id)}/artifacts/${enc(type)}/${enc(key)}`, { base_revision }), // AR-07
    validateArtifact: (id, type, key, body) => client.post(`${P(id)}/artifacts/${enc(type)}/${enc(key)}/validate`, body || {}), // AR-08
    artifactSummary: (id, type) => client.get(`${P(id)}/artifacts/${enc(type)}/summary`), // AR-09

    // 3.19 证据源
    evidenceSources: (query) => client.get('/evidence/sources', { query }), // EV-01
    evidenceWhereUsed: (id, anchor) => client.get(`${P(id)}/evidence/where-used`, { query: { anchor } }), // EV-26
    resolveAnchors: (id, anchors) => client.post(`${P(id)}/anchors/resolve`, { anchors }), // EV-29

    // 3.22 自主构建
    autobuildPolicy: (id) => client.get(`${P(id)}/autobuild/policy`), // AB-01
    saveAutobuildPolicy: (id, body) => client.put(`${P(id)}/autobuild/policy`, body), // AB-02
    previewAutobuild: (id, body) => client.post(`${P(id)}/autobuild/runs/preview`, body || {}), // AB-03
    startAutobuild: (id, body) => client.post(`${P(id)}/autobuild/runs`, body), // AB-04
    autobuildRun: (id, runId) => client.get(`${P(id)}/autobuild/runs/${enc(runId)}`), // AB-05
    pauseAutobuild: (id, runId) => client.post(`${P(id)}/autobuild/runs/${enc(runId)}/pause`, {}), // AB-06
    stopAutobuildAtSafePoint: (id, runId) => client.post(`${P(id)}/autobuild/runs/${enc(runId)}/stop-at-safe-point`, {}), // AB-07
    convertAutobuildToL2: (id, runId) => client.post(`${P(id)}/autobuild/runs/${enc(runId)}/convert-to-l2`, {}), // AB-08
    resumeAutobuild: (id, runId, body) => client.post(`${P(id)}/autobuild/runs/${enc(runId)}/resume`, body || {}), // AB-09
    autobuildLedger: (id, runId, query) => client.get(`${P(id)}/autobuild/runs/${enc(runId)}/ledger`, { query }), // AB-10
    acceptLedgerEntry: (id, entryId) => client.post(`${P(id)}/autobuild/ledger/${enc(entryId)}/accept`, {}), // AB-11
    rejudgeLedgerEntry: (id, entryId, body) => client.post(`${P(id)}/autobuild/ledger/${enc(entryId)}/rejudge`, body), // AB-12
    acceptHighConfidence: (id, runId) => client.post(`${P(id)}/autobuild/runs/${enc(runId)}/ledger/accept-high-confidence`, {}), // AB-13
    autobuildSuspensions: (id, runId) => client.get(`${P(id)}/autobuild/runs/${enc(runId)}/suspensions`), // AB-14
    answerSuspension: (id, suspensionId, body) => client.post(`${P(id)}/autobuild/suspensions/${enc(suspensionId)}/answer`, body), // AB-15
    autobuildReport: (id, runId) => client.get(`${P(id)}/autobuild/runs/${enc(runId)}/report`), // AB-16
    revertAutobuildSlice: (id, runId, sliceId, body) => client.post(`${P(id)}/autobuild/runs/${enc(runId)}/slices/${enc(sliceId)}/revert`, body || {}), // AB-17
    autobuildRuns: (id, query) => client.get(`${P(id)}/autobuild/runs`, { query }), // AB-18
  };
}
