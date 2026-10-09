# 0006 onto-web 更名为 onto-modeling

## Context

产品定位已从"onto 的 Web 端"演进为独立的引导式建模工作台，需要一个
与 modeling/consumer 家族一致的名字；同时根部署链此前不包含本应用。

## Decision

- 目录 `modeling/onto-web` → `modeling/onto-modeling`；发布包、镜像、容器
  统一改名 onto-modeling / onto-modeling-auth-server。
- **数据兼容保留旧标识（不改）**：compose 项目名固定 `name: onto-web`、
  数据卷 `onto-web-data` / `onto-web-auth-data`、容器内数据路径
  `/data/onto-web`、`.env` 变量族 `ONTO_WEB_*`、项目元数据文件
  `.onto-web.json`。这些是存量部署与项目数据的寻址键，改名即丢数据。
- 根一键部署链（deploy/build.sh + deploy/deploy.sh）纳入 onto-modeling：
  全栈构建并入 bundle 的 `onto-modeling/` 子目录，部署顺序 consumer →
  modeling → onto-modeling，新增 `--onto-modeling-only`；旧发布包缺席该
  子目录时跳过并提示。
- 部署零配置直连 auth-server：deploy.sh 在 .env 缺失时自动生成，
  并自动生成随机初始管理员密码写入 .env（600）、部署完成时提示；
  模型网关令牌缺失降级为提示（可在配置中心事后配置），不再阻断部署。

## Consequences

- 存量部署用新包直接升级：同 compose 项目接管，数据卷无缝复用。
- 全新环境 `./deploy.sh` 一步到位：admin 初始密码自动生成，无需手工 env。
- 历史文档/ADR 中的 onto-web 提法保留不改（历史记录）。
