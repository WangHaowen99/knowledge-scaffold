# 删除 Modeling Workbench，NL2SQL 服务与评测库单独部署

## Context

`deploy/` 的构建和部署已不包含 Modeling Workbench（`mw-modeling`、`mw-modeling-agent`），见 [deploy-four-required-services.md](deploy-four-required-services.md)。它的源码、构建和部署脚本仍留在 `modeling/` 下，与 Onto Modeling 混在一起，造成三个问题：

- 仓库要继续维护一套不再交付的服务：Fastify 后端、前端、modeling-agent、CCE 联合部署脚本和近 900 个文件。
- Onto Modeling 还保留三条依赖 Workbench 的旧链路：旧版数据评测（含历史记录查看、停止和对账）、远端 Modeling 项目的 MCP 关联与清理、建模侧求解验证执行器。新的评测、求解验证和本地 MCP 都已有独立实现，这三条链路在没有部署 Workbench 的环境里只会连接失败。
- 评测用的 NL2SQL 服务和 MySQL 原本复用 Workbench 镜像，通过 compose 的 `eval` profile 启动。删除 Workbench 后，它们需要自己的部署方式。

## Decision

- 删除 `modeling/` 下 Workbench 的全部代码、文档、测试和部署脚本，以及 `deploy/docker-compose.modeling.yml`。`modeling/` 下保留 `onto-modeling/`、`nl2sql-service/`，以及 `deploy/cce/`：后者已改为部署 Onto Modeling、知识包 Hub 和独立评测的 CCE 链路，不再部署 Workbench，`install-consumer-modeling.sh cce` 入口随之保留。
- 新建 `modeling/nl2sql-service/`：NL2SQL 服务从 Workbench 迁出，代码只依赖 `mysql2`，接口和环境变量名保持不变；与 MySQL 8 放在同一个 compose 项目里，由 `deploy.sh` 一键部署。它不加入 `deploy/` 的构建和部署。查询改用只读账号 `nl2sql_reader`（对业务库只读，收回 `mysql`、`sys` 库权限，无文件读写权限），并在 SQL 校验中拦截读写服务器文件和访问系统库的写法；root 只用于导入数据。MySQL 不加入 consumer 网络，两个宿主端口分别设置绑定地址，默认只绑本机。
- 从 Onto Modeling 删除依赖 Workbench 的三条旧链路，以及 `ONTO_WEB_MODELING_URL` 等相关配置；`deploy/` 的脚本不再为 Onto Modeling 写入 Workbench 地址。
- 旧版评测的数据源配置保留只读读取：发布知识包时，其中引用的上传数据表仍作为数据映射的表头来源；删除项目时继续清除配置中的连接凭据。
- 保留 `migrate-from-modeling.sh` 数据迁移工具，以及部署脚本中避开旧 Workbench 宿主端口的检查，供现场仍在运行的旧实例使用。

## Alternatives considered

- 保留 Workbench 源码，只停止交付：仓库继续承担它的测试和依赖维护，Onto Modeling 的旧链路仍会在无 Workbench 的环境里失败。
- 把 NL2SQL 和 MySQL 加入 `deploy/` 的整栈部署：它们只服务评测数据准备，不是四个必需服务之一，加入后会扩大整栈的部署和升级范围。
- 连同旧评测数据源配置一起删除：老项目发布时会失去这部分表头来源，数据映射结果随之改变，且没有替代的配置入口。

## Consequences

- 仓库不再能构建或部署 Workbench；现场旧实例不受影响，可用 `migrate-from-modeling.sh` 把数据迁入 Onto Modeling。
- Onto Modeling 不再提供 `/api/projects/{pid}/eval/*` 接口和旧评测记录入口，工作区里已有的 `reports/eval-*.md` 报告保留。管理端的 Modeling 清理任务接口一并移除。
- NL2SQL 与 MySQL 的容器名改为 `nl2sql-service` 和 `nl2sql-mysql`，使用新的数据卷，不继承旧 `mw-eval-mysql` 的数据；迁移步骤见 `modeling/nl2sql-service/README.md`。
- 现场仍在运行旧 Workbench 时，Onto Modeling 升级后与 Workbench 的项目关联不再维护：旧版本创建的项目元数据里存有 `modeling_project_id`，删除这类项目不再级联删除 Workbench 中的对应项目；数据目录下 `modeling-cleanup-tasks.json` 里未完成的远端清理任务不再执行。需要清理时，在 Workbench 侧手工删除对应项目，或在迁移完成后停用整个 Workbench 实例；该文件可在确认后删除。
- consumer 仍会生成并接受 `MODELING_SERVICE_TOKEN`，仓库内已没有调用方，是否移除需另行决定。
