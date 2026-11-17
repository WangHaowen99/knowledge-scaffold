# knowledge-provider/v1

知识后端（provider）对 kb-gateway 提供的接口。kb-gateway 在它之上实现知识库页的管理接口、DA 工具接口（`/v1/tools/*`）和 MCP，消费方只连 gateway。

参考实现是 `service/kb-service`：本契约的路径和数据形状就是 kb-service 现有的 `/v1` 管理接口，kb-service 只额外提供 `GET /v1/capabilities`。其他后端（WeKnora、DA 知识管线）通过适配服务实现同样的路径。

## 1. 角色

| 角色 | 说明 | v1 状态 |
|---|---|---|
| 基础后端（`role: base`） | 拥有知识库数据，负责入库、索引和检索。每个知识库只属于一个基础后端 | 已实现 |
| 增强服务（`role: enhancement`） | 不拥有原始数据，接管少量接口或挂在检索前后、入库事件上 | 仅预留字段，gateway 拒绝注册，见第 8 节 |

## 2. 通用约定

- **传输**：HTTP/1.1，JSON（UTF-8）。上传用 `multipart/form-data`，正文读取返回 `text/markdown` 或 `application/json`。
- **鉴权**：gateway 用 `Authorization: Bearer <后端凭据>` 调用后端，凭据由 gateway 部署配置按环境变量名引用。后端不接受浏览器直连。
- **租户**：gateway 带 `X-Tenant-Id`。v1 只有一个租户，后端可以忽略。
- **ID**：知识库、文档、分块、Wiki 页面的 ID 对 gateway 是不透明字符串，但必须满足两条：
  1. 以该后端注册时配置的 `id_prefix` 开头（最多一个后端的前缀为空，通常是 kb-service；它和「默认后端」无关，默认后端只决定新建知识库建在哪里）。适配服务负责在出参加前缀、入参去前缀。gateway 按最长前缀把请求路由到后端。
  2. 不含 `:`、`,`、`/` 和空白。`:` 留给 DA 的 pageId 和锚点（`{docId}:L1`、`{docId}:{type}:{index}`），`,` 留给 `X-Kb-Scope`。
- **错误**：非 2xx 时返回 `{"error": {"code": "<机器可读>", "message": "<给人看的中文说明>"}}`。404 用 `not_found`，能力不支持用 501 `unsupported`。错误对象可以另带少量标量字段（如重复上传的 `existingDocId`），gateway 原样透传给调用方（最多 10 个，只透传字符串、数字、布尔和 null）。各接口用到的错误码见第 2.1 节。
- **分页**：列表接口用 `limit` / `offset`，响应带 `total`。每个接口的最坏结果大小见第 5 节的上限列。
- **时间**：ISO 8601，带时区。

### 2.1 错误码

| 错误码 | HTTP | 出现在 | 含义 |
|---|---|---|---|
| `not_found` | 404 | 所有按 ID 访问的接口 | 对象不存在 |
| `unsupported` | 501 | 可选能力 | 后端没有声明该能力 |
| `duplicate` | 409 | 入库 | 同一知识库里已有内容相同的文档；错误对象另带 `existingDocId`、`existingStatus`（已有文档的 ID 和解析状态，查不到时为 null） |
| `duplicate_name` | 409 | `POST /v1/kbs`、`PATCH /v1/kbs/{kb_id}` | 已有同名知识库 |
| `bad_encoding` | 400 | 入库文本类文件 | 认不出文字编码，或是二进制文件。适配服务转码时发现，在入库时就拒绝；kb-service 在异步解析时才发现，文档以 `failed` 结束（见下） |
| `url_not_allowed` | 400 | `POST .../documents/url` | 网址指向回环、内网、链路本地等内部或私有地址（含特殊用途 IPv6 前缀里嵌的这类地址），或带用户名口令、非 http(s) 协议。提示里不回显解析出的地址 |
| `url_unresolvable` | 400 | `POST .../documents/url` | 域名解析失败。无法确认它不指向内部地址，所以一律拒绝 |
| `bad_pattern` | 400 | `POST /v1/grep` | 正则写法会导致灾难性回溯（嵌套量词、可重叠的分支） |
| `pattern_timeout` | 400 | `POST /v1/grep` | 正则在时限内没有匹配完 |
| `search_unavailable` | 503 | `POST /v1/search` | 检索整体不可用（如向量模型和全文检索都失败）。部分降级时不报错，见第 5.4 节的 `warnings` |
| `checksum_mismatch` | 400 | 分块上传 | 大小或 sha256 不符 |

文档解析失败不是 HTTP 错误：文档的 `parse_status` 为 `failed`，`error_message` 写给人看的原因；kb-service 另在 `error_code` 里给出机器可读的原因，如 `mineru_error`（调用 MinerU 识别图片或扫描件失败）、`asr_error`（调用语音转写失败）、`bad_encoding`、`parse_failed`。

## 3. `GET /v1/capabilities`

返回后端声明，格式见 `capabilities.schema.json`。gateway 启动时读取，之后每 60 秒刷新一次。例：

```json
{
  "contract": "knowledge-provider/v1",
  "role": "base",
  "provider": {"type": "weknora", "version": "0.1.0", "label": "WeKnora"},
  "features": ["kbs.write", "ingest.file", "ingest.text", "ingest.url", "documents.delete",
               "documents.reprocess", "documents.cancel", "wiki.read"],
  "file_types": ["pdf", "docx", "md", "txt", "xlsx"],
  "limits": {"max_upload_mb": 100, "max_top_k": 50},
  "config_schema": {"type": "object", "properties": {}}
}
```

## 4. 能力（features）

核心能力不写进 `features`，每个后端都必须实现。

| 能力 | 接口 | 必选 |
|---|---|---|
| （核心）知识库只读 | `GET /v1/kbs`、`GET /v1/kbs/{kb_id}` | 是 |
| （核心）文档只读 | `GET /v1/kbs/{kb_id}/documents`、`GET /v1/documents/{doc_id}` | 是 |
| （核心）正文 | `GET /v1/documents/{doc_id}/content?level=L0\|L1` | 是 |
| （核心）分块 | `GET /v1/documents/{doc_id}/chunks` | 是 |
| （核心）检索 | `POST /v1/search` | 是 |
| `kbs.write` | `POST /v1/kbs`、`PATCH /v1/kbs/{kb_id}`、`DELETE /v1/kbs/{kb_id}` | |
| `kbs.reprocess` | `POST /v1/kbs/{kb_id}/reprocess` | |
| `ingest.file` | `POST /v1/kbs/{kb_id}/documents`（multipart） | |
| `ingest.text` | `POST /v1/kbs/{kb_id}/documents/text` | |
| `ingest.url` | `POST /v1/kbs/{kb_id}/documents/url` | |
| `documents.delete` | `DELETE /v1/documents/{doc_id}` | |
| `documents.reprocess` | `POST /v1/documents/{doc_id}/reprocess` | |
| `documents.cancel` | `POST /v1/documents/{doc_id}/cancel` | |
| `documents.file` | `GET /v1/documents/{doc_id}/file` | |
| `content.L2` | `GET /v1/documents/{doc_id}/content?level=L2`（JSON） | |
| `content.structure` | `GET /v1/documents/{doc_id}/structure`：L1 的分段读取、锚点和目录（DA 的 `expand` 依赖它） | |
| `grep` | `POST /v1/grep`：在 L1 正文里逐行查找（DA 的 `doc_grep`、MCP 的 `grep_documents` 依赖它） | |
| `uploads` | `POST /v1/uploads`、`PUT /v1/uploads/{upload_id}/parts/{index}`、`POST /v1/uploads/{upload_id}/complete`：分块上传 | |
| `wiki.read` | `GET /v1/kbs/{kb_id}/wiki/{status,pages,page,graph}` | |
| `wiki.write` | `PUT/DELETE /v1/kbs/{kb_id}/wiki/page`、`POST .../wiki/{rebuild,finalize}` | |
| `questions` | `POST /v1/documents/{doc_id}/questions/regenerate`、`PUT /v1/chunks/{chunk_id}/questions` | |
| `profile` | `POST /v1/kbs/{kb_id}/profile/{generate,apply}` | |
| `config.meta` | `GET /v1/config/meta`（kb-service 专用的设置元数据） | |
| `tools.da` | 后端自己实现 `POST /v1/tools/{kb_search,expand,doc_grep,wiki_browse,reprocess_document}` | |

gateway 只做转发、格式转换和多后端结果合并，不读取正文，也不自己实现知识能力：

- 请求只转给声明了对应能力的后端，否则直接返回 501；一次查询涉及多个后端时，没有该能力的后端被跳过并在结果里注明。
- DA 工具：范围全部落在一个声明了 `tools.da` 的后端上时原样转发；否则 gateway 把 DA 工具调用换算成对应的统一接口（`search`、`content.structure`、`grep` 等），只转换数据格式。
- 不同后端可以共用的实现（L1 窗口、锚点、目录、逐行查找）在 kb-service 的 `kb_service.text_structure` 里，只依赖标准库，适配服务按无依赖方式安装 kb-service 后直接引用；分块上传的会话管理在 `provider-kit` 里。

## 5. 接口与数据形状

字段名用 snake_case。下列字段是 gateway 依赖的部分，后端可以多返回字段（gateway 原样透传给知识库页）。

### 5.1 知识库

`GET /v1/kbs` → `{"items": [Kb]}`，上限：全部知识库，不分页。

`Kb`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | |
| `name` | string | |
| `description` | string | |
| `created_at` / `updated_at` | string | |
| `stats` | object | 至少 `documents`、`completed`、`processing`、`failed`（int）；可另带 `cancelled`，已取消的文档不算作 `completed` |
| `config` | object | 后端私有配置，形状由 `config_schema` 描述 |

`POST /v1/kbs`：`{"name", "description", "config"?}` → 201 `Kb`。`PATCH` 同形状、字段可选。`DELETE` → 204。

### 5.2 文档

`GET /v1/kbs/{kb_id}/documents?status=&q=&limit=100&offset=0` → `{"items": [Document], "total": int}`，`limit` 上限 500。

`Document`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id`、`kb_id` | string | |
| `title`、`file_name`、`file_type` | string | `file_type` 为小写扩展名 |
| `file_size` | int \| null | |
| `folder_path` | string | |
| `source_type` | string | `file` / `text` / `url` |
| `parse_status` | string | `pending` / `processing` / `completed` / `failed` / `cancelled` |
| `error_message` | string \| null | |
| `summary` | string \| null | L0 |
| `chunk_count` | int | 分块数。适配服务在后端不直接给出时自己统计填写，知识库页据此显示 |
| `l1_chars` | int \| null | L1 正文长度，未知时为 null |
| `created_at`、`processed_at` | string \| null | |

入库：

- `POST /v1/kbs/{kb_id}/documents`：multipart，字段 `file`（必选）、`folder_path`、`process_overrides`（JSON 字符串）→ 201 `Document`。
- `POST .../documents/text`：`{"title", "text", "folder_path"?, "process_overrides"?}` → 201 `Document`。
- `POST .../documents/url`：`{"url", "folder_path"?, "process_overrides"?}` → 201 `Document`。

入库是异步的，返回时 `parse_status` 一般为 `pending` 或 `processing`。

### 5.3 正文与分块

- `GET /v1/documents/{doc_id}/content?level=L0` → `text/markdown`，文档摘要，可以为空串。
- `level=L1` → `text/markdown`，完整的结构化 Markdown 正文。上限由后端决定，gateway 自己分页给 DA。
- `level=L2` → `application/json`，解析定位数据（可选能力 `content.L2`）。
- `GET /v1/documents/{doc_id}/chunks?offset=0&limit=20` → `{"items": [Chunk], "total": int, "offset": int}`，`limit` 上限 200，按阅读顺序。

`Chunk`：`id`、`seq`（从 0 起的阅读顺序）、`chunk_type`（`text` / `table` / `image` / …）、`content`、`context_header`（标题路径，可为空串）、`line_start`（在 L1 中的起始行，未知为 null）、`anchor_id`（可为 null）、`source_locators`（`[{page?, bbox?, sheet?, row_start?, row_end?, slide?}]`）。

### 5.4 检索

`POST /v1/search`：

```json
{"query": "…", "kb_ids": ["…"], "top_k": 8, "min_score": null, "rerank": null, "include_wiki": true, "options": null}
```

- `kb_ids` 为空时检索该后端全部知识库。`top_k` 上限 50。
- `min_score` 按 `relevance` 过滤，不按 `score`；`relevance` 始终为 null 的后端忽略它。
- 响应 `{"query": "…", "items": [Hit], "warnings"?: ["…"]}`，按相关度降序。`warnings` 只在部分降级时出现（如向量检索失败、只用了全文检索），每条是给人看的说明；检索整体不可用时返回 503 `search_unavailable`。

`Hit`：`chunk_id`、`doc_id`、`kb_id`、`seq`、`chunk_type`、`title`（文档标题）、`file_name`、`context_header`、`content`、`score`（后端自己的分数，不同后端之间不可比）、`relevance`（0–1，可为 null）、`line_start`、`anchor_id`、`locators`，可选 `matched_by`（命中途径列表，如 `vector`、`keyword`、`exact`；`exact` 表示查询里的编号、型号等在正文里原样出现，这类命中排在前面）。Wiki 命中的 `chunk_type` 为 `wiki_page`，另带 `wiki_slug`、`wiki_type`、`wiki_sources`。

跨后端检索由 gateway 并行调用各后端，再按排名融合（RRF，k=60）；gateway 不比较各后端的 `score`。

### 5.5 结构化读取、逐行查找、分块上传

`GET /v1/documents/{doc_id}/structure?offset=0&limit=30000&heading=`（`content.structure`）

- 不带 `heading`：从 `offset` 起最多 `limit` 个字符，结尾尽量落在换行处；`limit=0` 只返回目录和长度。
- 带 `heading`：返回该标题下的整节；找不到时 404。
- 响应：`{"content", "offset", "total_chars", "has_more", "next_offset"?, "outline": ["标题", "  子标题"], "anchors": [{"id": "{docId}:{type}:{index}", "type", "line_start", "preview", "section_title"}]}`。`limit` 上限 200000。

`POST /v1/grep`（`grep`）：`{"pattern", "kb_ids"?, "max_results": 50, "max_lines_per_doc": 10}`。`pattern` 不是合法正则时按字面量查找，长度上限 500；`kb_ids` 为空表示该后端全部知识库。响应：`{"pattern", "search_mode": "regex|literal", "total_matches", "total_documents", "has_more", "matches": [{"doc_id", "kb_id", "title", "file_name", "lines": [{"line_number", "text"}]}]}`。

分块上传（`uploads`）：

- `POST /v1/uploads`：`{"kb_id", "file_name", "file_size", "sha256"?, "folder_path"?, "process_overrides"?}` → 201 `{"upload_id", "part_size", "part_count"}`。`upload_id` 同样带后端的 ID 前缀。
- `PUT /v1/uploads/{upload_id}/parts/{index}`：请求体是该分块的原始字节。
- `POST /v1/uploads/{upload_id}/complete` → 201 `Document`；大小或 sha256 不符时 400 `checksum_mismatch`。

### 5.6 Wiki（`wiki.read` / `wiki.write`）

路径和形状同 kb-service 的 `api/wiki_api.py`：

- `GET /v1/kbs/{kb_id}/wiki/status` → `{"enabled", "pages": {type: n}, "documents": {...}, "pending_jobs", "errors"}`
- `GET .../wiki/pages?type=&q=&limit=200&offset=0` → `{"items": [{id, slug, title, page_type, summary, ...}], "total"}`，`page_type` 取 `summary` / `entity` / `concept` / `index`
- `GET .../wiki/page?slug=` → 页面详情，含 `content`、`links_to`、`linked_from`、`sources`、`source_doc_ids`
- `GET .../wiki/graph?center=&limit=300` → `{"nodes": [...], "edges": [...]}`
- 知识库没开 Wiki 时，`status` 返回 `"enabled": false`，`pages` 和 `graph` 返回空结果（不是错误），`page` 返回 404。
- `slug` 可以含 `/`（如 `entity/pump`），由后端自己校验；gateway 不转发含 `..`、`?`、`#`、`%` 的 slug。

## 6. 健康检查

`GET /healthz` → 200 `{"status": "ok"}`，供容器编排做存活检查。gateway 定时（默认 60 秒）拉取 `GET /v1/capabilities` 判断后端是否可用：连不上、返回不是 JSON 或不符合本接口集的，标为不可用，它的知识库不出现在列表里，其他后端不受影响。

## 7. 合规检查

`contracts/knowledge-provider/v1/tck`（后续补充）对一个后端跑只读用例和可选写用例；新后端接入前要跑通核心部分。

## 8. 增强服务（预留）

v1 只固定字段，不实现路由。`capabilities` 里 `role: enhancement` 的服务会被 gateway 拒绝注册，并在日志里说明原因。预留字段：

| 字段 | 说明 |
|---|---|
| `provides` | 接管的能力，如 `["wiki.read", "wiki.write", "profile"]`。同一知识库上同一能力只能有一家接管 |
| `hooks` | 挂载点：`search.pre`（改写查询）、`search.post`（加工命中）、`document.completed`、`document.deleted`、`kb.deleted` |
| `requires` | 需要基础后端提供的能力，如 `["chunks", "content.L1", "chunks.questions.write"]`；基础后端不满足时拒绝挂载 |
| `capability_groups` | 必须一起接管或一起保留的能力组，如 DA 知识管线的 `["wiki.read", "timeline", "graph"]` |
| `ui` | 自定义界面挂载点，如 `[{"slot": "kb.tab", "title": "…", "entry": "/ui/index.html"}]`，由知识库页用沙箱 iframe 加载 |

预留的基础后端写入能力：`chunks.questions.write`、`documents.summary.write`，供增强服务写回会影响检索的数据。
