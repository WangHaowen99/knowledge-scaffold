# kb-service 设计：管理接口与异步入库（2026-10-26）

参照的源码：WeKnora main `97796c6` 的入库链路（`internal/application/service/knowledge_process.go`、`internal/infrastructure/chunker/`、`internal/infrastructure/docparser/`、`migrations/paradedb/00-init-db.sql`、`docreader/`），以及 DA-java `origin/common` e7596b979 的工具契约。

> **v1 实现与本设计的差异（2026-10-26）**
> - 数据库用的是 ParadeDB 镜像（pgvector + pg_search 中文 BM25），和 WeKnora 原实现一致。设计里推荐的 zhparser 需要联网自建镜像，部署机上没有这个条件。
> - 数据访问直接用 asyncpg，迁移是 SQL 文件，没有用 SQLAlchemy 和 Alembic。任务队列是自己实现的 Postgres `SKIP LOCKED` 队列，没有用 procrastinate。
> - 入库合并成一个 ingest 任务，依次执行 parse、chunk、embed；摘要和删除是单独的任务。
> - 文件存在本地卷上，还没接 S3/MinIO。
> - 操作类接口用路径形式，例如 `/reprocess`、`/cancel`、`/text`、`/url`。

## 1. 技术选型

| 项 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.11+ | 与 openJiuwen、Swarm、docreader 保持一致 |
| API | FastAPI | 管理接口、DA 工具接口（K7）、MCP（用官方 `mcp` SDK 的 streamable HTTP 挂载）共用一个进程 |
| 数据库 | PostgreSQL + pgvector + zhparser | DA 现在就用这套，镜像和运维经验都能复用。如果要贴近 WeKnora 原实现，可以改用 ParadeDB（`pg_search`，分词器 `chinese_lindera`），这一项待定 |
| ORM 与迁移 | SQLAlchemy 2（async）+ Alembic | |
| 任务队列 | 首选建在 Postgres 上的队列（procrastinate，或者自己用 `SKIP LOCKED` 实现任务表） | 写入文档记录和投递任务可以放在同一个事务里，从源头避免「有记录、没任务」。WeKnora 为此写了大量兜底代码，`ProcessDocument` 里就有不少。也少了一个组件。备选方案是 Celery + Redis |
| 对象存储 | S3 协议（MinIO） | 第一期只接这一种 |
| 模型调用 | 统一走 OpenAI 兼容协议：对话、向量、rerank、VLM、ASR | BGE-M3 和 rerank 用 DA 模块矩阵的服务 |

## 2. 进程和依赖

```
kb-api（无状态，可多副本）   管理 API ／ DA 工具接口 /v1/tools/* ／ MCP ／ 检索
kb-worker（按队列分组）
  parse    受解析服务容量限制，并发低
  embed    受向量服务吞吐限制
  enrich   摘要、图片 OCR 和描述，受大模型限流
  maint    删除、重建索引、巡检卡住的任务
外部服务
  docreader（直接复用 WeKnora 镜像，gRPC，固定版本）
  MinerU-NPU ／ PaddleOCR-VL ／ BGE-M3 ／ rerank（DA 模块矩阵）
  大模型（摘要）／ VLM（图片描述）
基础设施
  Postgres、MinIO；Redis 可选（限流、上传会话缓存）
```

## 3. 数据模型（第一期）

| 表 | 主要字段 |
|---|---|
| `tenants` | id, name |
| `service_keys` | 密钥哈希, tenant_id, 能力（manage/ingest/retrieve/reprocess）, kb 白名单, 是否允许通过请求头指定租户（只给 DA 这类可信服务） |
| `knowledge_bases` | tenant_id, name, description, config（解析规则、切分参数、向量模型及维度、rerank、摘要模型、VLM 开关） |
| `documents` | kb_id, tenant_id, 来源（file/url/text）, file_name, file_type, size, storage_key, content_hash, folder_path, title, **parse_status**（pending/processing/finalizing/completed/failed/deleting/cancelled，沿用 WeKnora 的取值）, stage, **attempt**, active_attempt, error_code, error_message, **summary（L0）**, summary_status, **l1_key（完整 Markdown 存在对象存储里）**, parse_meta |
| `chunks` | document_id, attempt, seq（阅读顺序）, chunk_type, content, context_header（标题路径）, start_offset, end_offset, line_start, **source_locators**（页码、bbox、sheet 和行、时间戳）, image_refs, content_hash, embedding（halfvec）, tsv（zhparser） |
| `document_images` | document_id, storage_key, caption, ocr_text, status |
| `upload_sessions` | 分块上传用：tenant_id, kb_id, file_name, size, sha256, 已收到的分块, status |
| `ingest_events` | 阶段进度和耗时，供插件页面展示，也用于审计 |

**索引**：
- 向量：按维度建 HNSW 部分索引，和 WeKnora 一样。检索带着租户和知识库过滤，所以要打开 pgvector 0.8 的 iterative scan，避免召回率下降。
- 全文：tsv 列上建 GIN 索引。

## 4. 管理 API（I2）

- 知识库：`POST/GET/PATCH/DELETE /v1/kbs`，以及 `GET /v1/kbs/{kb}/stats`。
- 导入：
  - 小文件：`POST /v1/kbs/{kb}/documents`（multipart）；
  - 大文件：`POST /v1/uploads` → `PUT /v1/uploads/{id}/parts/{n}` → `POST /v1/uploads/{id}/complete`，分块上传，最后校验 sha256；
  - URL：`POST /v1/kbs/{kb}/documents/url`；
  - 文本：`POST /v1/kbs/{kb}/documents/text`。
- 查询：
  - 文档列表：`GET /v1/kbs/{kb}/documents`，可按状态、目录、关键字过滤；
  - 文档详情：`GET /v1/documents/{id}`，包括状态、阶段、错误、摘要；
  - 分块：`GET /v1/documents/{id}/chunks`；
  - 分层内容：`GET /v1/documents/{id}/content?level=L0|L1|L2`；
  - 原文件：`GET /v1/documents/{id}/file`，返回签名 URL。
- 操作：`POST /v1/documents/{id}/reprocess`、`POST /v1/documents/{id}/cancel`、`DELETE /v1/documents/{id}`。
- 检索测试：`POST /v1/kbs/{kb}/search`。
- 进度：`GET /v1/kbs/{kb}/events`（SSE），给 Swarm 插件后端订阅。
- 模型端点：第一期写在部署配置里，知识库按名称引用，不做配置界面。

## 5. 入库流水线

### 5.1 状态机

```
pending → processing(parse → chunk → embed) → finalizing → completed
                    │ 每个阶段的边界都检查一次取消或删除                │
                    └→ failed（终态错误，或重试用完）       cancelled / deleting
summary_status：none → pending → processing → completed/failed（与主状态互不影响）
```

### 5.2 阶段

每个阶段都是一个独立任务，用 `(document_id, attempt)` 保证幂等。

| 阶段 | 做什么 | 从 WeKnora 移植什么 |
|---|---|---|
| S0 接收 | API 落文件，写 documents（pending，attempt=1），同一个事务里投递 parse 任务；按 content_hash 在知识库内去重 | `knowledge_create.go` |
| S1 解析 | 按文件类型和知识库的解析规则选路线：<br>• md/txt/csv/json 直接读；<br>• pdf/docx/xlsx/pptx/html 交给 docreader（builtin/markitdown/opendataloader）；<br>• pdf 可以改走 MinerU；<br>• 扫描件和图片走 PaddleOCR-VL；<br>• URL 先做防 SSRF 的抓取再解析；<br>• 音频走 ASR（是否放第一期待定）。<br>之后的处理：清洗 UTF-8，HTML 表格转成 GFM，图片转存到对象存储并改写引用，记录 source_blocks。Excel/CSV 在 L1 里保留完整表格（DA 契约要求）。**L1 完整 Markdown 落到对象存储** | `convert()`、`docparser/mineru_*.go`、`paddleocr_vl_*.go`、`html_table_normalizer.go`、`image_resolver.go`、`source_blocks.go` |
| S2 切分 | 移植 WeKnora 的切分器：<br>• 策略：auto/heading/heuristic/recursive；<br>• 默认块大小 512、重叠 80，支持 token 上限；<br>• 代码块和表格保护；<br>• 标题层级生成 context_header。<br>同时计算偏移和行号，把 source_blocks 映射成每个分块的定位 | `infrastructure/chunker/*`、`sourceloc/` |
| S3 向量化和索引 | 分批调用向量模型（批大小和并发都可配置；遇到 429 或 5xx 退避重试），写入 embedding 和 tsv。全部成功后在一个事务里切换 active_attempt，再删除旧版本的分块，这样重处理期间文档始终可检索 | `processChunks()`、`models/embedding/batch.go`、`repository/retriever/postgres` |
| S4 补充 | 不阻塞文档可检索：L0 摘要（大模型）；图片 OCR 和描述，写回图片分块后重新向量化。第二期在这里接 Wiki 和图谱抽取 | `ProcessSummaryGeneration`、`image_multimodal.go`、`config/prompt_templates` |

### 5.3 可靠性

- **错误分两类**：可重试的（超时、429、5xx）按阶段设重试上限并退避；终态的（格式不支持、文件损坏、模型没配）直接置为 failed，并写入 error_code。
- **巡检任务**：定期扫描卡住的文档，即阶段开始时间超过阈值的，重新投递或置为失败。
- **限流**：MinerU 和大模型这类稀缺资源用全局信号量限流（Postgres advisory lock 或 Redis 都可以），各队列的并发数也可以配置。
- **防旧任务覆盖**：重处理时 attempt 加一，旧任务发现 attempt 不一致就自动退出，避免覆盖新结果。WeKnora 在这里踩过的坑见 `ProcessDocument` 里关于 reparse 的注释。

## 6. 权限

中间件先把服务密钥解析成调用方身份。

| 调用方 | 能力 | 租户和范围 |
|---|---|---|
| Swarm 插件 | manage、ingest、retrieve | 租户固定 |
| Swarm MCP | 只读 | 租户和知识库都固定 |
| DA | retrieve、reprocess | 租户取自 `X-Tenant-Id`；范围取自 `X-Kb-Scope`，不能为空，每个 kb 都必须属于该租户，并且和密钥的白名单取交集 |

## 7. 代码结构

```
kb_service/
  api/        kbs.py documents.py uploads.py search.py da_tools.py(K7) mcp.py events.py
  core/       config.py auth.py tenancy.py errors.py logging.py
  db/         models.py repositories/ alembic/
  ingest/     pipeline.py(状态机) tasks.py stages/{parse,chunk,embed,enrich}.py housekeeping.py
  parsers/    docreader_client.py(gRPC) mineru.py paddleocr_vl.py simple.py url_fetch.py html_tables.py images.py locators.py
  chunking/   splitter.py heading.py heuristic.py recursive.py tokens.py protected_spans.py
  models/     openai_compat.py(chat/embedding/rerank/vlm/asr) batching.py ratelimit.py
  retrieval/  vector.py fulltext.py hybrid.py rerank.py reader.py(read/grep/list)
  storage/    s3.py
  compat_da/  views.py(L0/L1/L2、pageId、锚点 {docId}:{type}:{index}) shapes.py
tests/        unit/ contract/(DA parity-goldens) diff/(和 WeKnora 原版实例做差分)
```

## 8. 开发顺序

1. **先跑通一条最简链路（2–3 周）**：表结构、密钥鉴权、上传、docreader 解析、切分、向量化、索引、简单向量检索。用一个 PDF 走通全程。
2. **补齐入库（3–4 周）**：MinerU 和 PaddleOCR-VL、图片处理、溯源定位、L0 摘要、重处理、取消、删除、巡检、分块上传、进度事件。
3. **补齐检索和对外接口（3–4 周）**：混合检索加 rerank、K7 DA 工具契约、MCP、管理接口收尾。和 WeKnora 原版实例做差分测试，用 parity-goldens 跑契约测试。

## 9. 待定事项

1. 全文检索用 zhparser，还是 ParadeDB 的 pg_search。
2. 任务队列用 procrastinate、自己实现的 Postgres 任务表，还是 Celery + Redis。
3. 音频 ASR 放第一期还是第二期。
4. 每个知识库只用一个向量模型；换模型就等于全量重建索引。
5. docreader 直接用 WeKnora 镜像，并固定版本。
