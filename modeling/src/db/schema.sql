-- 建模工具项目库（SQLite）。业务样例数据不在此库（见评测 MySQL）。
CREATE TABLE IF NOT EXISTS projects (
  id            TEXT PRIMARY KEY,
  owner         TEXT NOT NULL,
  external_id   TEXT,
  kind          TEXT NOT NULL DEFAULT 'modeling'
                CHECK (kind IN ('modeling', 'package')),
  runtime       TEXT NOT NULL DEFAULT 'legacy'
                CHECK (runtime IN ('legacy', 'agent')),
  name          TEXT NOT NULL,
  domain        TEXT NOT NULL DEFAULT '',
  background    TEXT NOT NULL DEFAULT '',
  target_users  TEXT,
  glossary_prefs TEXT,
  sensitivity_level TEXT NOT NULL DEFAULT 'standard',
  -- 当前项目是否由用户明确要求先完成产品/规则大清单台账。
  -- 该标志独立于全局 advisory/enforce 配置，作为计划和写入门禁的事实来源。
  product_ledger_required INTEGER NOT NULL DEFAULT 0 CHECK (product_ledger_required IN (0, 1)),
  workflow_profile_json TEXT NOT NULL DEFAULT '{"enabledSteps":[],"requiredSteps":["scenario","data_source"],"releaseRequested":true,"externalResearch":false}',
  stage         TEXT NOT NULL DEFAULT 'project_setup',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_members (
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  username      TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('viewer', 'modeler')),
  added_by      TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, username)
);

CREATE INDEX IF NOT EXISTS idx_project_members_username
  ON project_members(username, updated_at DESC);

-- 唯一的可恢复建模任务图。由项目创建或会话推进入口显式初始化。
CREATE TABLE IF NOT EXISTS ai_construction_graphs (
  project_id    TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  revision      INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'completed', 'superseded')),
  source        TEXT NOT NULL DEFAULT 'new'
                CHECK (source IN ('new', 'package_import')),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_construction_milestones (
  project_id    TEXT NOT NULL REFERENCES ai_construction_graphs(project_id) ON DELETE CASCADE,
  milestone_id  TEXT NOT NULL,
  ordinal       INTEGER NOT NULL,
  title         TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'active', 'confirmed', 'stale')),
  confirmed_revision_oid TEXT,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, milestone_id),
  UNIQUE (project_id, ordinal)
);

CREATE TABLE IF NOT EXISTS ai_construction_tasks (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES ai_construction_graphs(project_id) ON DELETE CASCADE,
  task_kind     TEXT NOT NULL CHECK (task_kind IN (
                  'material_analysis', 'package_baseline_review', 'modeling_brief',
                  'process_interview', 'process_diagram', 'process_review',
                  'operation_extraction', 'knowledge_extraction', 'script_decision',
                  'script_generation', 'package_validation', 'package_repair',
                  'evaluation_repair', 'completion_review',
                  -- 资料变更后的定向复核：只列出真正受影响的内容，不再整段推倒重来
                  'material_change_review',
                  -- 增量补充：知识包建完之后补新资料，只处理增量而不重走全流程
                  'incremental_intake'
                )),
  milestone_id  TEXT NOT NULL,
  ordinal       INTEGER NOT NULL,
  title         TEXT NOT NULL,
  goal          TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                  'pending', 'ready', 'running', 'waiting_user', 'waiting_confirmation',
                  'completed', 'failed', 'skipped', 'stale', 'cancelled'
                )),
  required      INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
  reason        TEXT,
  input_revision_oid TEXT,
  output_revision_oid TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  FOREIGN KEY (project_id, milestone_id)
    REFERENCES ai_construction_milestones(project_id, milestone_id) ON DELETE CASCADE,
  UNIQUE (project_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_ai_construction_tasks_project
  ON ai_construction_tasks(project_id, ordinal);

CREATE TABLE IF NOT EXISTS ai_construction_task_dependencies (
  project_id       TEXT NOT NULL REFERENCES ai_construction_graphs(project_id) ON DELETE CASCADE,
  task_id          TEXT NOT NULL REFERENCES ai_construction_tasks(id) ON DELETE CASCADE,
  depends_on_task_id TEXT NOT NULL REFERENCES ai_construction_tasks(id) ON DELETE CASCADE,
  PRIMARY KEY (project_id, task_id, depends_on_task_id),
  CHECK (task_id <> depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS ai_construction_confirmations (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES ai_construction_graphs(project_id) ON DELETE CASCADE,
  milestone_id          TEXT NOT NULL,
  graph_revision        INTEGER NOT NULL,
  package_revision_oid  TEXT NOT NULL,
  actor_username        TEXT NOT NULL,
  actor_role            TEXT NOT NULL CHECK (actor_role IN (
                          'project_owner', 'modeler', 'domain_expert', 'validator'
                        )),
  note                  TEXT,
  invalidated_at        TEXT,
  invalidated_by_rollback_id TEXT,
  created_at            TEXT NOT NULL,
  FOREIGN KEY (project_id, milestone_id)
    REFERENCES ai_construction_milestones(project_id, milestone_id) ON DELETE CASCADE,
  UNIQUE (project_id, milestone_id, graph_revision)
);
CREATE INDEX IF NOT EXISTS idx_ai_construction_confirmations_project
  ON ai_construction_confirmations(project_id, created_at);

-- 阶段回退需要同时恢复 Git 知识包和 SQLite 任务图。先持久化意图，Git 恢复成功后
-- 再原子收敛任务图；进程若在两步之间退出，同一个 request_id 可以安全续做。
CREATE TABLE IF NOT EXISTS ai_construction_milestone_rollbacks (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES ai_construction_graphs(project_id) ON DELETE CASCADE,
  request_id            TEXT NOT NULL,
  target_milestone_id   TEXT NOT NULL,
  graph_revision_before INTEGER NOT NULL,
  graph_revision_after  INTEGER,
  base_package_revision_oid TEXT NOT NULL,
  current_package_revision_oid TEXT NOT NULL,
  restored_package_revision_oid TEXT,
  actor_username        TEXT NOT NULL,
  reason                TEXT NOT NULL,
  impact_json           TEXT NOT NULL,
  response_json         TEXT,
  status                TEXT NOT NULL DEFAULT 'prepared'
                        CHECK (status IN ('prepared', 'package_restored', 'completed', 'failed')),
  error                 TEXT,
  created_at            TEXT NOT NULL,
  completed_at          TEXT,
  UNIQUE (project_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_construction_milestone_rollbacks_project
  ON ai_construction_milestone_rollbacks(project_id, created_at);

CREATE TABLE IF NOT EXISTS ai_construction_task_adjustments (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES ai_construction_graphs(project_id) ON DELETE CASCADE,
  task_id               TEXT NOT NULL,
  action                TEXT NOT NULL CHECK (action IN ('skip', 'resume', 'defer', 'redo')),
  graph_revision_before INTEGER NOT NULL,
  graph_revision_after  INTEGER NOT NULL,
  actor_username        TEXT NOT NULL,
  reason                TEXT NOT NULL,
  impact_json           TEXT NOT NULL,
  created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_construction_task_adjustments_project
  ON ai_construction_task_adjustments(project_id, created_at);

CREATE TABLE IF NOT EXISTS ai_construction_task_edits (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES ai_construction_graphs(project_id) ON DELETE CASCADE,
  action                TEXT NOT NULL CHECK (action IN ('add', 'reorder', 'split', 'merge')),
  edit_json             TEXT NOT NULL,
  graph_revision_before INTEGER NOT NULL,
  graph_revision_after  INTEGER NOT NULL,
  actor_username        TEXT NOT NULL,
  reason                TEXT NOT NULL,
  impact_json           TEXT NOT NULL,
  created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_construction_task_edits_project
  ON ai_construction_task_edits(project_id, created_at);

CREATE TABLE IF NOT EXISTS ai_construction_messages (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  task_id       TEXT REFERENCES ai_construction_tasks(id) ON DELETE SET NULL,
  message_key   TEXT,
  seq           INTEGER NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('assistant', 'user', 'system')),
  blocks_json   TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  UNIQUE (project_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ai_construction_messages_project
  ON ai_construction_messages(project_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_construction_messages_key
  ON ai_construction_messages(project_id, message_key) WHERE message_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS documents (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_key    TEXT,
  revision_no   INTEGER NOT NULL DEFAULT 1,
  filename      TEXT NOT NULL,
  kind          TEXT NOT NULL,
  purpose       TEXT NOT NULL DEFAULT 'auto'
                  CHECK (purpose IN ('auto', 'goal', 'schema', 'evidence', 'term')),
  reading_mode  TEXT NOT NULL DEFAULT 'on_demand'
                  CHECK (reading_mode IN ('on_demand', 'full')),
  directive_revision INTEGER NOT NULL DEFAULT 0,
  parse_status  TEXT NOT NULL DEFAULT 'pending',
  parse_error   TEXT,
  text_content  TEXT,
  tables_json   TEXT,               -- DataDictTable[] JSON（excel）
  raw_content   BLOB,               -- 新上传资料保留原始字节，支持可追溯下载与复用
  content_hash  TEXT NOT NULL DEFAULT '',
  semantic_hash TEXT NOT NULL DEFAULT '',
  lifecycle_status TEXT NOT NULL DEFAULT 'active'
                  CHECK (lifecycle_status IN ('active', 'superseded', 'deleted')),
  supersedes_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
  created_at    TEXT NOT NULL,
  retired_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);
CREATE INDEX IF NOT EXISTS idx_documents_source_history
  ON documents(project_id, source_key, revision_no);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_active_source
  ON documents(project_id, source_key) WHERE lifecycle_status = 'active';

-- 产品/规则大清单的逐项对账台账。它独立于知识包候选文件，先以 advisory
-- 方式记录确定性校验结果，部署切换 enforce 后才参与批量生成门禁。
CREATE TABLE IF NOT EXISTS product_ledger_snapshots (
  project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_revision  TEXT NOT NULL,
  scope            TEXT NOT NULL,
  required         INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
  ledger_json      TEXT NOT NULL,
  validation_json  TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (project_id, source_revision)
);
CREATE INDEX IF NOT EXISTS idx_product_ledger_project
  ON product_ledger_snapshots(project_id, updated_at DESC);

-- v2 台账按范围和真实依赖指纹保存历史；ruleRefs 可被多个条目共享。
CREATE TABLE IF NOT EXISTS product_ledger_v2_snapshots (
  project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scope               TEXT NOT NULL,
  dependency_fingerprint TEXT NOT NULL,
  source_revision     TEXT NOT NULL,
  required            INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
  ledger_json         TEXT NOT NULL,
  validation_json     TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  PRIMARY KEY (project_id, scope, dependency_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_product_ledger_v2_project
  ON product_ledger_v2_snapshots(project_id, scope, updated_at DESC);

-- 可恢复的产品台账草稿；条目按批次保存，支持幂等重放和服务重启后续跑。
CREATE TABLE IF NOT EXISTS product_ledger_drafts (
  project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  ledger_id        TEXT NOT NULL,
  source_revision  TEXT NOT NULL,
  analysis_revision TEXT,
  -- 建立草稿时来源分析的资料集指纹；完成时据此核对来源是否被换掉。
  source_fingerprint TEXT,
  scope            TEXT NOT NULL,
  raw_count        INTEGER,
  canonical_count  INTEGER,
  merged_group_count INTEGER,
  status           TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'finalized', 'needs_review', 'stale')),
  next_batch_no    INTEGER NOT NULL DEFAULT 1,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (project_id, ledger_id)
);
CREATE TABLE IF NOT EXISTS product_ledger_batches (
  project_id       TEXT NOT NULL,
  ledger_id        TEXT NOT NULL,
  batch_no         INTEGER NOT NULL,
  request_hash     TEXT NOT NULL,
  source_cursor    TEXT,
  entries_json     TEXT NOT NULL,
  entry_count      INTEGER NOT NULL,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (project_id, ledger_id, batch_no),
  UNIQUE (project_id, ledger_id, request_hash),
  FOREIGN KEY (project_id, ledger_id)
    REFERENCES product_ledger_drafts(project_id, ledger_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_product_ledger_batches_progress
  ON product_ledger_batches(project_id, ledger_id, batch_no);

-- 资料用途建议与用户确认分离；表格额外保存逐 Sheet 处置。审查绑定不可变资料修订。
CREATE TABLE IF NOT EXISTS material_purpose_reviews (
  document_id             TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  content_hash            TEXT NOT NULL,
  status                  TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'confirmed')),
  suggested_purpose       TEXT NOT NULL
                          CHECK (suggested_purpose IN ('auto', 'goal', 'schema', 'evidence', 'term')),
  sheet_suggestions_json  TEXT NOT NULL DEFAULT '[]',
  suggested_by            TEXT NOT NULL DEFAULT 'system'
                          CHECK (suggested_by IN ('system', 'agent', 'user')),
  confirmed_purpose       TEXT
                          CHECK (confirmed_purpose IS NULL OR confirmed_purpose IN ('goal', 'schema', 'evidence', 'term')),
  sheet_decisions_json    TEXT NOT NULL DEFAULT '[]',
  confirmed_by            TEXT,
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL,
  confirmed_at            TEXT
);
CREATE INDEX IF NOT EXISTS idx_material_purpose_reviews_project
  ON material_purpose_reviews(project_id, status, updated_at);

CREATE TABLE IF NOT EXISTS material_revision_impacts (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_key        TEXT NOT NULL,
  previous_document_id TEXT,
  current_document_id  TEXT,
  change_kind       TEXT NOT NULL CHECK (change_kind IN ('added', 'replaced', 'deleted', 'format_only')),
  semantic_changed  INTEGER NOT NULL CHECK (semantic_changed IN (0, 1)),
  affected_json     TEXT NOT NULL DEFAULT '{}',
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_material_revision_impacts_project
  ON material_revision_impacts(project_id, created_at);

-- modeling core 到新 Agent 运行时的可靠事件 outbox。这里只记录上传/解析事实与
-- 投递状态，不保存业务阶段；project_agent_state 仍是唯一业务状态源。
CREATE TABLE IF NOT EXISTS agent_project_event_outbox (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  event_type    TEXT NOT NULL CHECK (event_type IN ('material_parse', 'studio_review', 'fusion_review', 'package_import', 'evaluation_terminal')),
  payload_json  TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_project_event_outbox_pending
  ON agent_project_event_outbox(project_id, delivered_at, created_at);

CREATE TABLE IF NOT EXISTS project_mcp_endpoints (
  project_id    TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  endpoint_id   TEXT NOT NULL UNIQUE,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_mcp_tokens (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  token_name    TEXT NOT NULL,
  subject_username TEXT NOT NULL,
  issued_by     TEXT NOT NULL,
  scopes_json   TEXT NOT NULL,
  expires_at    TEXT,
  created_at    TEXT NOT NULL,
  last_used_at  TEXT,
  revoked_at    TEXT,
  revoked_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_project_mcp_tokens_project
  ON project_mcp_tokens(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_mcp_tokens_subject
  ON project_mcp_tokens(subject_username, project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS modeling_mcp_runtime (
  singleton_id  INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  instance_id   TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('running', 'stopped')),
  started_at    TEXT NOT NULL,
  heartbeat_at  TEXT NOT NULL,
  detail        TEXT
);

CREATE TABLE IF NOT EXISTS wf_nodes (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type          TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',
  ordinal       INTEGER NOT NULL,
  title         TEXT NOT NULL,
  reason        TEXT,               -- 动态节点插入理由
  agent_messages TEXT,              -- pi state.messages 快照 JSON（可恢复）
  output_json   TEXT,               -- 节点产物 JSON
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wf_nodes_project ON wf_nodes(project_id, ordinal);

CREATE TABLE IF NOT EXISTS workflow_step_progress (
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  step_id       TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('running', 'waiting_answer', 'waiting_review', 'completed', 'skipped', 'stale', 'failed')),
  output_json   TEXT NOT NULL DEFAULT '{}',
  revision_oid  TEXT,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, step_id)
);

CREATE TABLE IF NOT EXISTS workflow_trace_events (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  node_id       TEXT NOT NULL REFERENCES wf_nodes(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  phase         TEXT NOT NULL,
  kind          TEXT NOT NULL,
  status        TEXT NOT NULL,
  title         TEXT NOT NULL,
  summary       TEXT,
  details_json  TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE(node_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_trace_node ON workflow_trace_events(node_id, seq);

-- ---- 统一建模运行 / 待办交互 / 项目事件 ----

CREATE TABLE IF NOT EXISTS authoring_runs (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  intent          TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('queued', 'running', 'waiting', 'completed', 'failed', 'cancelled')),
  base_revision   TEXT,
  state_json      TEXT NOT NULL DEFAULT '{}',
  result_json     TEXT,
  error           TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_authoring_runs_project
  ON authoring_runs(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_authoring_runs_active
  ON authoring_runs(project_id, status, updated_at);

CREATE TABLE IF NOT EXISTS authoring_interactions (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id          TEXT NOT NULL REFERENCES authoring_runs(id) ON DELETE CASCADE,
  type            TEXT NOT NULL CHECK (type IN (
                    'question_batch', 'change_plan', 'decision', 'artifact_review',
                    'finding_review', 'knowledge_conflict', 'flowchart_review', 'scenario_review'
                  )),
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'answered', 'cancelled')),
  base_revision   TEXT,
  payload_json    TEXT NOT NULL,
  response_id     TEXT,
  response_json   TEXT,
  created_at      TEXT NOT NULL,
  responded_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_authoring_interactions_pending
  ON authoring_interactions(project_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_authoring_interactions_run
  ON authoring_interactions(run_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_authoring_interactions_response
  ON authoring_interactions(project_id, response_id)
  WHERE response_id IS NOT NULL;

-- 知识结构生成批次检查点。只记录生成阶段产物，不保存或改写上游资料分析结果。
-- 同一生成计划恢复时复用已完成批次，避免额度、网络或服务重启后从对象阶段重跑。
CREATE TABLE IF NOT EXISTS generation_batch_checkpoints (
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id        TEXT NOT NULL REFERENCES authoring_runs(id) ON DELETE CASCADE,
  plan_hash     TEXT NOT NULL,
  phase         TEXT NOT NULL,
  batch_kind    TEXT NOT NULL CHECK (batch_kind IN ('object', 'knowledge', 'operation')),
  batch_index   INTEGER NOT NULL CHECK (batch_index > 0),
  dependency_hash TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'skipped')),
  output_json   TEXT NOT NULL DEFAULT '{}',
  issues_json   TEXT NOT NULL DEFAULT '[]',
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  invalidated_at TEXT,
  invalidated_reason TEXT,
  invalidated_by_rollback_id TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (run_id, phase)
);
CREATE INDEX IF NOT EXISTS idx_generation_batch_checkpoints_project
  ON generation_batch_checkpoints(project_id, run_id, batch_index);

-- 可编辑流程草稿。当前修订只保存在头记录中，文档内容按修订不可变保存。
CREATE TABLE IF NOT EXISTS flowchart_drafts (
  interaction_id    TEXT PRIMARY KEY REFERENCES authoring_interactions(id) ON DELETE CASCADE,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  current_revision  INTEGER NOT NULL CHECK (current_revision >= 1),
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE (project_id, interaction_id)
);
CREATE INDEX IF NOT EXISTS idx_flowchart_drafts_project
  ON flowchart_drafts(project_id, updated_at);

CREATE TABLE IF NOT EXISTS flowchart_draft_revisions (
  project_id        TEXT NOT NULL,
  interaction_id    TEXT NOT NULL,
  revision          INTEGER NOT NULL CHECK (revision >= 1),
  document_json     TEXT NOT NULL,
  warnings_json     TEXT NOT NULL DEFAULT '[]',
  actor_username    TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  PRIMARY KEY (interaction_id, revision),
  UNIQUE (project_id, interaction_id, revision),
  FOREIGN KEY (project_id, interaction_id)
    REFERENCES flowchart_drafts(project_id, interaction_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS flowchart_draft_submissions (
  project_id        TEXT NOT NULL,
  interaction_id    TEXT NOT NULL,
  revision          INTEGER NOT NULL,
  actor_username    TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  PRIMARY KEY (interaction_id, revision),
  FOREIGN KEY (project_id, interaction_id, revision)
    REFERENCES flowchart_draft_revisions(project_id, interaction_id, revision) ON DELETE CASCADE
);

-- Git 不能参加 SQLite 事务，先记录流程图提交意图，服务重试时可继续收敛交互状态。
CREATE TABLE IF NOT EXISTS flowchart_review_finalizations (
  project_id           TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  interaction_id       TEXT NOT NULL,
  draft_revision       INTEGER NOT NULL CHECK (draft_revision > 0),
  request_id           TEXT NOT NULL,
  response_json        TEXT NOT NULL,
  actor_username       TEXT NOT NULL,
  package_revision_oid TEXT,
  status               TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'completed')),
  created_at           TEXT NOT NULL,
  completed_at         TEXT,
  PRIMARY KEY (interaction_id, draft_revision),
  UNIQUE (project_id, request_id),
  FOREIGN KEY (project_id, interaction_id, draft_revision)
    REFERENCES flowchart_draft_revisions(project_id, interaction_id, revision) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_flowchart_review_finalizations_project
  ON flowchart_review_finalizations(project_id, status, created_at);

CREATE TRIGGER IF NOT EXISTS trg_flowchart_revisions_immutable
BEFORE UPDATE ON flowchart_draft_revisions
BEGIN
  SELECT RAISE(ABORT, 'flowchart draft revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_flowchart_submissions_immutable
BEFORE UPDATE ON flowchart_draft_submissions
BEGIN
  SELECT RAISE(ABORT, 'flowchart draft submissions are immutable');
END;

CREATE TABLE IF NOT EXISTS project_events (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  seq             INTEGER NOT NULL,
  type            TEXT NOT NULL,
  run_id          TEXT REFERENCES authoring_runs(id) ON DELETE SET NULL,
  interaction_id  TEXT REFERENCES authoring_interactions(id) ON DELETE SET NULL,
  payload_json    TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL,
  UNIQUE(project_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_project_events_replay
  ON project_events(project_id, seq);

CREATE TABLE IF NOT EXISTS project_operation_leases (
  project_id        TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  operation         TEXT NOT NULL,
  owner             TEXT NOT NULL,
  token             TEXT NOT NULL,
  acquired_at       TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  expires_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_operation_leases_expiry
  ON project_operation_leases(expires_at);

-- ---- M2：包版本 ----

-- 知识包版本（每次生成/优化产出一版；spec_json 是结构化规格，files_json 是渲染产物索引）
CREATE TABLE IF NOT EXISTS package_versions (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version       TEXT NOT NULL,       -- 1.0.0 / 1.1.0 ...
  spec_json     TEXT NOT NULL,       -- PackageSpec（对象/操作/映射结构化规格）
  coverage_json TEXT,                -- 胜任力覆盖检查结果
  note          TEXT,                -- 变更说明
  revision_oid  TEXT,                -- 对应的 Git 修订
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pkgver_project ON package_versions(project_id, created_at);

-- Git 修订的轻量索引。文件内容仍以每个项目的 Git 仓库为准。
CREATE TABLE IF NOT EXISTS artifact_revisions (
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  oid               TEXT NOT NULL,
  parent_oid        TEXT,
  sequence          INTEGER NOT NULL,
  cause             TEXT NOT NULL,
  summary           TEXT NOT NULL,
  actor_kind        TEXT NOT NULL,
  actor_name        TEXT NOT NULL,
  package_version   TEXT,
  changed_paths_json TEXT NOT NULL DEFAULT '[]',
  created_at        TEXT NOT NULL,
  PRIMARY KEY (project_id, oid),
  UNIQUE (project_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_artifact_revisions_project
  ON artifact_revisions(project_id, sequence DESC);

-- Git 与 SQLite 跨存储提交的持久化意图。正常完成时与索引同事务删除；
-- 进程中断后首次访问会把未建立索引的单个 Git 提交回退到 base_revision_oid。
CREATE TABLE IF NOT EXISTS artifact_commit_intents (
  project_id        TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  base_revision_oid TEXT NOT NULL,
  created_at        TEXT NOT NULL
);

-- 尚未成为当前确认版本的修改提案。内容以不可变变更组保存，接受时才写入 Git。
CREATE TABLE IF NOT EXISTS artifact_candidate_revisions (
  id                      TEXT PRIMARY KEY,
  project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  base_revision_oid       TEXT NOT NULL,
  status                  TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN (
                            'pending', 'accepted', 'revision_requested', 'rejected', 'superseded'
                          )),
  cause                   TEXT NOT NULL,
  summary                 TEXT NOT NULL,
  actor_kind              TEXT NOT NULL,
  actor_name              TEXT NOT NULL,
  groups_json             TEXT NOT NULL,
  selected_group_ids_json TEXT NOT NULL DEFAULT '[]',
  accepted_revision_oid   TEXT,
  decision_cause          TEXT,
  decision_summary        TEXT,
  decision_actor_kind     TEXT,
  decision_actor_name     TEXT,
  decision_note           TEXT,
  created_at              TEXT NOT NULL,
  decided_at              TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifact_candidates_project
  ON artifact_candidate_revisions(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifact_candidates_pending
  ON artifact_candidate_revisions(project_id, status, created_at DESC);

-- 新 Agent 运行时的项目级候选工作区。Agent 只在这里累积修改；只有人工确认后
-- 才通过 artifact_candidate_revisions 写入 Git 事实版本。
CREATE TABLE IF NOT EXISTS agent_candidate_workspaces (
  project_id        TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  run_id            TEXT NOT NULL,
  owner             TEXT NOT NULL,
  base_revision_oid TEXT NOT NULL,
  package_data_source_type TEXT NOT NULL DEFAULT 'neo4j'
                    CHECK (package_data_source_type IN ('nl2sql', 'neo4j')),
  candidate_id      TEXT REFERENCES artifact_candidate_revisions(id) ON DELETE SET NULL,
  lease_expires_at  TEXT NOT NULL,
  credential_hash   TEXT,
  credential_expires_at TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_workspace_run
  ON agent_candidate_workspaces(run_id);

CREATE TABLE IF NOT EXISTS agent_candidate_changes (
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id            TEXT NOT NULL,
  path              TEXT NOT NULL,
  kind              TEXT NOT NULL CHECK (kind IN ('write', 'delete')),
  content           TEXT,
  group_id          TEXT NOT NULL,
  evidence_json     TEXT NOT NULL DEFAULT '[]',
  reason            TEXT,
  updated_at        TEXT NOT NULL,
  PRIMARY KEY (project_id, run_id, path)
);
CREATE INDEX IF NOT EXISTS idx_agent_changes_group
  ON agent_candidate_changes(project_id, run_id, group_id);

-- 确定性脚本只有在隔离沙箱对当前源码全量通过后才能进入确认。
-- source_hash 将测试结果绑定到实际候选内容，避免旧脚本的通过结果授权新脚本。
CREATE TABLE IF NOT EXISTS agent_script_test_receipts (
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id            TEXT NOT NULL,
  script_path       TEXT NOT NULL,
  source_hash       TEXT NOT NULL,
  total             INTEGER NOT NULL CHECK (total > 0),
  passed            INTEGER NOT NULL CHECK (passed = total),
  failed            INTEGER NOT NULL CHECK (failed = 0),
  sandbox           TEXT NOT NULL CHECK (sandbox = 'jiuwenbox'),
  created_at        TEXT NOT NULL,
  PRIMARY KEY (project_id, run_id, script_path, source_hash)
);

-- 确定性计算范围必须由专家显式确认。scope_hash 绑定不含脚本实现的业务知识，
-- 业务操作、规则、指标或对象发生变化后，旧决定自动失效并重新进入人工确认。
CREATE TABLE IF NOT EXISTS agent_deterministic_scope_decisions (
  project_id                  TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  scope_hash                  TEXT NOT NULL,
  candidates_json             TEXT NOT NULL,
  selected_operation_keys_json TEXT NOT NULL,
  reason                      TEXT NOT NULL,
  decided_by                  TEXT NOT NULL,
  decided_at                  TEXT NOT NULL
);

-- Studio 写接口的幂等回执。这里只保存一次 HTTP 写操作的输入摘要和响应，
-- 不表达项目阶段或当前业务状态；项目业务状态仍以 project_agent_state 为唯一来源。
CREATE TABLE IF NOT EXISTS studio_mutation_receipts (
  project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  client_mutation_id  TEXT NOT NULL,
  request_hash        TEXT NOT NULL,
  response_json       TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  PRIMARY KEY (project_id, client_mutation_id)
);

-- Agent 领域工具的确定性失败回执。相同请求只有在工作修订或资料依赖变化后才重新执行，
-- 避免模型通过更换调用 ID 之外的无效重试反复触发同一轮编译和校验。
CREATE TABLE IF NOT EXISTS agent_domain_mutation_failure_receipts (
  project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  mutation_id         TEXT NOT NULL,
  request_hash        TEXT NOT NULL,
  working_revision    TEXT NOT NULL,
  dependency_revision TEXT NOT NULL,
  error_json          TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  PRIMARY KEY (project_id, mutation_id, request_hash, working_revision, dependency_revision)
);
CREATE INDEX IF NOT EXISTS idx_agent_domain_failure_mutation
  ON agent_domain_mutation_failure_receipts(project_id, mutation_id, created_at DESC);

-- 运行一旦使用 v2 领域工具，即由领域编译器接管受管路径；历史 checkpoint 未接管时保留兼容窗口。
CREATE TABLE IF NOT EXISTS agent_domain_tool_adoptions (
  project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id           TEXT NOT NULL,
  contract_version TEXT NOT NULL,
  adopted_at       TEXT NOT NULL,
  PRIMARY KEY (project_id, run_id)
);

-- 模型只负责提出任务，计划状态由 Change Set、校验和用户确认的确定性事实推进。
CREATE TABLE IF NOT EXISTS agent_change_plans (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id            TEXT NOT NULL,
  goal              TEXT NOT NULL,
  mode              TEXT NOT NULL CHECK (mode IN ('expert_edit', 'generation')),
  status            TEXT NOT NULL CHECK (status IN ('active', 'paused', 'completed', 'failed', 'cancelled')),
  approval_status   TEXT NOT NULL DEFAULT 'approved'
                    CHECK (approval_status IN ('pending', 'approved', 'rejected')),
  approved_by       TEXT,
  approved_at       TEXT,
  version           INTEGER NOT NULL DEFAULT 1,
  expected_revision TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_change_plans_run
  ON agent_change_plans(project_id, run_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_change_plan_tasks (
  id                 TEXT PRIMARY KEY,
  plan_id            TEXT NOT NULL REFERENCES agent_change_plans(id) ON DELETE CASCADE,
  client_key         TEXT NOT NULL,
  ordinal            INTEGER NOT NULL,
  title              TEXT NOT NULL,
  resource_kind      TEXT NOT NULL CHECK (resource_kind IN ('object', 'rule', 'operation', 'metric', 'script', 'binding', 'other')),
  target_key         TEXT NOT NULL,
  allowed_tools_json TEXT NOT NULL,
  depends_on_json    TEXT NOT NULL,
  acceptance         TEXT NOT NULL,
  required           INTEGER NOT NULL CHECK (required IN (0, 1)),
  status             TEXT NOT NULL CHECK (status IN ('pending', 'staged', 'validated', 'waiting_user', 'accepted', 'rejected', 'failed', 'cancelled', 'skipped')),
  change_set_id      TEXT,
  updated_at         TEXT NOT NULL,
  UNIQUE (plan_id, client_key),
  UNIQUE (plan_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_agent_change_plan_tasks_plan
  ON agent_change_plan_tasks(plan_id, ordinal);

CREATE TABLE IF NOT EXISTS agent_object_relation_reviews (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  plan_id           TEXT NOT NULL REFERENCES agent_change_plans(id) ON DELETE CASCADE,
  task_id           TEXT NOT NULL REFERENCES agent_change_plan_tasks(id) ON DELETE CASCADE,
  working_revision  TEXT NOT NULL,
  schema_input_hash TEXT NOT NULL,
  status            TEXT NOT NULL CHECK (status IN ('open', 'applied', 'stale')),
  applied_revision  TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_object_relation_reviews_task
  ON agent_object_relation_reviews(project_id, task_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_object_relation_review_candidates (
  review_id          TEXT NOT NULL REFERENCES agent_object_relation_reviews(id) ON DELETE CASCADE,
  fingerprint        TEXT NOT NULL,
  ordinal            INTEGER NOT NULL,
  source_object      TEXT NOT NULL,
  target_object      TEXT NOT NULL,
  relation           TEXT NOT NULL,
  note               TEXT NOT NULL,
  reason             TEXT NOT NULL,
  evidence_json      TEXT NOT NULL DEFAULT '[]',
  default_selected   INTEGER NOT NULL CHECK (default_selected IN (0, 1)),
  selected           INTEGER NOT NULL CHECK (selected IN (0, 1)),
  error_json         TEXT,
  PRIMARY KEY (review_id, fingerprint),
  UNIQUE (review_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_agent_object_relation_review_candidates_page
  ON agent_object_relation_review_candidates(review_id, ordinal);

-- staging 表示结果尚待结算；同一 mutation_id + request_hash 可安全重放或查询。
CREATE TABLE IF NOT EXISTS agent_change_sets (
  id                 TEXT PRIMARY KEY,
  project_id         TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id             TEXT NOT NULL,
  plan_id            TEXT NOT NULL REFERENCES agent_change_plans(id) ON DELETE CASCADE,
  mutation_id        TEXT NOT NULL,
  request_hash       TEXT NOT NULL,
  expected_revision  TEXT NOT NULL,
  resulting_revision TEXT,
  candidate_revision TEXT,
  group_id           TEXT,
  status             TEXT NOT NULL CHECK (status IN ('staging', 'staged', 'validated', 'waiting_user', 'accepted', 'rejected', 'failed', 'discarded', 'cancelled')),
  actions_json       TEXT NOT NULL,
  receipt_json       TEXT,
  error_json         TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (project_id, mutation_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_change_sets_plan
  ON agent_change_sets(plan_id, created_at);

CREATE TABLE IF NOT EXISTS agent_change_control_events (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id      TEXT NOT NULL,
  plan_id     TEXT NOT NULL REFERENCES agent_change_plans(id) ON DELETE CASCADE,
  change_set_id TEXT,
  event_type  TEXT NOT NULL,
  summary     TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_change_control_events_plan
  ON agent_change_control_events(plan_id, created_at);

-- Python Agent 的取消栅栏。它只阻止同一 run 在 Node 侧开始新的写入，
-- 不保存业务正文；已完成的候选和审计记录仍由各自表保留。
CREATE TABLE IF NOT EXISTS agent_run_cancellations (
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id      TEXT NOT NULL,
  reason      TEXT NOT NULL DEFAULT '',
  requested_at TEXT NOT NULL,
  PRIMARY KEY (project_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_run_cancellations_project
  ON agent_run_cancellations(project_id, requested_at DESC);

-- 浏览器黑盒验收专用的一次性故障开关。仅在显式验收模式下由可见页面读写，
-- 不保存任意代码、地址或请求内容，也不会在生产模式下提供路由。
CREATE TABLE IF NOT EXISTS acceptance_faults (
  owner          TEXT NOT NULL,
  fault_id       TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'closed'
                 CHECK (status IN ('armed', 'consumed', 'closed')),
  consumed_count INTEGER NOT NULL DEFAULT 0,
  effect_count   INTEGER NOT NULL DEFAULT 0,
  active_run_id  TEXT,
  armed_at       TEXT,
  consumed_at    TEXT,
  closed_at      TEXT,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (owner, fault_id)
);
CREATE INDEX IF NOT EXISTS idx_acceptance_faults_owner_status
  ON acceptance_faults(owner, status, updated_at);

-- 浏览器验收项目的隔离标记。只有显式创建的合成项目才能使用确定性验收模型，
-- 普通项目即使部署时开启验收页面也始终沿用用户或平台的真实模型配置。
CREATE TABLE IF NOT EXISTS acceptance_projects (
  project_id   TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  owner        TEXT NOT NULL,
  scenario_id  TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acceptance_projects_owner
  ON acceptance_projects(owner, created_at DESC);

-- 脚本先定义结构化契约，再提交源码。契约绑定操作资源哈希，避免在操作变化后继续复用旧约束。
CREATE TABLE IF NOT EXISTS agent_script_contract_drafts (
  project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id              TEXT NOT NULL,
  script_key          TEXT NOT NULL,
  operation_hash      TEXT NOT NULL,
  contract_hash       TEXT NOT NULL,
  contract_json       TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  PRIMARY KEY (project_id, run_id, script_key)
);

-- 非 Studio 结构化工具的成功回执，供脚本契约和源码写入做业务级幂等重放。
CREATE TABLE IF NOT EXISTS agent_structured_mutation_receipts (
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  mutation_id   TEXT NOT NULL,
  tool          TEXT NOT NULL,
  request_hash  TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, mutation_id)
);

-- 结构化建模目标草稿：按项目/run/draft 持久化分段，支持服务重启后继续提交。
CREATE TABLE IF NOT EXISTS agent_goal_drafts (
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id        TEXT NOT NULL,
  draft_id      TEXT NOT NULL,
  group_id      TEXT NOT NULL,
  base_revision TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'finalized', 'abandoned')),
  sections_json TEXT NOT NULL DEFAULT '{}',
  section_revisions_json TEXT NOT NULL DEFAULT '{}',
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, run_id, draft_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_goal_drafts_project
  ON agent_goal_drafts(project_id, updated_at DESC);

-- 兼容旧版本的可选关系推荐进度结构；当前版本不以它作为建模门禁，也不要求写入。
CREATE TABLE IF NOT EXISTS agent_schema_relation_suggestions (
  project_id              TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  semantic_fingerprint    TEXT NOT NULL,
  status                  TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'completed')),
  schema_input_hash       TEXT NOT NULL DEFAULT '',
  total_candidate_count   INTEGER NOT NULL DEFAULT 0 CHECK (total_candidate_count >= 0),
  returned_candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (returned_candidate_count >= 0),
  completed_at            TEXT,
  updated_at              TEXT NOT NULL
);

-- artifact candidate 的 groups_json 保持通用格式；工作台所需的分诊层级与影响提示
-- 单独保存，避免把 Agent UI 语义耦合进底层 Git 仓库。
CREATE TABLE IF NOT EXISTS agent_candidate_group_metadata (
  candidate_id      TEXT NOT NULL REFERENCES artifact_candidate_revisions(id) ON DELETE CASCADE,
  group_id          TEXT NOT NULL,
  tier              TEXT NOT NULL CHECK (tier IN ('caliber', 'structural', 'index', 'script')),
  title             TEXT NOT NULL,
  impact_json       TEXT NOT NULL DEFAULT '[]',
  warning           TEXT,
  PRIMARY KEY (candidate_id, group_id)
);

-- 候选冻结前的确定性校验回执。接受时必须重新计算候选哈希并匹配当前校验器版本，
-- 防止用户确认后候选内容变化或旧校验结果被新版本规则继续复用。
CREATE TABLE IF NOT EXISTS agent_candidate_validation_receipts (
  candidate_id       TEXT PRIMARY KEY REFERENCES artifact_candidate_revisions(id) ON DELETE CASCADE,
  project_id         TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  base_revision_oid  TEXT NOT NULL,
  candidate_hash     TEXT NOT NULL,
  validator_version  TEXT NOT NULL,
  validated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_candidate_validation_receipts_project
  ON agent_candidate_validation_receipts(project_id, validated_at DESC);

-- 校验报告采用“完整事实 + 上下文摘要”双层存储。Agent 工具结果只携带摘要和
-- report id；逐条 finding 在需要修复时分页读取，避免同一大报告反复进入模型上下文。
CREATE TABLE IF NOT EXISTS agent_validation_reports (
  id                   TEXT PRIMARY KEY,
  project_id           TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id               TEXT NOT NULL,
  tool_name             TEXT NOT NULL,
  error_code            TEXT NOT NULL,
  validator_version     TEXT NOT NULL,
  finding_fingerprint   TEXT NOT NULL,
  finding_count         INTEGER NOT NULL CHECK (finding_count >= 0),
  summary_json          TEXT NOT NULL,
  report_json           TEXT NOT NULL,
  created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_validation_reports_run
  ON agent_validation_reports(project_id, run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_validation_reports_fingerprint
  ON agent_validation_reports(project_id, finding_fingerprint, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_validation_findings (
  report_id          TEXT NOT NULL REFERENCES agent_validation_reports(id) ON DELETE CASCADE,
  ordinal            INTEGER NOT NULL CHECK (ordinal >= 0),
  rule_id            TEXT NOT NULL,
  path               TEXT NOT NULL,
  message            TEXT NOT NULL,
  suggestion         TEXT NOT NULL,
  finding_json       TEXT NOT NULL,
  PRIMARY KEY (report_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_agent_validation_findings_path
  ON agent_validation_findings(report_id, path, ordinal);

CREATE TABLE IF NOT EXISTS agent_confirmations (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  candidate_id      TEXT NOT NULL REFERENCES artifact_candidate_revisions(id) ON DELETE CASCADE,
  revision_oid      TEXT NOT NULL,
  actor_username    TEXT NOT NULL,
  review_mode       TEXT NOT NULL CHECK (review_mode IN ('full', 'sequential', 'sampled', 'automatic')),
  selected_groups_json TEXT NOT NULL,
  sampled_groups_json  TEXT NOT NULL DEFAULT '[]',
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_confirmations_project
  ON agent_confirmations(project_id, created_at DESC);

-- 融合来源是只读输入事实，不承载项目当前状态；所有实际导入仍进入 Agent 候选工作区。
CREATE TABLE IF NOT EXISTS agent_package_sources (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  filename          TEXT NOT NULL,
  package_id        TEXT NOT NULL,
  package_name      TEXT NOT NULL,
  package_version   TEXT NOT NULL,
  spec_json         TEXT NOT NULL,
  files_json        TEXT NOT NULL,
  uploaded_by       TEXT NOT NULL,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_package_sources_project
  ON agent_package_sources(project_id, created_at DESC);

-- 来源知识包一经上传便按原始字节只读保存。旧的 files_json 继续服务单包 merge，
-- 批量融合只读取这里的完整归档，避免丢失根文件和二进制资产。
CREATE TABLE IF NOT EXISTS agent_package_source_archives (
  source_id         TEXT PRIMARY KEY REFERENCES agent_package_sources(id) ON DELETE CASCADE,
  fingerprint       TEXT NOT NULL,
  coverage_json     TEXT NOT NULL,
  text_files_json   TEXT NOT NULL,
  warnings_json     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS agent_package_source_files (
  source_id         TEXT NOT NULL REFERENCES agent_package_sources(id) ON DELETE CASCADE,
  path              TEXT NOT NULL,
  content           BLOB NOT NULL,
  PRIMARY KEY (source_id, path)
);

-- 融合计划是一次批量编译的可审计输入。ledger_json 在待裁决时保存逐元素
-- 去向要求，生成候选后保存编译器给出的最终 fused key 和 provenance。
CREATE TABLE IF NOT EXISTS agent_package_fusion_plans (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  expected_revision_oid TEXT NOT NULL,
  source_ids_json       TEXT NOT NULL,
  plan_json             TEXT NOT NULL,
  ledger_json           TEXT NOT NULL,
  quality_json          TEXT,
  output_digest         TEXT,
  status                TEXT NOT NULL CHECK (status IN (
                          'awaiting_decisions', 'ready', 'compiling', 'candidate', 'failed'
                        )),
  candidate_id          TEXT REFERENCES artifact_candidate_revisions(id) ON DELETE SET NULL,
  error                 TEXT,
  created_by            TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_package_fusion_plans_project
  ON agent_package_fusion_plans(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_confirm_policy (
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  tier              TEXT NOT NULL CHECK (tier IN ('structural', 'index')),
  mode              TEXT NOT NULL CHECK (mode IN ('ask', 'auto')),
  updated_at        TEXT NOT NULL,
  PRIMARY KEY (project_id, tier)
);

-- 人工编辑草稿。Git revision 是事实基线；语义版本字段只用于展示和历史追踪。
CREATE TABLE IF NOT EXISTS package_drafts (
  id                      TEXT PRIMARY KEY,
  project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  base_package_version_id TEXT NOT NULL REFERENCES package_versions(id) ON DELETE CASCADE,
  base_version            TEXT NOT NULL,
  base_revision_oid       TEXT NOT NULL,
  status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'published', 'discarded')),
  revision                INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  created_by              TEXT NOT NULL,
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_package_drafts_one_active
  ON package_drafts(project_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_package_drafts_project
  ON package_drafts(project_id, updated_at);

-- 草稿只保存相对基线版本发生变化的文件；恢复为基线内容时删除对应行。
CREATE TABLE IF NOT EXISTS package_draft_files (
  draft_id          TEXT NOT NULL REFERENCES package_drafts(id) ON DELETE CASCADE,
  path              TEXT NOT NULL,
  content           TEXT NOT NULL,
  base_content_hash TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  PRIMARY KEY (draft_id, path)
);
CREATE INDEX IF NOT EXISTS idx_package_draft_files_draft
  ON package_draft_files(draft_id, updated_at);

-- ---- M3：服务设置（键值，页面可配置） ----
CREATE TABLE IF NOT EXISTS settings (
  key           TEXT PRIMARY KEY,
  value         TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- 已认证用户的 LLM 页面配置。部署环境变量作为未覆盖字段的默认值。
CREATE TABLE IF NOT EXISTS user_llm_settings (
  username      TEXT PRIMARY KEY,
  base_url      TEXT,
  model         TEXT,
  api_key       TEXT,
  max_tokens    INTEGER CHECK (max_tokens IS NULL OR max_tokens BETWEEN 1 AND 1000000),
  context_window INTEGER CHECK (context_window IS NULL OR context_window >= 1),
  thinking_mode TEXT CHECK (thinking_mode IS NULL OR thinking_mode IN ('default', 'enabled', 'disabled')),
  reasoning_effort TEXT,
  extra_body_json TEXT,
  updated_at    TEXT NOT NULL
);

-- 已认证用户的后台任务 LLM 页面配置；为空时使用该用户当前主模型配置。
CREATE TABLE IF NOT EXISTS user_task_llm_settings (
  username       TEXT PRIMARY KEY,
  base_url       TEXT,
  model          TEXT,
  api_key        TEXT,
  max_tokens     INTEGER CHECK (max_tokens IS NULL OR max_tokens BETWEEN 1 AND 1000000),
  context_window INTEGER CHECK (context_window IS NULL OR context_window >= 1),
  thinking_mode  TEXT CHECK (thinking_mode IS NULL OR thinking_mode IN ('default', 'enabled', 'disabled')),
  reasoning_effort TEXT,
  extra_body_json TEXT,
  updated_at     TEXT NOT NULL
);

-- Modeling 平台级 LLM 默认值。admin 只负责维护，配置不归属于 admin 个人账号。
-- API Key 使用部署密钥加密后保存；接口和日志均不得返回密文或明文。
CREATE TABLE IF NOT EXISTS llm_default_settings (
  purpose             TEXT PRIMARY KEY CHECK (purpose IN ('main', 'task')),
  base_url            TEXT NOT NULL,
  model               TEXT NOT NULL,
  api_key_ciphertext  TEXT,
  max_tokens          INTEGER NOT NULL CHECK (max_tokens BETWEEN 1 AND 1000000),
  context_window      INTEGER NOT NULL DEFAULT 1000000 CHECK (context_window >= 1),
  thinking_mode       TEXT NOT NULL DEFAULT 'default' CHECK (thinking_mode IN ('default', 'enabled', 'disabled')),
  reasoning_effort    TEXT NOT NULL DEFAULT '',
  extra_body_json     TEXT NOT NULL DEFAULT '{}',
  revision            INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  updated_by          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);

-- 平台默认只作为模板。新用户注册或用户主动同步时复制为独立快照；
-- admin 后续修改/删除模板不会改变既有用户快照。
CREATE TABLE IF NOT EXISTS user_llm_default_snapshots (
  username            TEXT NOT NULL,
  purpose             TEXT NOT NULL CHECK (purpose IN ('main', 'task')),
  base_url            TEXT NOT NULL,
  model               TEXT NOT NULL,
  api_key_ciphertext  TEXT,
  max_tokens          INTEGER NOT NULL CHECK (max_tokens BETWEEN 1 AND 1000000),
  context_window      INTEGER NOT NULL DEFAULT 1000000 CHECK (context_window >= 1),
  thinking_mode       TEXT NOT NULL DEFAULT 'default' CHECK (thinking_mode IN ('default', 'enabled', 'disabled')),
  reasoning_effort    TEXT NOT NULL DEFAULT '',
  extra_body_json     TEXT NOT NULL DEFAULT '{}',
  default_revision    INTEGER NOT NULL CHECK (default_revision >= 1),
  assigned_via        TEXT NOT NULL CHECK (assigned_via IN ('registration', 'sync')),
  assigned_at         TEXT NOT NULL,
  PRIMARY KEY (username, purpose)
);

-- 项目级评测数据源。由 modeling 持久化公开配置；密钥不落库。
CREATE TABLE IF NOT EXISTS project_data_sources (
  project_id        TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  mode              TEXT NOT NULL,          -- connection | none
  provider          TEXT,
  workspace_id      TEXT NOT NULL,
  ontology_id       TEXT,
  public_config_json TEXT NOT NULL DEFAULT '{}',
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

-- 知识包生成使用的数据源类型。它只决定 mapping 资源命名，不保存真实连接信息。
CREATE TABLE IF NOT EXISTS project_package_data_sources (
  project_id        TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  data_source_type  TEXT NOT NULL CHECK (data_source_type IN ('nl2sql', 'neo4j')),
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

-- Neo4j Excel 图导入临时文件；执行、取消或过期后删除文件与记录。
CREATE TABLE IF NOT EXISTS graph_import_uploads (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  owner         TEXT NOT NULL,
  filename      TEXT NOT NULL,
  stored_path   TEXT NOT NULL,
  file_hash     TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  package_revision_oid TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_graph_upload_project ON graph_import_uploads(project_id, created_at);

-- 图导入审计；只保存脱敏映射与统计，不保存 Excel 和 Neo4j 密码。
CREATE TABLE IF NOT EXISTS graph_import_runs (
  id                 TEXT PRIMARY KEY,
  project_id         TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  filename           TEXT NOT NULL,
  file_hash          TEXT NOT NULL,
  package_version    TEXT NOT NULL,
  package_revision_oid TEXT NOT NULL,
  mapping_json       TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'running',
  node_count         INTEGER NOT NULL DEFAULT 0,
  relationship_count INTEGER NOT NULL DEFAULT 0,
  skipped_count      INTEGER NOT NULL DEFAULT 0,
  error              TEXT,
  created_at         TEXT NOT NULL,
  finished_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_graph_run_project ON graph_import_runs(project_id, created_at);

-- 评测库 Excel 后台导入状态；不保存 Excel 内容或数据库密码。
CREATE TABLE IF NOT EXISTS evaluation_database_import_runs (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  filename      TEXT NOT NULL,
  database_name TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'running',
  table_count   INTEGER NOT NULL DEFAULT 0,
  row_count     INTEGER NOT NULL DEFAULT 0,
  tables_json   TEXT NOT NULL DEFAULT '[]',
  error         TEXT,
  created_at    TEXT NOT NULL,
  finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_db_import_project
  ON evaluation_database_import_runs(project_id, created_at);

-- ---- M3：评测用例 / 轮次 / 结果 ----
CREATE TABLE IF NOT EXISTS testcases (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  question      TEXT NOT NULL,
  ground_truth  TEXT NOT NULL DEFAULT '',
  source        TEXT NOT NULL DEFAULT 'manual',  -- manual | competency
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tc_project ON testcases(project_id);

CREATE TABLE IF NOT EXISTS eval_rounds (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  package_version TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'running', -- running | done | failed
  progress_done INTEGER NOT NULL DEFAULT 0,
  progress_total INTEGER NOT NULL DEFAULT 0,
  pass_count    INTEGER NOT NULL DEFAULT 0,
  phase         TEXT NOT NULL DEFAULT 'workspace',
  phase_started_at TEXT,
  current_item_started_at TEXT,
  log           TEXT NOT NULL DEFAULT '',        -- 轮次执行日志（换行分隔）
  artifact_revision_oid TEXT,                    -- 本轮固定使用的 Git 修订
  evaluation_scope_json TEXT NOT NULL DEFAULT '{"kind":"current"}',
  created_at    TEXT NOT NULL,
  finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_round_project ON eval_rounds(project_id, created_at);

CREATE TABLE IF NOT EXISTS eval_results (
  id            TEXT PRIMARY KEY,
  round_id      TEXT NOT NULL REFERENCES eval_rounds(id) ON DELETE CASCADE,
  testcase_id   TEXT NOT NULL,
  question      TEXT NOT NULL,
  ground_truth  TEXT NOT NULL,
  final_answer  TEXT,
  verdict       TEXT,               -- pass | partial | fail | error
  reason        TEXT,
  overridden    INTEGER NOT NULL DEFAULT 0,  -- 人工改判标记
  original_verdict TEXT,                     -- 首次人工改判前的自动判定
  override_reason TEXT,                      -- 人工改判理由
  override_actor TEXT,                       -- 人工改判操作人
  overridden_at TEXT,                        -- 最后一次人工改判时间
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_res_round ON eval_results(round_id);

CREATE TABLE IF NOT EXISTS eval_result_traces (
  result_id      TEXT PRIMARY KEY REFERENCES eval_results(id) ON DELETE CASCADE,
  run_id         TEXT,
  session_id     TEXT,
  tool_calls_json TEXT NOT NULL DEFAULT '[]',
  tool_failures_json TEXT NOT NULL DEFAULT '[]',
  trace_status   TEXT NOT NULL DEFAULT 'complete',
  trace_error    TEXT
);

-- ---- M4：优化建议（Judge 失败结果 → 结构化建议 → 候选修订 → 专家采纳） ----
CREATE TABLE IF NOT EXISTS opt_suggestions (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  round_id      TEXT NOT NULL,       -- 基于哪一轮评测产生
  based_version TEXT NOT NULL,       -- 基于的包版本
  based_revision_oid TEXT NOT NULL DEFAULT '', -- 基于的 Git 修订；同版本号仍可精确判陈旧
  title         TEXT NOT NULL,
  detail        TEXT NOT NULL,       -- 建议详情（含要沉淀进包的业务规则）
  kind          TEXT NOT NULL,       -- add/modify: operation | object | rule | metric | script；或 clarify_description
  target        TEXT,                -- 目标对象/操作/规则/指标/脚本 key（新增类可空）
  related_question TEXT,             -- 关联的未通过用例问题
  diagnosis_ids_json TEXT NOT NULL DEFAULT '[]', -- 建议依据的失败诊断 ID
  evidence_json TEXT NOT NULL DEFAULT '[]',      -- 支持该建议的独立证据
  generalization_plan TEXT NOT NULL DEFAULT '',  -- 如何避免只适配当前用例
  validation_plan TEXT NOT NULL DEFAULT '',      -- 如何用样例外场景验证
  candidate_revision_id TEXT,          -- 最近一次 PR7 候选修订 ID；退回后保留审计关联
  candidate_feedback TEXT,             -- 候选被退回后带入下一次修订的调整要求
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending | candidate | applied | rejected
  applied_version TEXT,              -- 接受候选时生成的新 spec.version
  applied_revision_oid TEXT,         -- 接受候选后形成的正式 Git 修订
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opt_project ON opt_suggestions(project_id, created_at);

CREATE TABLE IF NOT EXISTS failure_diagnoses (
  id                 TEXT PRIMARY KEY,
  project_id         TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  round_id           TEXT NOT NULL,
  based_version      TEXT NOT NULL,
  based_revision_oid TEXT NOT NULL DEFAULT '',
  category           TEXT NOT NULL,
  title              TEXT NOT NULL,
  summary            TEXT NOT NULL,
  evidence_json      TEXT NOT NULL DEFAULT '[]',
  confidence         TEXT NOT NULL,
  blocking           INTEGER NOT NULL DEFAULT 0,
  repair_scope       TEXT NOT NULL,
  package_optimizable INTEGER NOT NULL DEFAULT 0,
  result_ids_json    TEXT NOT NULL DEFAULT '[]',
  related_questions_json TEXT NOT NULL DEFAULT '[]',
  suggested_action   TEXT NOT NULL,
  resolution_status  TEXT NOT NULL DEFAULT 'pending',
  resolution_action  TEXT,
  resolution_note    TEXT,
  resolution_actor   TEXT,
  resolution_payload_json TEXT NOT NULL DEFAULT '{}',
  resolved_at        TEXT,
  created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diag_project_round ON failure_diagnoses(project_id, round_id, created_at);

-- ---- 包体检（最近一次结果；规则引擎 + 语义 agent 的结构化发现） ----
CREATE TABLE IF NOT EXISTS package_checks (
  project_id    TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  version       TEXT NOT NULL,       -- 体检时的包版本
  findings_json TEXT NOT NULL,
  artifact_revision_oid TEXT,        -- 体检时使用的 Git 修订
  created_at    TEXT NOT NULL
);

-- 非阻断警告的显式接受记录。接受只对指定知识包修订有效；发布前需再次确认。
CREATE TABLE IF NOT EXISTS package_warning_acceptances (
  project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  revision_oid        TEXT NOT NULL,
  finding_fingerprint TEXT NOT NULL,
  finding_json        TEXT NOT NULL,
  impact_note         TEXT NOT NULL,
  accepted_by         TEXT NOT NULL,
  accepted_role       TEXT NOT NULL,
  accepted_at         TEXT NOT NULL,
  release_note        TEXT,
  release_confirmed_by TEXT,
  release_confirmed_role TEXT,
  release_confirmed_at TEXT,
  PRIMARY KEY (project_id, revision_oid, finding_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_package_warning_acceptances_project
  ON package_warning_acceptances(project_id, revision_oid, accepted_at);

-- 用户可显式暂缓当前修订的阻断错误以先进入验证；记录只对指定错误集合有效，正式发布仍要求错误清零。
CREATE TABLE IF NOT EXISTS package_error_deferrals (
  project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  revision_oid     TEXT NOT NULL,
  error_count      INTEGER NOT NULL CHECK (error_count > 0),
  error_fingerprints_json TEXT NOT NULL,
  findings_json    TEXT NOT NULL,
  reason           TEXT NOT NULL,
  deferred_by      TEXT NOT NULL,
  deferred_role    TEXT NOT NULL,
  deferred_at      TEXT NOT NULL,
  PRIMARY KEY (project_id, revision_oid)
);
CREATE INDEX IF NOT EXISTS idx_package_error_deferrals_project
  ON package_error_deferrals(project_id, deferred_at);

CREATE TABLE IF NOT EXISTS package_release_records (
  project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version             TEXT NOT NULL,
  revision_oid        TEXT NOT NULL,
  known_limitations_json TEXT NOT NULL DEFAULT '[]',
  released_by         TEXT NOT NULL,
  released_at         TEXT NOT NULL,
  PRIMARY KEY (project_id, version)
);

CREATE TABLE IF NOT EXISTS package_check_revisions (
  project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  artifact_revision_oid TEXT NOT NULL,
  version               TEXT NOT NULL,
  findings_json         TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  PRIMARY KEY (project_id, artifact_revision_oid)
);
CREATE INDEX IF NOT EXISTS idx_package_check_revisions_project
  ON package_check_revisions(project_id, created_at DESC);

DROP TABLE IF EXISTS package_semantic_check_cache;

-- 受控修复逐次审计。root_revision_oid 把自动重试绑定到同一修订链；
-- 用户或其他流程产生的新修订不会沿用上一条链的重试额度。
CREATE TABLE IF NOT EXISTS package_repair_attempts (
  id                   TEXT PRIMARY KEY,
  project_id           TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  root_revision_oid    TEXT NOT NULL,
  input_revision_oid   TEXT NOT NULL,
  output_revision_oid  TEXT,
  authority            TEXT NOT NULL CHECK (
                         authority IN ('automatic', 'approval_required', 'expert_decision')
                       ),
  status               TEXT NOT NULL CHECK (
                         status IN ('running', 'completed', 'waiting_user', 'stopped', 'failed')
                       ),
  execution_status     TEXT NOT NULL DEFAULT 'pending' CHECK (
                         execution_status IN (
                           'pending', 'running', 'waiting_user', 'validating', 'finished', 'failed'
                         )
                       ),
  quality_outcome      TEXT NOT NULL DEFAULT 'unknown_legacy' CHECK (
                         quality_outcome IN (
                           'unresolved', 'resolved', 'improved', 'regressed',
                           'stale', 'rejected', 'unknown_legacy'
                         )
                       ),
  findings_before_json TEXT NOT NULL DEFAULT '[]',
  findings_after_json  TEXT NOT NULL DEFAULT '[]',
  target_fingerprints_json     TEXT NOT NULL DEFAULT '[]',
  resolved_fingerprints_json   TEXT NOT NULL DEFAULT '[]',
  introduced_fingerprints_json TEXT NOT NULL DEFAULT '[]',
  before_blocking_count         INTEGER NOT NULL DEFAULT 0,
  after_blocking_count          INTEGER,
  candidate_revision_oid       TEXT,
  quality_gate_json             TEXT NOT NULL DEFAULT '{}',
  retry_count                   INTEGER NOT NULL DEFAULT 0,
  next_action_json              TEXT,
  actions_json         TEXT NOT NULL DEFAULT '[]',
  error                TEXT,
  created_at           TEXT NOT NULL,
  completed_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_package_repair_attempts_chain
  ON package_repair_attempts(project_id, root_revision_oid, created_at);
CREATE INDEX IF NOT EXISTS idx_package_repair_attempts_output
  ON package_repair_attempts(project_id, output_revision_oid);

-- ---- 旧版包文件覆盖（仅作为升级迁移源；运行时不再读写） ----
CREATE TABLE IF NOT EXISTS package_overrides (
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path          TEXT NOT NULL,       -- 包内相对路径（如 operations/OP-001.md）
  content       TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, path)
);

-- 旧 override 迁移审计。原始内容完整保留，避免自动迁移、忽略失效路径或人工放弃草稿时不可追溯。
CREATE TABLE IF NOT EXISTS package_override_migration_archive (
  project_id              TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path                    TEXT NOT NULL,
  content                 TEXT NOT NULL,
  original_updated_at     TEXT NOT NULL,
  base_package_version_id TEXT REFERENCES package_versions(id) ON DELETE SET NULL,
  base_version            TEXT,
  draft_id                TEXT REFERENCES package_drafts(id) ON DELETE SET NULL,
  disposition             TEXT NOT NULL,
  target_package_version_id TEXT REFERENCES package_versions(id) ON DELETE SET NULL,
  target_version          TEXT,
  detail                  TEXT,
  migrated_at             TEXT NOT NULL,
  PRIMARY KEY (project_id, path, original_updated_at)
);
CREATE INDEX IF NOT EXISTS idx_package_override_archive_project
  ON package_override_migration_archive(project_id, migrated_at);

-- 旧“四份材料一批”的缓存已被内容定义分段检查点取代；升级时移除，避免保留
-- 无生产消费者的第二套缓存状态。该表只存可重算的模型分析缓存，不含用户原始资料。
DROP TABLE IF EXISTS material_analysis_batch_cache;

-- 全文资料分析 Map-Reduce 事实源。分段计划在一次运行创建时冻结；模型失败、
-- 进程重启或普通限流后复用 completed 分段，只领取尚未完成的部分。
CREATE TABLE IF NOT EXISTS material_analysis_runs (
  id                       TEXT PRIMARY KEY,
  project_id               TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_set_fingerprint   TEXT NOT NULL,
  brief_fingerprint        TEXT NOT NULL,
  model_id                 TEXT NOT NULL,
  protocol_version         TEXT NOT NULL,
  plan_json                TEXT NOT NULL,
  status                   TEXT NOT NULL DEFAULT 'running'
                           CHECK (status IN ('running', 'paused', 'failed', 'completed')),
  stage                    TEXT NOT NULL DEFAULT 'planning',
  result_json              TEXT,
  error                    TEXT,
  created_at               TEXT NOT NULL,
  updated_at               TEXT NOT NULL,
  completed_at             TEXT,
  UNIQUE (
    project_id, source_set_fingerprint, brief_fingerprint, model_id, protocol_version
  )
);
CREATE INDEX IF NOT EXISTS idx_material_analysis_runs_project
  ON material_analysis_runs(project_id, updated_at);

CREATE TABLE IF NOT EXISTS material_analysis_chunks (
  run_id                   TEXT NOT NULL REFERENCES material_analysis_runs(id) ON DELETE CASCADE,
  document_key             TEXT NOT NULL,
  document_id              TEXT,
  filename                 TEXT NOT NULL,
  revision_fingerprint     TEXT NOT NULL,
  chunk_index              INTEGER NOT NULL,
  start_offset             INTEGER NOT NULL,
  end_offset               INTEGER NOT NULL,
  content_hash             TEXT NOT NULL,
  status                   TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  attempt_count            INTEGER NOT NULL DEFAULT 0,
  payload_json             TEXT,
  error                    TEXT,
  started_at               TEXT,
  updated_at               TEXT NOT NULL,
  completed_at             TEXT,
  PRIMARY KEY (run_id, document_key, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_material_analysis_chunks_status
  ON material_analysis_chunks(run_id, status, document_key, chunk_index);
CREATE INDEX IF NOT EXISTS idx_material_analysis_chunks_reuse
  ON material_analysis_chunks(
    document_key, revision_fingerprint, start_offset, end_offset, content_hash, status
  );

CREATE TABLE IF NOT EXISTS material_analysis_document_summaries (
  run_id                   TEXT NOT NULL REFERENCES material_analysis_runs(id) ON DELETE CASCADE,
  document_key             TEXT NOT NULL,
  document_id              TEXT,
  filename                 TEXT NOT NULL,
  revision_fingerprint     TEXT NOT NULL,
  status                   TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  attempt_count            INTEGER NOT NULL DEFAULT 0,
  payload_json             TEXT,
  error                    TEXT,
  updated_at               TEXT NOT NULL,
  completed_at             TEXT,
  PRIMARY KEY (run_id, document_key)
);
CREATE INDEX IF NOT EXISTS idx_material_analysis_document_summaries_status
  ON material_analysis_document_summaries(run_id, status, document_key);

-- 表结构摘要缓存。只保存字段统计和有限样例，不保存业务数据行；资料版本、用途版本或摘要算法变化时自然失效。
CREATE TABLE IF NOT EXISTS schema_profile_cache (
  project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  document_id         TEXT NOT NULL,
  content_hash        TEXT NOT NULL,
  sheet_name          TEXT NOT NULL,
  purpose_revision    TEXT NOT NULL,
  analysis_version    TEXT NOT NULL,
  profile_json        TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  PRIMARY KEY (project_id, document_id, content_hash, sheet_name, purpose_revision, analysis_version)
);
CREATE INDEX IF NOT EXISTS idx_schema_profile_cache_current
  ON schema_profile_cache(project_id, document_id, sheet_name, updated_at DESC);

-- 长任务登记表：同项目同操作只能跑一份。原先是进程内 Map，起两个实例后去重会失效，
-- 界面也读不到跑在另一个实例上的任务。带 TTL 并在运行期间续期；启动时统一清空，
-- 因为上个进程的登记不可能再续期（见 authoring/restart-recovery.ts）。
CREATE TABLE IF NOT EXISTS inflight_operations (
  key         TEXT PRIMARY KEY,   -- 形如 pj_xxx:optimize
  operation   TEXT NOT NULL,
  token       TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  -- 取消请求。任务在检查点（批次之间、逐条修复之间）读它并主动退出：
  -- 单次模型调用中途停不下来，所以取消是「当前这批跑完就停」，不是立即中断。
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_inflight_operations_expiry ON inflight_operations(expires_at);
