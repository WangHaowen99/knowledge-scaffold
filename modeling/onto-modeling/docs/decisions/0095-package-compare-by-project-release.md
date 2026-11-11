# 0095 知识包版本对比：照 develop 引用 Hub 发布版本，子进程解析 ZIP

## Context

**目标**：项目所有者看两份知识包差在哪——Hub 上的两个发布版本之间，或某个版本和手上的一份 ZIP（别人改过的包、导出的包）。
成功标准：逐文件给出新增、删除、修改、未变化，能看正文差异；恶意或畸形的 ZIP 不拖垮主服务、不写出比较目录。

**现状**：develop（!585）已经实现 `package_compare` 服务、解析子进程、八个接口和前端对话框：发布版本按 Hub 的
`{ontology_id, release_id}` 引用，用用户令牌下载，读结果时回 Hub 复核读权限。用户要求（2026-11-12）：除评测优化外，行为与界面
元素都与 develop 一致，只有样式按新 UI、接口挂在 `/api/v1` 下。

**修订记录**：第一轮（2026-11-11）曾改成按本项目版本号取包、只看项目所有者、游标分页并删减界面元素；第二轮按上面的要求全部改回
develop，本文按第二轮重写。

**基本约束**

1. 上传的 ZIP 不可信：路径穿越、符号链接、压缩炸弹、超大条目、加密条目、YAML 炸弹都可能出现。
2. 解析出问题时，影响只能落在这一次对比上，不能卡住或撑爆主进程。
3. 只读：对比不改项目内容，上传的包不进工作区。
4. Hub 是正式版本的权威（ADR-0098），读权限以 Hub 为准；PR555 的项目只有所有者能用（契约 1.4）。
5. 行为与 develop 一致；接口风格（`/api/v1`、错误体）按 PR555。
6. 资源有界：单用户、全局的临时空间与并发任务数都有上限，过期自动清。

## Decision

- 约束 1、2：沿用 develop 的子进程解析（`package_compare_worker`），限地址空间和 CPU 时间，条目数、单文件大小、解压总量、路径长度、
  manifest 深度在解析时就拦，超时或取消直接终止子进程。ADR-0076 管的是资料原件，知识包 ZIP 不在其内。
- 约束 4、5：服务层是 develop 原文（只换中文注释、补契约错误码）：
  - 发布来源 `{kind: "release", ontology_id?|hub_package_id?, release_id}`，缺包标识时取项目关联的 Hub 包，当前用户在 Hub 上可见的版本都能比；
  - 发起时用发起人的令牌 `hub_packages.download` 并冻结成 `left.zip`、`right.zip`；读结果、文件列表、正文差异、重放时 `ensure_sources_readable` 回 Hub 复核；
  - 身份是 Hub 主体（[签发方, sub]），本地模式没有 Hub 身份，401。
- 路由层（`routers/package_compare.py`）：参数、分页、响应照 develop；另过一道所有者守卫（PR555 单人所有权）。
  本项目版本号的简写 `{kind: "release", version}` 在路由里按发布记录标签里的 Hub 身份换成 develop 引用，服务层不感知。
- 跨包：两侧 manifest `id` 不同要确认。照 develop，前端发起前比较两侧包标识并要求「确认跨包对比」，后端没带 `allow_cross_package` 时任务失败。
- 约束 6：上传输入、结果、请求记录都在 `DATA_DIR/package-comparisons/<主体摘要>/`，24 小时过期；配额与并发上限沿用 develop（`ONTO_COMPARE_*`）。
  多 worker 部署时只有抢到服务锁的进程提供对比和定时清理（`main.py` 生命周期）。同一进程再次启动时换新线程池，测试里多次跑生命周期不会停掉对比。
- 前端 `pages/publish/PackageCompare.jsx` 的元素、文案与行为照 develop 的 `PackageCompareDialog`，用 `src/ui` 的组件与样式；纯函数沿用
  develop 的 `lib/packageCompareView.js` 与 `lib/latestRequest.js`。

## Alternatives considered

- **按本项目版本号取包、只看项目所有者（第一轮做法）**：不依赖 Hub 内部函数，本地模式也能比；但比不了 Hub 上别的包，读权限与 Hub 口径脱节，
  和 develop 行为不一致。用户要求一致，改回。
- **在主进程里解析**：省掉子进程启动（约 1 秒），但压缩炸弹、YAML 炸弹会直接打到主服务。

## Consequences

- 对比要统一账号登录；本地模式下这个入口不可用。
- 旧路径 `/api/projects/{id}/package-comparisons/*` 不做兼容映射：调用方只有 develop 的旧前端，本分支不带它。
- 没走 Hub 的本地导出（manifest `id` 是项目 ID）与 Hub 发布包（`id` 是 Hub 包标识）比较时算跨包，要先确认。
- 一次对比最多冻结两份发布包（各不超过 `ONTO_COMPARE_MAX_ARCHIVE_BYTES`，默认 50 MiB），计入用户临时空间。
