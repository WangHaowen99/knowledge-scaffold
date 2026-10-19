# 0014 代码生成的脚本必做运行验证，经脚本验证网关在 jiuwenbox 沙箱中执行

## Context

`/ontology-codegen` 此前只在提示词里要求复审 agent “运行脚本验证”：没有代码强制，也没有留下
记录，agent 漏跑了照样能提交；即使跑了，也是在 onto-modeling 容器里直接执行
`python3 ontology/scripts/x.py`，参数由 agent 手写，没有隔离。modeling 工作台的做法则是把脚本
送进 consumer 的 jiuwenbox 沙箱执行，跑通才记回执、没有回执不能入包。

consumer 在运行时同样在 jiuwenbox 里执行脚本。onto-modeling 生成的脚本此前要到评测时，才第一次
在沙箱里真正运行。

jiuwenbox 的管理接口经一个 UNIX socket 提供、没有鉴权，调用方在建沙箱时自带策略
（`policy_mode: override`）：挂哪些目录、能否出网、资源上限都由调用方决定。jiuwenbox 本身以高权限
运行（SYS_ADMIN、NET_ADMIN、宿主 cgroup、关闭 seccomp/apparmor），挂着 consumer 的运行数据，
consumer 的设计也只把这个 socket 交给 backend，mcp 和沙箱都不挂。onto-modeling 主容器里跑着能执行
任意命令的建模 agent（默认 `bypassPermissions`），它还要读用户上传的资料，存在被提示注入的可能。

## Decision

1. **验证是提交门禁，不是提示词。** `workspace-toolkit/scripts/script_verify.py`：
   `run` 按用例（`argv` + `expect: success | error`）逐个执行脚本，全部通过才把“脚本连同其 import
   的同目录模块”的源码哈希写进 `.ontology-state/script-verifications.json`；`check` 由 pre-commit
   调用，暂存的新增或修改脚本（以及 import 了改动模块的脚本）没有当前版本的通过记录就拦下提交。
   `/ontology-codegen` 新增 Step 4.6（必做），复审 agent 不再自行运行脚本。
2. **jiuwenbox socket 只交给无特权的脚本验证网关。** 新增 `sandbox-gateway` 容器
   （`onto-modeling-sandbox-gateway`，与 onto-modeling 同一镜像，`python -m app.sandbox_gateway`），
   它是唯一挂载 socket 的容器：
   - 只接受「打包好的脚本 + 入口文件名 + 用例」，沙箱策略、解释器、执行器、超时都写死在
     `jiuwenbox_client.run_bundle`（与 consumer、modeling 工作台同一套策略：只挂系统目录、仅 `/tmp`
     可写、出网全拒、cgroup 限额）；请求里多出任何字段都拒绝。
   - 同时最多 2 个沙箱、另外最多排队 8 个，再多直接返回 503；请求体上限 256KB。
   - 自己从不执行脚本：jiuwenbox 不可用时返回 503，由调用方回退。否则脚本就跑在能连 socket 的容器里。
   - 以 uid 65534 运行，根文件系统只读，去掉全部 capability，`no-new-privileges`，不读 `.env`、
     不挂数据卷、不发布端口，只在 onto-modeling 的内部网络上监听 8110。
   onto-modeling 主容器不挂 socket，经 `ONTO_WEB_SANDBOX_GATEWAY_URL` 调用网关。
3. **网关不可用时在容器内执行。** 网关连不上、没配置，或报 jiuwenbox 不可用时，同一个执行器改在
   onto-modeling 容器内运行（临时目录、最小环境变量），结果标注 `runtime: local` 与原因；脚本本身
   失败算用例失败，不触发回退。
4. **部署默认挂载，状态只读展示。** 整栈部署探测 consumer 的 jiuwenbox socket 卷，只读挂给网关
   （源码部署写进 `docker-compose.stack.yml`，发布包写进 `docker-compose.jiuwenbox.yml`，
   `redeploy-app.sh` 沿用该文件并一起替换网关）；配置中心「沙箱」经网关显示是否连上
   onto-agent-jiuwenbox 与沙箱 Python 环境，不提供开关或地址配置。

## Alternatives considered

- **只改提示词，要求 agent 改用沙箱运行**：仍然无法保证一定执行，正是要解决的问题。
- **socket 直接挂进 onto-modeling 主容器**：最省事，但 agent 能自己调管理接口建宽松的沙箱，读到
  consumer 全部工作空间的数据，或在不设限额时拖垮宿主；只读挂载拦不住 connect。
- **部署时显式开启才挂载**：安全，但默认就不能在沙箱里验证，不满足默认连接的要求。
- **容器内按用户隔离**（agent 以普通用户运行、socket 只给后端）：不加容器，但要改两个引擎启动 agent
  的方式和工作区文件权限，改动面与回归风险最大。
- **复用 mw-modeling 持有的 socket**：不加容器，但要跨应用加接口和专用令牌，且只有部署了 mw-modeling
  的环境能在沙箱里验证。
- **onto-modeling 自带一个 jiuwenbox 实例**：不依赖 consumer，但仍要把高权限组件的管理 socket 交给
  agent 所在容器，镜像约 1.3GB，包环境也与 consumer 不一致。
- **jiuwenbox 不可用时拒绝验证**：会让未部署 consumer 的环境无法生成脚本；按用户要求回退到容器内执行，
  并在结果中注明。

## Consequences

- 改过的脚本必须有用例并跑通才能提交；`/ontology-update` 等修改脚本的流程同样受 pre-commit 约束。
  后端自己的提交（Web 保存、导入、目录规范化）不经过 agent 的 hook，不受影响。
- 部署多一个轻量容器（内存上限 256MB），与 onto-modeling 同一镜像，不增加发布包体积。
- agent 仍可直接调用网关，但只能在固定的严格策略下运行脚本；风险与 consumer 执行生成脚本同类：
  jiuwenbox 一旦有沙箱逃逸漏洞，两边都会受影响。并发与排队上限防止它刷爆 jiuwenbox。
- 沙箱内禁止出网：查数脚本连不到数据库，只能用合成输入验证。依赖沙箱里没有装的包的脚本会如实失败。
- 回退到容器内执行时，结果不能代表脚本在 consumer 沙箱里的表现。
