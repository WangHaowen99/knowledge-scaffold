# Knowledge Package Hub

独立保存 Onto Modeling 发布的知识包版本和项目最新原始资料。使用文件、JSON 和每项目 Git；无需数据库、Agent SDK 或 LLM。项目和成员权限由 Onto 决定。

## 启动

```bash
cd knowledge-hub/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

从运行环境注入配置（变量见 `../.env.example`）：

- `HUB_DATA_DIR`：独立持久化目录，不与工作区共享写目录。
- `ONTO_AUTHZ_URL`：Onto 的 `/api/internal/hub/authorize` 地址。
- `HUB_SERVICE_TOKEN`：专用服务凭据，与 Onto 的 `ONTO_HUB_SERVICE_TOKEN` 相同；不得记录、提交或发往浏览器。
- `HUB_DEV_ALLOW_UNAUTH=1`：仅允许显式无鉴权的本地联调。仍然调用 Onto 检查项目存在性，且 Onto 必须同时开启 `ONTO_HUB_DEV_ALLOW_UNAUTH=1`。没有回调地址时始终拒绝访问。
- 上传压缩总量默认 256 MiB、展开总量 1 GiB、单文件 256 MiB、成员数 10000，可通过示例中的变量调整；包与资料分别校验。

在 `modeling/onto-modeling/` 运行 `docker compose up --build` 会启动独立 Hub 容器和卷。必须先通过环境注入 `ONTO_HUB_SERVICE_TOKEN`。离线发布脚本自动构建并保存 Hub 镜像，目标机部署脚本生成专用凭据到受限的运行配置。没有初始化 Hub 时，Onto 发布明确失败，不退回本地成功。

## 数据和恢复

```text
projects/<project_id>/
  .git/
  releases/<release_id>/{receipt.json,package.zip}
  snapshots/<snapshot_id>/{receipt.json,package.zip}
  pending/<request_id>/
  metadata.json
  resources-current/       # 当前资料及清单，排除在 Git 外
cleanup/<project_id>.json  # 永久删除标记
```

支持单实例、本地持久化卷。进程间文件锁串行化写入、删除和备份；不支持多副本共享网络文件系统。发布目录是可恢复提交意图：Git 成功后才对外提供；启动和读取时校验摘要并恢复。最新资料替换使用同盘目录切换，旧集合清除，启动清理中断残留。资料没有版本查询接口。

Onto 每次资料修改后异步同步，30 秒周期扫描补偿 Agent 写入、导入及失败重试。项目最新资料状态独立于发布状态。删除任务在移走本地项目之前保存，不含用户令牌；每 30 秒重试远端清理，删除标记阻止晚到提交复活。项目读取始终回查 Onto，不缓存成员权限。

`/api/health` 返回剩余磁盘空间；运维应监控独立卷容量和 Git 增长。空间不足、摘要不一致或 Git 失败均不能报告发布成功。

## 备份与恢复

备份命令持有同一写锁，包含包、Git、请求、序号、最新资料和删除标记：

```bash
cd knowledge-hub/backend
python -m hub.backup backup /data/knowledge-hub /backup/hub-current
python -m hub.backup restore /backup/hub-current /restore/knowledge-hub \
  --deletion-records /restore/onto/hub-tasks/cleanup
```

备份目标必须不存在；恢复到空目录，停服后再切换恢复卷。恢复必须提供当前 Onto 删除记录，优先重放记录再验证包摘要/Git。Onto 另行备份工作区及 `hub-tasks/`；原工作区备份不包含 Hub。切换前用授权用户验证项目归属与下载，以现存 Hub 回执对账，禁止从 Git 提交数伪造历史发布。

运维备份仅用于故障恢复：建议每日一次，保留最近 7 天，按实际批准策略覆盖；到期清除整份备份。不得向用户提供历史资料恢复入口，不为每个知识包版本创建资料备份。回退先停止新发布并核对请求，保留 Hub 卷；不要回退到本地发布旁路。

## 测试

```bash
cd knowledge-hub/backend
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests
```

全部测试使用隔离临时目录与合成数据。契约见 [API.md](API.md)。

未提交请求默认保留 7 天（`HUB_PENDING_TTL_SECONDS`），恢复扫描后清理包内容，保留过期回执和已占用序号；同一过期请求返回 410。Onto 对应输入缓存由 `ONTO_HUB_PENDING_TTL_SECONDS` 控制，默认同为 7 天，应保持两端配置一致。已提交包不按此策略清理。
