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
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pkgver_project ON package_versions(project_id, created_at);

-- ---- M3：服务设置（键值，页面可配置） ----
CREATE TABLE IF NOT EXISTS settings (
  key           TEXT PRIMARY KEY,
  value         TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

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
