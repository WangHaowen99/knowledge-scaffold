# kb-suite

知识服务（kb-service，部分移植自 WeKnora）、知识网关（kb-gateway）与 JiuwenSwarm 集成件：

- `contracts/knowledge-provider/v1/`：知识后端统一接口集（每个基础后端实现，kb-gateway 调用）
- `service/`：后端服务
  - `kb-gateway/`：对外的唯一知识接口——知识库页的管理接口、DA 工具接口、MCP，按知识库路由到各个知识后端
  - `kb-service/`：默认的知识后端，负责入库、检索、Wiki、问题生成等
  - `weknora-adapter/`：把 WeKnora 接成知识后端（ID 前缀 `wk_`）
  - `da-kb-adapter/`：把 DeepAnalyze 原版知识管线接成知识后端（ID 前缀 `da_`）
  - `provider-kit/`：适配服务共用的鉴权、ID 前缀和错误格式
  - `lf-service/`：把 agent-solution 的 Loop Forest 包成 DA 兼容的 HTTP 服务
- `harness/`：openJiuwen harness provider
  - `da-harness/`：把 DeepAnalyze-Java 包成 harness provider（入口点 `deepanalyze`）
  - `lf-harness/`：Loop Forest 的 harness provider（入口点 `loopforest`）
- `swarm-kb-plugin/`：JiuwenSwarm 应用插件（「知识库」管理页、「知识求索」页）
- `deploy/compose/`：Docker Compose 部署清单和构建脚本，说明见其中的 `README.md`

设计文档见 `docs/`，架构决策见 `docs/decisions/`。第三方代码来源见 `NOTICE`。

## 测试

Python 包要求 3.11 及以上，各包独立安装：

```bash
cd kb-suite/service/kb-service && pip install -e '.[dev]' && python -m pytest -q
cd kb-suite/service/kb-gateway && pip install -e '.[dev]' && python -m pytest -q
# 适配服务只用到 kb-service 的 kb_service.text_structure（正文窗口、锚点、逐行查找），装 kb-service 时不装依赖
cd kb-suite/service && pip install --no-deps ./kb-service && pip install -e ./provider-kit -e './weknora-adapter[dev]' -e './da-kb-adapter[dev]' \
  && (cd weknora-adapter && python -m pytest -q) && (cd da-kb-adapter && python -m pytest -q)
cd kb-suite/harness/da-harness && pip install '.[dev]' && python -m pytest -q tests
cd kb-suite/harness/lf-harness && pip install ../da-harness '.[dev]' && python -m pytest -q tests
cd kb-suite/swarm-kb-plugin/frontend && npm ci && npx tsc --noEmit && KB_APP=kb npx vite build --outDir /tmp/kb-manager
```

`service/lf-service` 依赖 openJiuwen 的 Java 构件，要先跑过 `deploy/compose/kbsuite.sh build-da`，再在 `kbs-m2` Maven 卷里执行 `mvn test`。
