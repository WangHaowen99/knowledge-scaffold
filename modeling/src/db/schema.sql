-- 建模工具项目库（SQLite）。业务样例数据不在此库（见评测 MySQL）。
CREATE TABLE IF NOT EXISTS projects (
  id            TEXT PRIMARY KEY,
  owner         TEXT NOT NULL,
  name          TEXT NOT NULL,
  domain        TEXT NOT NULL DEFAULT '',
  background    TEXT NOT NULL DEFAULT '',
  target_users  TEXT,
  glossary_prefs TEXT,
  stage         TEXT NOT NULL DEFAULT 'project_setup',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  filename      TEXT NOT NULL,
  kind          TEXT NOT NULL,
  parse_status  TEXT NOT NULL DEFAULT 'pending',
  parse_error   TEXT,
  text_content  TEXT,
  tables_json   TEXT,               -- DataDictTable[] JSON（excel）
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);

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

-- ---- M2：访谈 / 胜任力 / 包版本 ----

-- 访谈批次（一个节点可产生多批；动态追问也记为批）
CREATE TABLE IF NOT EXISTS interview_batches (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  node_id       TEXT,               -- 归属的 wf_node
  ordinal       INTEGER NOT NULL,
  questions_json TEXT NOT NULL,      -- [{id, kind(single|multi|text), q, options[], topic}]
  answered      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_project ON interview_batches(project_id, ordinal);

-- 访谈答案（专家应答，支持回溯修改）
CREATE TABLE IF NOT EXISTS interview_answers (
  id            TEXT PRIMARY KEY,
  batch_id      TEXT NOT NULL REFERENCES interview_batches(id) ON DELETE CASCADE,
  question_id   TEXT NOT NULL,
  answer_json   TEXT NOT NULL,       -- {selected[], other, skipped}
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_answers_batch ON interview_answers(batch_id);

-- 胜任力问题（候选 → 勾选；双落地：operation 覆盖 + 评测预填）
CREATE TABLE IF NOT EXISTS competency_questions (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  question      TEXT NOT NULL,
  selected      INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT 'generated', -- generated | custom
  covered_operation TEXT,            -- 生成后回填：覆盖它的 operation key
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cq_project ON competency_questions(project_id);

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

-- ---- M3：服务设置（键值，页面可配置） ----
CREATE TABLE IF NOT EXISTS settings (
  key           TEXT PRIMARY KEY,
  value         TEXT NOT NULL,
  updated_at    TEXT NOT NULL
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

-- Neo4j Excel 图导入临时文件；执行、取消或过期后删除文件与记录。
CREATE TABLE IF NOT EXISTS graph_import_uploads (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  owner         TEXT NOT NULL,
  filename      TEXT NOT NULL,
  stored_path   TEXT NOT NULL,
  file_hash     TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
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
  log           TEXT NOT NULL DEFAULT '',        -- 轮次执行日志（换行分隔）
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
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_res_round ON eval_results(round_id);

-- ---- M4：优化建议（Judge 失败结果 → 结构化建议 → 采纳 → 版本迭代） ----
CREATE TABLE IF NOT EXISTS opt_suggestions (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  round_id      TEXT NOT NULL,       -- 基于哪一轮评测产生
  based_version TEXT NOT NULL,       -- 基于的包版本
  title         TEXT NOT NULL,
  detail        TEXT NOT NULL,       -- 建议详情（含要沉淀进包的业务规则）
  kind          TEXT NOT NULL,       -- add_operation | modify_operation | add_object | modify_object | clarify_description
  target        TEXT,                -- 目标对象/操作 key（新增类可空）
  related_question TEXT,             -- 关联的未通过用例问题
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending | applied | rejected
  applied_version TEXT,              -- 应用产生的新版本号
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opt_project ON opt_suggestions(project_id, created_at);

-- ---- 包体检（最近一次结果；规则引擎 + 语义 agent 的结构化发现） ----
CREATE TABLE IF NOT EXISTS package_checks (
  project_id    TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  version       TEXT NOT NULL,       -- 体检时的包版本
  findings_json TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

-- ---- 包文件人工修订（覆盖渲染产物；重新生成/优化出新版本后仍生效，直至清除） ----
CREATE TABLE IF NOT EXISTS package_overrides (
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path          TEXT NOT NULL,       -- 包内相对路径（如 operations/OP-001.md）
  content       TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (project_id, path)
);
