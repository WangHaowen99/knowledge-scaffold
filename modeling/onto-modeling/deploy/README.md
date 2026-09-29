# Onto Modeling 部署说明

离线交付包部署流程，对应方案第八节与 4.1.1 容量规划。

## 交付包内容

```
onto-modeling/
  images/                     # onto-modeling 与 auth-server 镜像 tar
  images.env                  # 镜像 tag 清单（deploy.sh 自动读取）
  docker-compose.release.yml
  deploy.sh                   # 预检 → 加载镜像 → 拉起
  preflight.sh                # 可单独运行的部署预检
  .env.example                # 环境变量说明（复制为 .env 后填写）
  README.md
```

## 部署步骤

```bash
tar -xzf onto-modeling-<version>-linux-<arch>.tar.gz
cd onto-modeling
./deploy.sh          # 首次运行会生成 .env 并提示填写模型网关配置
vi .env              # 填 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
./deploy.sh          # 再次运行完成部署
```

访问 `http://<部署机地址>:5100`（端口可用 `ONTO_WEB_PORT` 覆盖）。

### Modeling MCP 关联与远端清理

如果 Onto Modeling 与核心 Modeling 共用认证中心，配置：

```env
ONTO_WEB_MODELING_URL=http://mw-modeling:8100
```

建议同时配置远端项目清理凭证，保证 Onto 项目创建失败或删除后，核心 Modeling
中的关联项目可以自动清理：

```env
ONTO_WEB_MODELING_CLEANUP_TOKEN=<随机高强度值>
ONTO_WEB_MODELING_CLEANUP_INTERVAL=30
```

核心 Modeling 侧配置同一个值：

```env
MODELING_MCP_CLEANUP_TOKEN=<与 Onto 相同的值>
```

该凭证只允许 Onto 后端与 Modeling 后端使用，不能写入前端、日志或普通用户请求。
未配置时不影响正常项目创建和 MCP 使用，但远端清理失败会进入 Onto 的持久化补偿
任务，可在管理员接口中查看和重试。清理任务不保存用户 Bearer Token。

## 容量规划（方案 4.1.1）

- 每个活跃建模会话 = 一个 CLI 子进程，均值约 400MB、峰值 500MB+。
- 8G 主机：保持默认 `ONTO_WEB_MAX_SESSIONS=12`，约支撑 12–15 并发活跃会话、
  30–50 名在线用户（突发式交互节奏）。
- 几十用户同时建模为目标时建议 16G 主机，并按比例调高 `ONTO_WEB_MAX_SESSIONS`
  与 compose 里的 `ONTO_WEB_MEM_LIMIT`。
- 建议主机配置 2–4G swap 或 zram 吸收峰值。
- 空闲会话默认 10 分钟回收（`ONTO_WEB_SESSION_IDLE_TTL`），回收后靠 resume
  续接模型上下文，用户无感；会话 transcript 持久化在数据卷 `/data/home/.claude/`。

## AVX 指令集要求（x86_64）

捆绑的 Claude Code CLI 是 Bun 编译的原生二进制，要求 CPU 暴露 AVX，否则子进程
SIGILL 崩溃。`preflight.sh` 会检查；未通过时按优先级整改：

1. **虚拟化层整改**：CPU 模型改 host-passthrough 或任一带 AVX2 的具体型号（零代码成本）。
2. **换 ARM 交付包**：aarch64 无 AVX 概念，无此限制。
3. **降级兼容包（最后手段）**：在目标机准备 Node ≥18 运行时和旧版 JS 交付的
   CLI（2.0.76，纯 JS 无 Bun/AVX 依赖），将其挂载进容器并设
   `ONTO_WEB_CLI_PATH=/path/to/cli.js`。注意：降级组合功能落后且冻结维护，
   必须在目标虚机实测关键路径（确认卡、hooks、子代理、会话恢复）后才能放行，
   界面无"兼容模式"标识前不建议长期使用。
4. 以上均不可行则明确不支持该环境。

## 数据与备份

- 业务数据全部在 docker 卷 `onto-web-data`（项目 workspace、会话历史、设置）
  与 `onto-web-auth-data`（用户账号）。
- 运维中心提供手动备份（保留最近 3 份）；建议在主机侧对两个卷做定期快照。

## 升级与回滚

- 升级：解包新版本交付包，重复部署步骤；`docker compose up -d` 滚动替换容器，
  数据卷保留。
- 回滚：用旧版本交付包重复部署步骤即可（镜像 tag 随版本走，互不覆盖）。

## 接入既有账号中心（单点账号）

onto-modeling 默认自带独立 auth-server。若要与 consumer / modeling 栈共用同一
用户库（账号密码即刻通用），在 `.env` 里加两行后重跑 `./deploy.sh`：

```
ONTO_MODELING_AUTH_URL=http://auth-server:5056
ONTO_MODELING_EXTERNAL_AUTH_NETWORK=consumer_onto-agent-network
```

deploy.sh 会停用自带 auth-server、让应用容器加入 consumer 栈网络，直接复用其
`auth-server` 服务（issuer/audience 一致，token 互认）。切换回自带模式：删除这
两行重跑 deploy.sh 即可（自带 auth 数据卷始终保留）。需要 docker compose v2.24+。

## 从原 modeling 服务迁移数据

deploy.sh 部署完成后会自动检测 `mw-modeling` 容器（可用
`ONTO_MODELING_MIGRATE_FROM=<容器名>` 覆盖），存在即执行
`migrate-from-modeling.sh` 把项目/资料/知识包迁入本服务；迁移幂等，
已迁移的项目自动跳过，也可随时手动重跑该脚本。
