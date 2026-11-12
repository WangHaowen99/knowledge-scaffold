import { ApiError } from './errors.js';
import { withRequestId } from './requestIds.js';

// 按契约编号组织的接口函数（F1～F4 用到的分组，后续阶段按同样写法补）
const enc = encodeURIComponent;
const P = (id) => `/projects/${enc(id)}`;
const C = (id, cid) => `${P(id)}/conversations/${enc(cid)}`;
// 运维管理面（契约 1.1）：请求 {ops_base_url}/api/v1/ops/...
const OPS = { ops: true };

export function createApi(client) {
  return {
    // 3.1 客户端配置、引擎
    // C-01；features.evidence === false 时证据源接口不再探测（EV-01 直接按 501 返回，调用处照旧走失败分支）
    clientConfig: () => client.get('/client-config').then((cfg) => { client.features = cfg?.features || {}; return cfg; }),
    engines: () => client.get('/engines'), // C-06

    // 3.2 认证与个人
    login: (username, password) => client.post('/auth/login', { username, password }), // A-01
    enterLocal: () => client.post('/auth/local', {}), // A-02
    register: (username, password) => client.post('/auth/register', { username, password }), // A-03
    logout: (refresh_token) => client.post('/auth/logout', refresh_token ? { refresh_token } : {}), // A-05
    me: () => client.get('/me'), // A-07
    changePassword: (old_password, new_password) => client.put('/me/password', { old_password, new_password }), // A-06
    preferences: () => client.get('/me/preferences'), // A-08
    savePreferences: (body) => client.put('/me/preferences', body), // A-08
    modelSettings: () => client.get('/me/model-settings'), // A-09
    saveModelSettings: (engine, body) => client.put(`/me/model-settings/${enc(engine)}`, body), // A-10
    clearModelSettings: (engine) => client.del(`/me/model-settings/${enc(engine)}`), // A-11
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
    engine: (id) => client.get(`${P(id)}/engine`), // P-12
    setEngine: (id, body) => client.put(`${P(id)}/engine`, body), // P-12
    buildMode: (id) => client.get(`${P(id)}/build-mode`), // P-13
    setBuildMode: (id, build_mode) => client.put(`${P(id)}/build-mode`, { build_mode }), // P-13
    projectMcpServers: (id) => client.get(`${P(id)}/mcp-servers`), // P-14
    saveProjectMcpServers: (id, body) => client.put(`${P(id)}/mcp-servers`, body), // P-14
    projectEventsUrl: (id, q) => client.url(`${P(id)}/events`, q), // P-15
    projectUsage: (id) => client.get(`${P(id)}/usage`), // P-16

    // 3.5 文件与检索
    fileTree: (id, query) => client.get(`${P(id)}/files/tree`, { query }), // F-01
    fileContent: (id, path) => client.get(`${P(id)}/files/content`, { query: { path } }), // F-02
    saveFileContent: (id, body) => client.put(`${P(id)}/files/content`, body), // F-03
    fileRawUrl: (id, path) => client.withToken(client.url(`${P(id)}/files/raw`, { path })), // F-04
    tablePreview: (id, path, sheet) => client.get(`${P(id)}/files/table-preview`, { query: { path, sheet } }), // F-05
    fileAt: (id, commit, path) => client.get(`${P(id)}/files/at`, { query: { commit, path } }), // F-06
    search: (id, q) => client.get(`${P(id)}/search`, { query: { q } }), // F-07

    // 3.6 资料与资料分析
    resources: (id, query) => client.get(`${P(id)}/resources`, { query }), // R-01
    addResources: (id, body) => client.post(`${P(id)}/resources`, body), // R-02
    setResourcePurpose: (id, body) => client.patch(`${P(id)}/resources/purpose`, body), // R-03
    deleteResource: (id, path) => client.del(`${P(id)}/resources`, undefined, { query: { path } }), // R-04
    resourceReferences: (id, path) => client.get(`${P(id)}/resources/references`, { query: { path } }), // R-05
    resourcePreview: (id, path, around) => client.get(`${P(id)}/resources/preview`, { query: { path, around } }), // R-06
    resourceText: (id, path) => client.get(`${P(id)}/resources/text`, { query: { path } }), // R-13
    resourcePositions: (id, path, query) => client.get(`${P(id)}/resources/positions`, { query: { path, ...query } }), // R-15
    resourceSchemaPreview: (id, path) => client.get(`${P(id)}/resources/schema-preview`, { query: { path } }), // R-16
    reconvertResource: (id, path) => client.post(`${P(id)}/resources/convert`, { path }), // R-14
    materialMap: (id) => client.get(`${P(id)}/materials/map`), // R-07
    materialCandidates: (id, query) => client.get(`${P(id)}/materials/candidates`, { query }), // R-08
    materialVerification: (id) => client.get(`${P(id)}/materials/verification`), // R-09
    promoteMaterialProblem: (id, problemId, body) => client.post(`${P(id)}/materials/problems/${enc(problemId)}/promote`, body || {}), // R-10
    feedback: (id, query) => client.get(`${P(id)}/feedback`, { query }), // R-11
    setFeedbackState: (id, feedbackId, body) => client.patch(`${P(id)}/feedback/${enc(feedbackId)}`, body), // R-12

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

    // 3.8 知识
    knowledge: (id, query) => client.get(`${P(id)}/knowledge`, { query }), // K-01
    knowledgeEntry: (id, kid) => client.get(`${P(id)}/knowledge/${enc(kid)}`), // K-02
    createKnowledge: (id, body) => client.post(`${P(id)}/knowledge`, body), // K-03
    updateKnowledge: (id, kid, body) => client.put(`${P(id)}/knowledge/${enc(kid)}`, body), // K-04
    deleteKnowledge: (id, kid, base_revision) => client.del(`${P(id)}/knowledge/${enc(kid)}`, base_revision ? { base_revision } : undefined), // K-05
    knowledgeConflicts: (id) => client.get(`${P(id)}/knowledge/conflicts`), // K-06
    checkKnowledgeConflicts: (id, body) => client.post(`${P(id)}/knowledge/conflicts`, body || {}), // K-06
    nominationPreview: (id, kid, body) => client.post(`${P(id)}/knowledge/${enc(kid)}/nomination-preview`, body || {}), // K-07
    nominateKnowledge: (id, kid, body) => client.post(`${P(id)}/knowledge/${enc(kid)}/nominations`, body), // K-08

    // 3.9 版本与标签
    versions: (id, query) => client.get(`${P(id)}/versions`, { query }), // V-01
    version: (id, commit) => client.get(`${P(id)}/versions/${enc(commit)}`), // V-02
    versionFiles: (id, commit, query) => client.get(`${P(id)}/versions/${enc(commit)}/files`, { query }), // V-03
    versionFilesUrl: (id, commit, query) => client.withToken(client.url(`${P(id)}/versions/${enc(commit)}/files`, { ...query, download: 1 })), // V-03
    versionDiff: (id, query) => client.get(`${P(id)}/versions/diff`, { query }), // V-04
    revertPreview: (id, commit) => client.get(`${P(id)}/versions/revert-preview`, { query: { commit } }), // V-05
    revertVersion: (id, commit, expectedHead) => client.post(`${P(id)}/versions/revert`, { commit, ...(expectedHead ? { expected_head: expectedHead } : {}) }), // V-06
    tagVersion: (id, commit, label) => client.post(`${P(id)}/versions/tags`, { commit, label }), // V-07
    deleteVersionTag: (id, name) => client.del(`${P(id)}/versions/tags/${enc(name)}`), // V-08
    history: (id, { limit = 50, cursor = '' } = {}) => client.get(`${P(id)}/history`, { query: { limit, cursor } }), // V-09
    hubReleaseDownloadPath: (id, releaseId) => `${P(id)}/hub/releases/${enc(releaseId)}/download`, // HB-06（services.download 用）

    // 3.10 变更集
    changesets: (id, query) => client.get(`${P(id)}/changesets`, { query }), // CS-01
    changeset: (id, cs) => client.get(`${P(id)}/changesets/${enc(cs)}`), // CS-02
    changesetDiff: (id, cs, target) => client.get(`${P(id)}/changesets/${enc(cs)}/diff`, { query: { target } }), // CS-03
    mergeChangeset: (id, cs, expected_head) => client.post(`${P(id)}/changesets/${enc(cs)}/merge`, expected_head ? { expected_head } : {}), // CS-04
    discardChangeset: (id, cs) => client.post(`${P(id)}/changesets/${enc(cs)}/discard`, {}), // CS-05
    scope: (id, target) => client.get(`${P(id)}/scope`, { query: { target } }), // CS-06

    // 3.11 会话
    conversations: (id) => client.get(`${P(id)}/conversations`), // S-01
    // kind 只在建插件对话时传（'plugin'，F17）
    createConversation: (id, title, kind) => client.post(`${P(id)}/conversations`, { ...(title ? { title } : {}), ...(kind ? { kind } : {}) }), // S-02
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
    // 平台代存的下一条命令（运行中发的命令）：取消（S-22）、现在开始（S-23）
    cancelNext: (id, cid) => client.post(`${C(id, cid)}/next/cancel`, {}),
    startNext: (id, cid) => client.post(`${C(id, cid)}/next/start`, {}),
    conversationEventsUrl: (id, cid, q) => client.url(`${C(id, cid)}/events`, q), // S-13
    // extra：建议条发起时带 { from_suggestion: true }（F7，情况变了回 suggestion_stale）
    retryTurn: (id, cid, extra) => client.post(`${C(id, cid)}/retry`, { ...extra }), // S-14
    subagentSteps: (id, cid, taskId, query) => client.get(`${C(id, cid)}/subagents/${enc(taskId)}/steps`, { query }), // S-21
    resumeExec: (id, execId, conversation_id, extra) => client.post(`${P(id)}/execs/${enc(execId)}/resume`, { ...(conversation_id ? { conversation_id } : {}), ...extra }), // S-16
    abandonExec: (id, execId, extra) => client.post(`${P(id)}/execs/${enc(execId)}/abandon`, { ...extra }), // S-17
    commandCatalog: (id) => client.get(`${P(id)}/commands/catalog`), // S-18
    commandTasks: (id, query) => client.get(`${P(id)}/commands/tasks`, { query }), // S-19
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

    // 3.14 增量更新（变更点清单）
    changeList: (id, cl) => client.get(`${P(id)}/change-lists/${enc(cl)}`), // INC-01
    saveChangeList: (id, cl, body) => client.put(`${P(id)}/change-lists/${enc(cl)}`, body), // INC-02
    acceptChangePoints: (id, cl, body) => client.post(`${P(id)}/change-lists/${enc(cl)}/accept`, body), // INC-03
    executeChangeList: (id, cl, body) => client.post(`${P(id)}/change-lists/${enc(cl)}/execute`, body || {}), // INC-04
    changeListImpact: (id, cl) => client.get(`${P(id)}/change-lists/${enc(cl)}/impact`), // INC-05

    // 3.15 报告
    reports: (id, query) => client.get(`${P(id)}/reports`, { query }), // RP-01
    report: (id, reportId) => client.get(`${P(id)}/reports/${enc(reportId)}`), // RP-02
    reportItems: (id, reportId, query) => client.get(`${P(id)}/reports/${enc(reportId)}/items`, { query }), // RP-03
    reportRaw: (id, reportId) => client.get(`${P(id)}/reports/${enc(reportId)}/raw`), // RP-04
    fixReport: (id, reportId, body) => client.post(`${P(id)}/reports/${enc(reportId)}/fix`, body || {}), // RP-05
    recheckReports: (id, body) => client.post(`${P(id)}/reports/recheck`, body || {}), // RP-06

    // 3.16 插件
    packs: (query) => client.get('/packs', { query }), // PL-01
    pack: (name, query) => client.get(`/packs/${enc(name)}`, { query }), // PL-02（project_id、version）
    packCompatibility: (name, version, query) => client.get(`/packs/${enc(name)}/versions/${enc(version)}/compatibility`, { query }), // PL-03
    requestPackInstall: (name, body) => client.post(`/packs/${enc(name)}/install-requests`, body || {}), // PL-04
    projectPacks: (id) => client.get(`${P(id)}/packs`), // PL-05
    enablePack: (id, body) => client.post(`${P(id)}/packs`, body), // PL-06
    upgradePack: (id, name, body) => client.post(`${P(id)}/packs/${enc(name)}/upgrade`, body), // PL-07
    rollbackPack: (id, name, body) => client.post(`${P(id)}/packs/${enc(name)}/rollback`, body), // PL-08
    disablePack: (id, name, body) => client.post(`${P(id)}/packs/${enc(name)}/disable`, body || {}), // PL-09
    // F17 插件草稿（工作台插件开发模式，ADR-0087）
    pluginDrafts: (id) => client.get(`${P(id)}/plugin-drafts`), // PD-01
    pluginDraft: (id, name) => client.get(`${P(id)}/plugin-drafts/${enc(name)}`), // PD-02
    pluginDraftFile: (id, name, path) => client.get(`${P(id)}/plugin-drafts/${enc(name)}/files`, { query: { path } }), // PD-03
    savePluginDraftFile: (id, name, body) => client.put(`${P(id)}/plugin-drafts/${enc(name)}/files`, body), // PD-04
    installPluginDraft: (id, name) => client.post(`${P(id)}/plugin-drafts/${enc(name)}/install`, {}), // PD-05
    cancelPackPending: (id, name) => client.post(`${P(id)}/packs/${enc(name)}/cancel-pending`, {}), // PL-10
    assembly: (id) => client.get(`${P(id)}/assembly`), // PL-11
    extensions: (id) => client.get(`${P(id)}/extensions`), // PL-12
    // PL-13 上传插件包走 services.uploadPack（带进度）；这里是不带进度的版本
    uploadPack(file) { // PL-13
      const fd = new FormData();
      fd.append('file', file);
      return client.post('/admin/packs/uploads', fd);
    },
    packUploads: (query) => client.get('/admin/packs/uploads', { query }), // PL-14
    packUpload: (uploadId) => client.get(`/admin/packs/uploads/${enc(uploadId)}`), // PL-15
    packUploadReportUrl: (uploadId) => client.withToken(client.url(`/admin/packs/uploads/${enc(uploadId)}/report`)), // PL-16
    publishPackUpload: (uploadId, body) => client.post(`/admin/packs/uploads/${enc(uploadId)}/publish`, body), // PL-17
    rejectPackUpload: (uploadId, body) => client.post(`/admin/packs/uploads/${enc(uploadId)}/reject`, body || {}), // PL-18
    installPack: (name, body) => client.post(`/admin/packs/${enc(name)}/install`, body || {}), // PL-19
    // PL-22 上传自己用的包走 services.uploadPack（mine）
    myPackUploads: (query) => client.get('/me/pack-uploads', { query }), // PL-23
    myPackUpload: (uploadId) => client.get(`/me/pack-uploads/${enc(uploadId)}`), // PL-24
    installMyPackUpload: (uploadId) => client.post(`/me/pack-uploads/${enc(uploadId)}/install`, {}), // PL-26
    converters: () => client.get('/converters'), // PL-20
    setConverterDefault: (body) => client.put('/admin/converters/defaults', body), // PL-21

    // 3.17 插件产物
    artifactTypes: (id) => client.get(`${P(id)}/artifact-types`), // AR-01
    artifacts: (id, type, query) => client.get(`${P(id)}/artifacts/${enc(type)}`, { query }), // AR-02
    // AR-03；query.changeset：读这个变更集（助手副本）里的版本，这一轮还没合入时双击产物卡用
    artifact: (id, type, key, query) => client.get(`${P(id)}/artifacts/${enc(type)}/${enc(key)}`, query ? { query } : undefined), // AR-03
    artifactHistory: (id, type, key) => client.get(`${P(id)}/artifacts/${enc(type)}/${enc(key)}/history`), // AR-04
    saveArtifact: (id, type, key, body) => client.put(`${P(id)}/artifacts/${enc(type)}/${enc(key)}`, body), // AR-05
    createArtifact: (id, type, body) => client.post(`${P(id)}/artifacts/${enc(type)}`, body), // AR-06
    deleteArtifact: (id, type, key, base_revision) => client.del(`${P(id)}/artifacts/${enc(type)}/${enc(key)}`, { base_revision }), // AR-07
    validateArtifact: (id, type, key, body) => client.post(`${P(id)}/artifacts/${enc(type)}/${enc(key)}/validate`, body || {}), // AR-08
    artifactSummary: (id, type, query) => client.get(`${P(id)}/artifacts/${enc(type)}/summary`, { query }), // AR-09
    openPackUi: (id, name, editor) => client.post(`${P(id)}/packs/${enc(name)}/ui-sessions`, { editor }), // PL-20：上传包编辑器的 iframe 地址

    // 3.18 脚本与求解契约
    scripts: (id) => client.get(`${P(id)}/scripts`), // SC-01
    scriptVerification: (id, key) => client.get(`${P(id)}/scripts/${enc(key)}/verification`), // SC-02
    sandboxStatus: () => client.get('/sandbox/status'), // SC-03
    solverContract: (id) => client.get(`${P(id)}/solver/contract`), // SV-01
    saveSolverContract: (id, body) => client.put(`${P(id)}/solver/contract`, body), // SV-02
    checkSolverContract: (id, body) => client.post(`${P(id)}/solver/contract/check`, body), // SV-03

    // 3.19 证据源
    evidenceSources: (query) => (client.features?.evidence === false
      ? Promise.reject(new ApiError({ status: 501, code: 'evidence_not_available', message: '这一版没有开放业务系统的连接器' }))
      : client.get('/evidence/sources', { query })), // EV-01
    registerEvidenceSource: (body) => client.post('/admin/evidence/sources', body), // EV-02
    requestEvidenceSource: (body) => client.post('/evidence/source-requests', body), // EV-03
    updateEvidenceSource: (sid, body) => client.patch(`/admin/evidence/sources/${enc(sid)}`, body), // EV-04
    deleteEvidenceSource: (sid) => client.del(`/admin/evidence/sources/${enc(sid)}`), // EV-05
    writeEvidenceSecret: (sid, body) => client.put(`/admin/evidence/sources/${enc(sid)}/secret`, body), // EV-06
    deleteEvidenceSecret: (sid) => client.del(`/admin/evidence/sources/${enc(sid)}/secret`), // EV-07
    testEvidenceSource: (sid, body) => client.post(`/evidence/sources/${enc(sid)}/test`, body || {}), // EV-08
    evidenceAccessLog: (sid, query) => client.get(`/evidence/sources/${enc(sid)}/access-log`, { query }), // EV-09
    evidenceProviders: () => client.get('/evidence/providers'), // EV-10
    evidenceGrants: (id) => client.get(`${P(id)}/evidence/grants`), // EV-11
    setEvidenceGrant: (id, sid, body) => client.put(`${P(id)}/evidence/grants/${enc(sid)}`, body), // EV-11
    evidenceSummary: (id) => client.get(`${P(id)}/evidence/summary`), // EV-12
    previewEvidenceJob: (id, body) => client.post(`${P(id)}/evidence/jobs/preview`, body), // EV-13
    startEvidenceJob: (id, body) => client.post(`${P(id)}/evidence/jobs`, body), // EV-14
    evidenceJobs: (id, query) => client.get(`${P(id)}/evidence/jobs`, { query }), // EV-15
    evidenceJob: (id, jobId) => client.get(`${P(id)}/evidence/jobs/${enc(jobId)}`), // EV-16
    evidenceJobAction: (id, jobId, op, body) => client.post(`${P(id)}/evidence/jobs/${enc(jobId)}/${op}`, body || {}), // EV-17：op = cancel | resume | retry；生产库 retry 带 confirmation_token
    evidenceSnapshots: (id, query) => client.get(`${P(id)}/evidence/snapshots`, { query }), // EV-18
    evidenceSnapshot: (id, snapId) => client.get(`${P(id)}/evidence/snapshots/${enc(snapId)}`), // EV-19
    deleteEvidenceSnapshot: (id, snapId) => client.del(`${P(id)}/evidence/snapshots/${enc(snapId)}`), // EV-20
    evidenceSnapshotDiff: (id, query) => client.get(`${P(id)}/evidence/snapshots/diff`, { query }), // EV-21
    pushSnapshotSuggestions: (id, body) => client.post(`${P(id)}/evidence/snapshots/diff/suggestions`, body), // EV-22
    evidenceSummaryTree: (id, query) => client.get(`${P(id)}/evidence/summary-tree`, { query }), // EV-23
    evidenceSearch: (id, query) => client.get(`${P(id)}/evidence/search`, { query }), // EV-24
    evidenceRecord: (id, anchor, snapshot) => client.get(`${P(id)}/evidence/record`, { query: { anchor, snapshot } }), // EV-25
    evidenceWhereUsed: (id, anchor, prefix) => client.get(`${P(id)}/evidence/where-used`, { query: { anchor, prefix: prefix ? 'true' : undefined } }), // EV-26：prefix 匹配整张表
    evidenceLineage: (id, anchor, snapshot) => client.get(`${P(id)}/evidence/lineage`, { query: { anchor, snapshot } }), // EV-27
    evidenceEr: (id, query) => client.get(`${P(id)}/evidence/er`, { query }), // EV-28
    resolveAnchors: (id, anchors) => client.post(`${P(id)}/anchors/resolve`, { anchors }), // EV-29
    notifyEvidenceAdmin: (sid, body) => client.post(`/evidence/sources/${enc(sid)}/notify-admin`, body || {}), // EV-30
    startEvidenceUpload: (body) => client.post('/evidence/uploads', body), // EV-31
    putEvidenceUploadChunk: (uploadId, index, bytes, o) => client.put(`/evidence/uploads/${enc(uploadId)}/chunks/${index}`, bytes, o), // EV-32：请求体是这一块的字节
    completeEvidenceUpload: (uploadId, body) => client.post(`/evidence/uploads/${enc(uploadId)}/complete`, body), // EV-33
    cancelEvidenceUpload: (uploadId) => client.del(`/evidence/uploads/${enc(uploadId)}`), // EV-34：释放未完成上传的名额

    // 3.20 发布
    publishCheck: (id, query) => client.get(`${P(id)}/publish/check`, { query }), // PB-01
    setObjectDataSource: (id, key, body) => client.put(`${P(id)}/objects/${enc(key)}/data-source`, body), // PB-02
    // PB-03：经 Hub 发布（ADR-0098）；同一提交、同一选项在拿到结果前重试用同一个 request_id
    publishRelease: (id, body = {}) => withRequestId(
      `publish:${id}:${body.commit || ''}:${body.degrade ? 1 : 0}:${body.solver_unverified_acknowledged ? 1 : 0}`,
      (request_id) => client.post(`${P(id)}/releases`, { ...body, request_id }),
    ),
    releases: (id, query) => client.get(`${P(id)}/releases`, { query }), // PB-04
    release: (id, version) => client.get(`${P(id)}/releases/${enc(version)}`), // PB-05
    releaseManifest: (id, version) => client.get(`${P(id)}/releases/${enc(version)}/manifest`), // PB-06
    releaseDownloadUrl: (id, version, variant) => client.withToken(client.url(`${P(id)}/releases/${enc(version)}/download`, variant ? { variant } : undefined)), // PB-07
    // 3.20 知识包版本对比（PC 组）；上传带 X-Compare-Upload-Id，取消可以先于上传请求到达
    uploadCompareInput(id, file, { signal, requestId } = {}) { // PC-01
      const fd = new FormData();
      fd.append('file', file);
      return client.post(`${P(id)}/package-comparisons/inputs`, fd, { signal, headers: requestId ? { 'X-Compare-Upload-Id': requestId } : undefined });
    },
    cancelCompareUpload: (id, requestId) => client.del(`${P(id)}/package-comparisons/uploads/${enc(requestId)}`), // PC-02
    deleteCompareInput: (id, inputId) => client.del(`${P(id)}/package-comparisons/inputs/${enc(inputId)}`), // PC-03
    startComparison: (id, body) => client.post(`${P(id)}/package-comparisons`, body), // PC-04
    comparison: (id, cid) => client.get(`${P(id)}/package-comparisons/${enc(cid)}`), // PC-05
    comparisonFiles: (id, cid, query) => client.get(`${P(id)}/package-comparisons/${enc(cid)}/files`, { query }), // PC-06
    comparisonFileDiff: (id, cid, path) => client.get(`${P(id)}/package-comparisons/${enc(cid)}/file-diff`, { query: { path } }), // PC-07
    deleteComparison: (id, cid) => client.del(`${P(id)}/package-comparisons/${enc(cid)}`), // PC-08

    // 知识包 Hub（ADR-0098，HB-01～HB-09）：都要统一账号登录
    hubPackages: (offset = 0, limit = 20) => client.get('/hub/packages', { query: { offset, limit } }), // HB-01
    hubPackageReleases: (packageId, offset = 0, limit = 20) => client.get(`/hub/packages/${enc(packageId)}/releases`, { query: { offset, limit } }), // HB-02
    importHubPackage: (body) => withRequestId( // HB-04：同一版本、同一名称在拿到结果前重试得到同一个项目
      `hub-import:${body.ontology_id}:${body.release_id}:${body.name}`,
      (request_id) => client.post('/hub/imports', { ...body, request_id }),
    ),
    projectHubReleases: (id, offset = 0, limit = 20) => client.get(`${P(id)}/hub/releases`, { query: { offset, limit } }), // HB-05
    projectHubResourcesStatus: (id) => client.get(`${P(id)}/hub/resources/status`), // HB-07
    // 3.21a 独立评测（ADR-0092）
    evaluationEntry: () => client.get('/evaluation/entry'), // ES-01
    evaluations: (id, { reconcile = false } = {}) => client.get(`${P(id)}/evaluations`, { query: { reconcile: reconcile ? 1 : 0 } }), // ES-02
    evaluationReport: (id, runId) => client.get(`${P(id)}/evaluations/${enc(runId)}/report`), // ES-03
    evaluationSuggestions: (id, runId) => client.get(`${P(id)}/evaluations/${enc(runId)}/suggestions`), // ES-04
    evaluationAnswer: (id, runId, resultId) => client.get(`${P(id)}/evaluations/${enc(runId)}/answers/${enc(resultId)}`), // ES-05
    regenerateEvaluation: (id, runId) => client.post(`${P(id)}/evaluations/${enc(runId)}/regenerate`, {}), // ES-06
    optimizeEvaluation: (id, runId) => client.post(`${P(id)}/evaluations/${enc(runId)}/optimization`, {}), // ES-07
    reportEvaluationFailure: (id, runId, note) => client.post(`${P(id)}/evaluations/${enc(runId)}/modeling-result`, { note }), // ES-08
    legacyEvaluations: (id) => client.get(`${P(id)}/legacy-evaluations`), // ES-09
    legacyEvaluation: (id, stem) => client.get(`${P(id)}/legacy-evaluations/${enc(stem)}`), // ES-10
    legacyEvaluationAnswer: (id, stem, resultId) => client.get(`${P(id)}/legacy-evaluations/${enc(stem)}/answers/${enc(resultId)}`), // ES-11
    optimizeLegacyEvaluation: (id, stem) => client.post(`${P(id)}/legacy-evaluations/${enc(stem)}/optimization`, {}), // ES-12

    // 3.21 三份契约：服务令牌（X-06，设计稿没画入口，见 gaps F4）
    serviceTokens: () => client.get('/admin/service-tokens'), // X-06
    createServiceToken: (body) => client.post('/admin/service-tokens', body), // X-06
    revokeServiceToken: (tokenId) => client.del(`/admin/service-tokens/${enc(tokenId)}`), // X-06

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
    acknowledgeAutobuild: (id, runId) => client.post(`${P(id)}/autobuild/runs/${enc(runId)}/acknowledge`, {}), // AB-19

    // 3.23 对外 MCP
    mcp: (id) => client.get(`${P(id)}/mcp`), // M-01
    mcpTokens: (id) => client.get(`${P(id)}/mcp/tokens`), // M-02
    createMcpToken: (id, body) => client.post(`${P(id)}/mcp/tokens`, body), // M-03
    revokeMcpToken: (id, tokenId) => client.del(`${P(id)}/mcp/tokens/${enc(tokenId)}`), // M-04

    // 3.24 系统设置（admin）
    adminModelSettings: () => client.get('/admin/settings/model'), // SY-01
    saveAdminModelSettings: (engine, body) => client.put(`/admin/settings/model/${enc(engine)}`, body), // SY-02
    setDefaultEngine: (engine) => client.put('/admin/settings/default-engine', { engine }), // SY-03
    setPersonalKeyPolicy: (body) => client.put('/admin/settings/personal-key-policy', body), // SY-04
    contextPresets: () => client.get('/admin/settings/context-presets'), // SY-05
    saveCustomContextPresets: (body) => client.put('/admin/settings/context-presets/custom', body), // SY-06
    disableCustomContextPresets: () => client.post('/admin/settings/context-presets/custom/disable', {}), // SY-07
    matchContextPreset: (query) => client.get('/admin/settings/context-presets/match', { query }), // SY-08
    sessionSettings: () => client.get('/admin/settings/session'), // SY-09
    saveSessionSettings: (body) => client.put('/admin/settings/session', body), // SY-09
    sandboxSettings: () => client.get('/admin/settings/sandbox'), // SY-10
    probeSandbox: () => client.post('/admin/settings/sandbox/probe', {}), // SY-11
    globalMcpServers: () => client.get('/admin/settings/mcp-servers'), // SY-12
    saveGlobalMcpServers: (body) => client.put('/admin/settings/mcp-servers', body), // SY-12
    testMcpServer: (body) => client.post('/admin/settings/mcp-servers/test', body), // SY-13
    configRegistry: (query) => client.get('/admin/config-registry', { query }), // SY-14
    quotas: () => client.get('/admin/quotas'), // SY-15
    saveQuotas: (body) => client.put('/admin/quotas', body), // SY-15
    addQuotaOverride: (body) => client.post('/admin/quotas/overrides', body), // SY-16
    deleteQuotaOverride: (overrideId) => client.del(`/admin/quotas/overrides/${enc(overrideId)}`), // SY-16
    autobuildLimits: () => client.get('/admin/autobuild-limits'), // SY-17
    saveAutobuildLimits: (body) => client.put('/admin/autobuild-limits', body), // SY-17
    pricing: () => client.get('/admin/settings/pricing'), // SY-18（设计稿没画入口）
    savePricing: (body) => client.put('/admin/settings/pricing', body), // SY-18

    // 3.24 管理员：用户、项目所有者、申请审核
    adminUsers: (query) => client.get('/admin/users', { query }), // AD-01
    setUserRole: (username, role) => client.patch(`/admin/users/${enc(username)}`, { role }), // AD-02
    resetUserPassword: (username, body) => client.post(`/admin/users/${enc(username)}/reset-password`, body || {}), // AD-03
    deleteUser: (username, body) => client.del(`/admin/users/${enc(username)}`, body), // AD-04
    userProjects: (username) => client.get(`/admin/users/${enc(username)}/projects`), // AD-05
    unownedProjects: (query) => client.get('/admin/projects/unowned', { query }), // AD-06（设计稿没画入口）
    setProjectOwner: (id, body) => client.put(`/admin/projects/${enc(id)}/owner`, body), // AD-07（设计稿没画入口）
    adminRequests: (query) => client.get('/admin/requests', { query }), // AD-08（设计稿没画入口）
    approveRequest: (requestId, body) => client.post(`/admin/requests/${enc(requestId)}/approve`, body || {}), // AD-09
    rejectRequest: (requestId, body) => client.post(`/admin/requests/${enc(requestId)}/reject`, body || {}), // AD-10

    // 3.25 运维管理面（ops 读写，admin 只读；见 src/state/roles.js）
    ops: {
      overview: (query) => client.get('/overview', { ...OPS, query }), // O-01
      metrics: (query) => client.get('/metrics', { ...OPS, query }), // O-02
      attribution: (query) => client.get('/attribution', { ...OPS, query }), // O-03
      trace: (traceId) => client.get(`/traces/${enc(traceId)}`, OPS), // O-04
      traces: (query) => client.get('/traces', { ...OPS, query }), // O-05（设计稿没画入口）
      logs: (query) => client.get('/logs', { ...OPS, query }), // O-06
      logStreamUrl: (q) => client.url('/logs/stream', q, OPS), // O-07（EO-01）
      alertRules: (query) => client.get('/alerts/rules', { ...OPS, query }), // O-08
      createAlertRule: (body) => client.post('/alerts/rules', body, OPS), // O-09
      updateAlertRule: (ruleId, body) => client.put(`/alerts/rules/${enc(ruleId)}`, body, OPS), // O-10
      deleteAlertRule: (ruleId) => client.del(`/alerts/rules/${enc(ruleId)}`, undefined, OPS), // O-10
      exporters: () => client.get('/exporters', OPS), // O-11
      saveExporters: (body) => client.put('/exporters', body, OPS), // O-12
      health: () => client.get('/health', OPS), // O-13
      probeHealth: () => client.post('/health/probe', {}, OPS), // O-14
      sessions: (query) => client.get('/sessions', { ...OPS, query }), // O-15
      saveSessionConfig: (body) => client.put('/sessions/config', body, OPS), // O-16
      recycleSession: (pid, body) => client.post(`/sessions/${enc(pid)}/recycle`, body || {}, OPS), // O-17
      disconnectSession: (pid, body) => client.post(`/sessions/${enc(pid)}/disconnect`, body, OPS), // O-18
      drainSessions: (body) => client.post('/sessions/drain', body || {}, OPS), // O-19
      maintenance: () => client.get('/maintenance', OPS), // O-20
      enterMaintenance: (body) => client.post('/maintenance', body, OPS), // O-21
      advanceMaintenance: (body) => client.post('/maintenance/advance', body || {}, OPS), // O-22
      forceDisconnectMaintenance: (body) => client.post('/maintenance/force-disconnect', body || {}, OPS), // O-23
      exitMaintenance: () => client.del('/maintenance', undefined, OPS), // O-24
      packs: (query) => client.get('/packs', { ...OPS, query }), // O-25
      setPackRollout: (name, version, body) => client.put(`/packs/${enc(name)}/${enc(version)}/rollout`, body, OPS), // O-26
      rollbackPackChannel: (name, body) => client.post(`/packs/${enc(name)}/rollback`, body, OPS), // O-27
      disablePackVersion: (name, version, body) => client.post(`/packs/${enc(name)}/${enc(version)}/disable`, body || {}, OPS), // O-28
      packBreaker: (name, version, body) => client.post(`/packs/${enc(name)}/${enc(version)}/breaker`, body, OPS), // O-29
      coreDefault: () => client.get('/core-default', OPS), // O-30
      promoteCore: (body) => client.post('/core-default/promote', body, OPS), // O-31
      rollbackCore: (body) => client.post('/core-default/rollback', body, OPS), // O-32
      packRepoBackups: () => client.get('/pack-repo/backups', OPS), // O-33
      backupPackRepo: () => client.post('/pack-repo/backups', {}, OPS), // O-34
      startupReport: () => client.get('/startup-report', OPS), // O-35
      backups: () => client.get('/backups', OPS), // O-36
      backupNow: (body) => client.post('/backups', body || {}, OPS), // O-37
      startDrill: (body) => client.post('/backups/drills', body || {}, OPS), // O-38
      drills: () => client.get('/backups/drills', OPS), // O-39
      tasks: (query) => client.get('/tasks', { ...OPS, query }), // O-40
      taskAction: (taskId, op) => client.post(`/tasks/${enc(taskId)}/${op}`, {}, OPS), // O-41：op = cancel | retry | resume
      versions: () => client.get('/versions', OPS), // O-42
      retention: () => client.get('/retention', OPS), // O-43
      saveRetention: (body) => client.put('/retention', body, OPS), // O-44
      createDiagnostics: (body) => client.post('/diagnostics', body || {}, OPS), // O-45
      diagnostics: () => client.get('/diagnostics', OPS), // O-46
      diagnosticsDownloadUrl: (id) => client.withToken(client.url(`/diagnostics/${enc(id)}/download`, undefined, OPS)), // O-47
      audit: (query) => client.get('/audit', { ...OPS, query }), // O-48
      auditExportUrl: (query) => client.withToken(client.url('/audit/export', query, OPS)), // O-49
      errors: (query) => client.get('/errors', { ...OPS, query }), // O-50
      error: (errorId) => client.get(`/errors/${enc(errorId)}`, OPS), // O-51
      trash: (query) => client.get('/trash', { ...OPS, query }), // O-52
      restoreTrash: (entry, body) => client.post(`/trash/${enc(entry)}/restore`, body || {}, OPS), // O-53
      purgeTrash: (entry) => client.del(`/trash/${enc(entry)}`, undefined, OPS), // O-54
    },
  };
}
