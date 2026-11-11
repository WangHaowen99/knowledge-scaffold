# 0095 知识包版本对比：发布侧按本项目版本号取包，子进程解析 ZIP

## Context

**目标**：项目所有者看两份知识包差在哪——两个发布版本之间，或某个发布版本和手上的一份 ZIP（别人改过的包、导出的包）。
成功标准：逐文件给出新增、删除、修改、未变化，能看正文差异；恶意或畸形的 ZIP 不拖垮主服务、不写出比较目录。

**现状**：develop（!585）已经实现：`package_compare` 服务、解析子进程、八个接口和前端对话框。发布侧的来源是 Hub 的
`{ontology_id, release_id}`，用用户令牌下载，并且每次读结果都回 Hub 核对一次读权限。本分支的发布记录、发布包接口都按项目内的
版本号（`/releases/{version}`），取包统一走 `publish.release_package(manager, pid, version)`；P1 把它改成 Hub 支撑，签名不变。

**基本约束**

1. 上传的 ZIP 不可信：路径穿越、符号链接、压缩炸弹、超大条目、加密条目、YAML 炸弹都可能出现。
2. 解析出问题时，影响只能落在这一次对比上，不能卡住或撑爆主进程。
3. 只读：对比不改项目内容，上传的包不进工作区。
4. 项目内容只有所有者能看（契约 1.4）；比较数据按登录用户隔离。
5. 发布的版本不可变；Hub 缓存或留存可能被清，对比结果在有效期内要能一直看。
6. 资源有界：单用户、全局的临时空间与并发任务数都有上限，过期自动清。

## Decision

**推导**

- 约束 1、2：解析放进子进程（`package_compare_worker`），限地址空间和 CPU 时间；条目数、单文件大小、解压总量、路径长度、manifest
  深度都在解析时就拦。超时或取消直接终止子进程。沿用 develop 的实现，ZIP 校验与限额一条不改。ADR-0076 管的是资料原件，知识包 ZIP
  不在其内；这里的隔离是子进程加资源上限。
- 约束 4：发布版本属于项目，所有者守卫已经决定能不能看；再回 Hub 核对读权限是第二套鉴权，两边口径可能不一致。所以发布侧来源改为
  `{kind: "release", version}`，经 `publish.release_package` 取包，每个接口先过 `require_owner`。去掉 develop 的
  `ensure_sources_readable`，也不再带用户令牌。
- 约束 5：发起对比时把发布包复制成比较目录下的 `left.zip`、`right.zip`（冻结），之后的解析、正文差异只读这一份。
- 约束 3、6：上传输入、结果、请求记录都在 `DATA_DIR/package-comparisons/<用户摘要>/`，24 小时过期；配额与并发上限沿用 develop
  （环境变量 `ONTO_COMPARE_*`）。多 worker 部署时只有抢到服务锁的进程提供对比和定时清理。
- 跨包：两侧 manifest `id` 不同时要确认。任务在后台判定，失败码 `compare_cross_package`，前端给「仍要对比」，带
  `allow_cross_package` 重新发起；前端不必事先知道两侧的包标识。

**做法**

1. `services/package_compare.py`、`package_compare_worker.py`：自 develop 移植；发布来源改为版本号，错误补上契约码
   （`compare_expired`、`compare_not_ready`、`compare_cross_package`、`compare_input_in_use` 等）；任务失败只把工作区错误的原文给界面。
2. `routers/package_compare.py`：PC-01～PC-08，挂在 `/api/v1/projects/{id}/package-comparisons`；文件列表用游标分页（契约 1.6），
   时间用 RFC 3339。
3. `main.py` 生命周期：抢服务锁，先把上次没跑完的任务改判失败，再每 5 分钟清理一次；停机时释放。
4. 前端 `pages/publish/PackageCompare.jsx` 抽屉，从发布页「发布记录」打开。

## Alternatives considered

- **照搬 develop 的 Hub 引用（`ontology_id + release_id` + 每次核对 Hub 读权限）**：能比 Hub 上别的包的版本，但界面只列本项目的
  发布记录，别的包可以下载后上传来比；代价是 P4 直接依赖 `hub_packages` 的下载、关联、调用三个内部函数，本地模式（无统一账号）下
  不能用，还多一套鉴权。
- **不冻结、每次从 `release_package` 取**：省一份磁盘，但 P1 的 Hub 缓存被清后正文差异要重新下载，慢且可能失败。冻结的空间计入
  用户配额，24 小时后清掉。
- **在主进程里解析**：省掉子进程启动（约 1 秒），但压缩炸弹、YAML 炸弹会直接打到主服务。

## Consequences

- 不能直接选 Hub 上别的包的发布版本做对比，要先下载再上传。
- 本地模式也能用（develop 要求统一账号登录）。
- 旧路径 `/api/projects/{id}/package-comparisons/*` 不做兼容映射：调用方只有 develop 的旧前端，本分支不带它。
- 一次对比最多复制两份发布包（各不超过 `ONTO_COMPARE_MAX_ARCHIVE_BYTES`，默认 50 MiB），计入用户临时空间。
