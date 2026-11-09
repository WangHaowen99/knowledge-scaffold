# deploy/ 只构建和部署四个必需服务

## Context

`deploy/` 下的构建和部署入口同时承担 Consumer、Modeling Workbench、知识包 Hub、Onto Modeling 和独立评测服务。Workbench 已是可选组件，却仍占用完整包的镜像、`deploy-stack.sh` 的大部分编排逻辑，以及三个安装脚本中最复杂的迁移和回滚分支。

## Decision

- `deploy/build.sh`、`deploy/deploy.sh`、`deploy/deploy-stack.sh` 和三个安装脚本只处理 Consumer、Onto Modeling、知识包 Hub、独立评测服务。`--modeling-only`、`--modeling-package`、`--modeling-tag`、`--with-eval`、`--no-eval` 移除，传入时报错，不静默忽略。
- Modeling Workbench 由 `modeling/build.sh` 和 `modeling/deploy/` 单独打包和部署。`deploy/docker-compose.modeling.yml` 暂留，供 `modeling/deploy/deploy.sh` 的源码部署使用。
- 源码模式的 `MODELING_MCP_TOKEN_SECRET` 改由 `modeling/deploy/deploy.sh` 自己生成，它只有 Workbench 使用。
- 保留 Consumer 侧的 `MODELING_SERVICE_TOKEN`：单独部署 Workbench 时，`modeling/deploy/deploy.sh` 会校验运行中的 Consumer 带有相同的值，保留后无需重启 Consumer。
- 保留 Onto Modeling 的 `ONTO_WEB_MODELING_URL` 默认值，已有或单独部署的 Workbench 无需改配置即可对接。
- 升级时继续读取已有 Workbench 占用的宿主端口，避免 Onto Modeling 抢占，但不停止、不备份、不迁移它。
- `install-consumer-modeling.sh cce` 的联合部署入口不在本次范围内，保持不变。

## Alternatives considered

- 继续把 Workbench 作为可选包：分支和迁移逻辑仍留在 `deploy/`，无法收敛到四个发布包。
- 同时停止生成 Consumer 的 `MODELING_SERVICE_TOKEN`：配置更干净，但之后单独部署 Workbench 必须先改 Consumer 配置并重启。
- 现在把 `docker-compose.modeling.yml` 移到 `modeling/deploy/`：需要同步调整构建上下文和 modeling 的测试，与本次目标无关。

## Consequences

- 完整包不再包含 `mw-modeling` 和 `mw-modeling-agent` 镜像，`deploy-stack.sh up` 只启动 Consumer，`down` 也不再停止 Workbench 和评测库。
- 旧合并部署升级后，Workbench 继续以旧版本运行，后续升级需要走 `modeling/` 的独立流程。
- 未单独部署 Workbench 的环境里，Onto Modeling 依赖 Workbench 的数据评测和 MCP 调用会因连不上 `http://mw-modeling:5100` 而失败。
