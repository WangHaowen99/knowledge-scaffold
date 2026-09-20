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

访问 `http://<部署机地址>:8100`（端口可用 `ONTO_WEB_PORT` 覆盖）。

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
