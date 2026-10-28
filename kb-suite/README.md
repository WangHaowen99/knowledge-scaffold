# kb-suite

知识服务（kb-service，部分移植自 WeKnora）与 JiuwenSwarm 集成件：

- `service/`：后端服务
  - `kb-service/`：知识库入库、检索、管理接口、DA 工具接口、MCP
  - `lf-service/`：把 agent-solution 的 Loop Forest 包成 DA 兼容的 HTTP 服务
- `harness/`：openJiuwen harness provider
  - `da-harness/`：把 DeepAnalyze-Java 包成 harness provider（入口点 `deepanalyze`）
  - `lf-harness/`：Loop Forest 的 harness provider（入口点 `loopforest`）
- `swarm-kb-plugin/`：JiuwenSwarm 应用插件（「知识库」管理页、「知识求索」页）
- `deploy/compose/`：Docker Compose 部署清单和构建脚本，说明见其中的 `README.md`

设计文档见 `docs/`。第三方代码来源见 `NOTICE`。

## 测试

Python 包要求 3.11 及以上，各包独立安装：

```bash
cd kb-suite/service/kb-service && pip install -e '.[dev]' && python -m pytest -q
cd kb-suite/harness/da-harness && pip install '.[dev]' && python -m pytest -q tests
cd kb-suite/harness/lf-harness && pip install ../da-harness '.[dev]' && python -m pytest -q tests
cd kb-suite/swarm-kb-plugin/frontend && npm ci && npx tsc --noEmit && KB_APP=kb npx vite build --outDir /tmp/kb-manager
```

`service/lf-service` 依赖 openJiuwen 的 Java 构件，要先跑过 `deploy/compose/kbsuite.sh build-da`，再在 `kbs-m2` Maven 卷里执行 `mvn test`。
