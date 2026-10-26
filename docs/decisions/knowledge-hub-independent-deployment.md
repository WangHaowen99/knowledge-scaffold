# 知识包 Hub 独立部署

## Context

Hub 写在 onto-modeling 的源码与发布 Compose 里：与 onto-modeling 同一 compose 项目（发布为 `onto-web`）、同一离线包、同一 external-auth/stack override。onto-modeling 重建、升级或 `--remove-orphans` 都会牵动 Hub；Hub 的镜像只能随 onto-modeling 包交付，分离安装脚本也只能“顺带”拿到 Hub。Hub 是独立服务（评测、消费也会调用它），部署生命周期需要和 onto-modeling 解耦。

## Decision

- 新增 `deploy/docker-compose.knowledge-hub.yml`：固定项目名与容器名 `knowledge-hub`，数据卷 `knowledge-hub_knowledge-hub-data`；加入共享 Docker 网络（整栈即 consumer 网络，`CONSUMER_NETWORK` 必填），服务名不变，onto-modeling 仍经 `http://knowledge-hub:8200` 访问；不发布宿主端口。compose 不写 `build`，源码与离线包共用：源码由部署脚本先构建镜像，离线包加载包内镜像。
- `deploy/deploy-knowledge-hub.sh` 是唯一的 Hub 部署脚本（离线包内为 `knowledge-hub/deploy.sh`）：读写所在目录 `.env` 的 `HUB_SERVICE_TOKEN`（注入 > 已有 > 生成），网络必须由调用方注入或显式配置，部署后在容器内做健康检查。
- `knowledge-hub/build.sh` 产出独立离线包；`deploy/build.sh` 把它并入完整包。onto-modeling 的 compose、override 与打包脚本移除 Hub。
- 编排顺序为 Hub → onto-modeling。共享凭据由编排方对齐：Hub 首次部署沿用 onto-modeling 已有的 `ONTO_HUB_SERVICE_TOKEN`，onto-modeling 部署前改用 Hub 实际使用的值。`deploy.sh` 新增 `--knowledge-hub-only`，`--onto-modeling-only` 不再触碰 Hub；分离安装脚本新增 `--knowledge-hub-package`（交互脚本为可选提问）。

## Alternatives considered

- 保留在 onto-modeling compose，仅拆出 override：生命周期和交付包仍耦合。
- 把 Hub 并入 `deploy-stack.sh` 或 consumer compose：会与 consumer 耦合，且 onto-modeling 不经 deploy-stack 部署。
- compose 中保留 `build`：离线包内没有 Hub 源码目录，能否以 `--no-build` 安全运行依赖 Compose 版本行为，未采纳。
- 由 Hub 部署脚本自行猜测网络：猜错会让 compose 报 external network not found，与现有“只用现场探测结果”的约定冲突。

## Consequences

- Hub 与 onto-modeling 可分别部署、升级、重建；整栈与安装脚本保证 Hub 先就绪、两侧凭据一致。只部署 onto-modeling 而不部署 Hub 时，知识包发布按既有设计失败关闭。
- 单独部署（不经编排脚本）时需显式提供 `CONSUMER_NETWORK`，并手工保证 `HUB_SERVICE_TOKEN` 与 onto-modeling 的 `ONTO_HUB_SERVICE_TOKEN` 一致。
- 数据卷名变化：原 `onto-web_knowledge-hub-data` / `onto-modeling_knowledge-hub-data` 不自动迁移（Hub 尚无发布存量）；旧 Hub 容器在 onto-modeling 重新部署时作为孤儿清理。
- 源码模式每次部署都会重建镜像并可能重建容器，与 onto-modeling 源码部署一致，数据保留在卷中。
