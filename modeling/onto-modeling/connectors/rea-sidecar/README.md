# rea-sidecar

REA 静态分析旁路容器（ADR-0082）。网关把目标程序转过来，这里判定类型、只跑白名单里的 REA 静态命令，
把结果转成证据层标准记录（带 `bin:` 锚点和 `hash`）。只有这个容器碰目标程序的字节；容器断网、只读、非 root。

## 接口

只在 `rea` 内部网络上监听 8130，不验票据。字段和规则见 `docs/plugin-architecture/design/evidence-collection.md` §1、§2、§5。

| 接口 | 说明 |
|---|---|
| `GET /health` | `{status, rea_version, running, queued}` |
| `POST /v1/jobs?id=&source=&filename=&sha256=&max_seconds=&max_records=&max_bytes=` | 请求体是目标程序，要 `Content-Length`；回 202 `{id}` |
| `GET /v1/jobs/{id}`、`POST …/cancel`、`DELETE …` | 进度 / 取消（杀整组进程）/ 清目录 |
| `GET /v1/jobs/{id}/result`、`…/records`、`…/raw/{name}` | 结果说明 / 记录 JSONL / REA 原始结果 |

错误回 `{"error": 代码, "message": 说明}`：`too_large` 413、`artifact_mismatch` 422、`queue_full` 503、`job_exists` 409、
`length_required` 411、`not_ready` 409。任务失败的 `error_code`：`timeout`、`analyzer_failed`、`runner_crashed`。

同时跑 1 个、最多排 4 个；时限从收下任务算起。每个任务一个 runner 子进程（独立进程组），REA 子进程只拿到
`PATH`、`HOME`、`LANG`、`TMPDIR`（后两者在任务目录里）。结束后目标程序立即删，结果留到 `DELETE`，6 小时没取自动清。

## 构建与运行

```bash
docker build -t onto-modeling-rea-sidecar .                      # 默认 npmmirror，--build-arg NPM_REGISTRY=… 可换
docker volume create rea-scratch
docker run -d --network none --read-only --tmpfs /tmp -v rea-scratch:/scratch --user node \
  --memory 2g --pids-limit 128 --cap-drop ALL --security-opt no-new-privileges onto-modeling-rea-sidecar
```

可调的环境变量（都有默认值）：`REA_SIDECAR_MAX_BYTES_MB`（2048）、`REA_SIDECAR_MAX_OUTPUT_MB`（512，单条 REA 输出）、
`REA_SIDECAR_RUNNER_HEAP_MB`（1536）、`REA_SIDECAR_MAX_QUEUED`（4）、`REA_SIDECAR_TTL_S`（21600）、
`REA_SIDECAR_MAX_PYC`（50）、`REA_SIDECAR_MAX_ASSEMBLIES`（20）。

## 测试

```bash
node --test test/*.test.mjs        # 不需要装 REA：服务端用 test/fake-rea 顶替
```

- `anchors.test.mjs` 用 `backend/tests/fixtures/evidence_vectors.json` 和 `spec.py` 对齐；单独拷出这个目录跑时设 `EVIDENCE_VECTORS`。
- `map.test.mjs` 的夹具是 REA 对 `test/targets/` 合成程序的真实输出，用 `test/fixtures/trim.mjs` 裁过。
- 端到端：`bash test/targets/build.sh <目录>` 编出合成程序（JS 目录/zip/asar、.NET、PyInstaller、普通 zip，不进 git），
  再用 `docker run --network container:<旁路容器> … node test/e2e.mjs http://127.0.0.1:8130 <程序> [源] [保存目录]` 逐个跑，
  它会核对锚点前缀、`hash`、上级与关系端点、原始结果的哈希，并确认 `DELETE` 后查不到。

## 升级 REA

1. 改 `package.json` 里 `rea-agents` 的精确版本，`npm install --package-lock-only --ignore-scripts` 重生成锁文件。
2. 构建镜像，用 `build.sh` 的产物跑 `test/e2e.mjs`（加保存目录），把 `raw/` 里的结果用 `trim.mjs` 重做夹具。
3. 跑 `node --test`；映射测试不过就是 REA 的输出结构变了，改 `src/map-*.mjs`，锚点规则不能变（快照之间按锚点比差异）。
4. 新版本加了命令也不会自动放行，白名单在 `src/rea.mjs`。
