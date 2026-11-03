# kb-gateway：多个知识后端共用一套知识接口

## Context

kb-service 原来同时对外提供三套协议：知识库页的管理接口、DA-java 的工具接口（`/v1/tools/*`）和给 Swarm 主 agent、Loop Forest 用的 MCP。要接入 WeKnora、DA 原版知识管线这类其他知识后端，照原来的做法，每个后端都要把三套协议各实现一遍。

## Decision

1. **统一接口集**：定义 `contracts/knowledge-provider/v1`，路径和数据形状沿用 kb-service 现有的 `/v1` 管理接口，再加 `GET /v1/capabilities`。核心能力（知识库与文档只读、L0/L1 正文、分块、检索）每个后端必须实现；写入、Wiki、问题生成、概况、DA 原生工具等是可选能力，在 capabilities 里声明。
2. **kb-gateway**（`service/kb-gateway`）：三套对外协议只在这里实现一次。gateway 只做转发、格式转换和多后端结果合并，**不读取正文、不自己实现知识能力**：凡是要读取或分析文档内容的（逐行查找、按标题取章节、锚点和目录、分块上传），都是统一接口集里的能力，由后端实现。
   - 管理接口按 ID 路由到所属后端，不支持的能力直接返回 501。
   - DA 工具接口：查询范围全部落在一个声明了 `tools.da` 的后端上时原样转发（kb-service 的行为不变）；否则把 DA 工具调用换算成统一接口（`search`、`content.structure`、`grep` 等）分发给各后端，只转换数据格式。后端没有相应能力时，跨后端的查询跳过它并注明，单个后端的查询直接告知不支持。
   - MCP：7 个工具都基于统一接口集实现。
   - 分块上传：会话建在知识库所属的后端上（`upload_id` 带该后端的前缀），gateway 只把每个分块原样转过去。
   - gateway 不存数据：后端列表是静态的 `backends.yaml`，服务密钥沿用调用方现有的 kb-service 密钥（按环境变量名引用），后端凭据也按环境变量名引用。
3. **按 ID 前缀路由**：每个适配后端配置唯一前缀（`wk_`、`da_`），由它的适配服务负责给所有出参 ID 加前缀、给入参去前缀；kb-service 不带前缀，已有的知识库 ID 继续有效。「默认后端」只决定新建知识库建在哪里，和谁不带前缀无关，所以换默认后端（现在是 WeKnora）不会改动任何已有 ID。前缀不能含 `:`、`,`、`/`，以免和 DA 的 pageId、锚点、`X-Kb-Scope` 冲突。
4. **多个后端并存**：一个知识库只属于一个后端。跨后端检索由 gateway 并行查询，按排名融合（RRF，k=60），不比较各后端的分数；某个后端失败时返回其余结果，并在 `failed_backends` 里注明。
5. **适配服务**：`service/weknora-adapter`、`service/da-kb-adapter` 把各自后端的接口转换成统一接口集；共用的鉴权、ID 前缀、错误格式和分块上传会话放在 `service/provider-kit`。L1 窗口、锚点、目录和逐行查找的实现在 kb-service 的 `kb_service.text_structure`（只依赖标准库），kb-service 自己的接口和两个适配服务共用这一份，适配服务按无依赖方式安装 kb-service 后引用。DA 原版是 TypeScript 源码，文件头有个人署名，所以不进本仓库：镜像从外部源码目录（`DA_TS_SRC`）构建，仓库里只放 Dockerfile 和适配层。
6. **前端**：知识库页和「知识求索」页复用同一套插件，按每个知识库的 `features` 显示页签和按钮。kb-service 的知识库继续用原来手写的设置表单；其他后端的私有设置按 capabilities 里的 `config_schema` 自动生成表单。
7. **增强服务只预留设计**：capabilities 里固定了 `role: enhancement` 以及 `provides`、`hooks`、`requires`、`capability_groups`、`ui` 等字段（见契约第 8 节）；v1 的 gateway 遇到增强服务直接拒绝注册。

## Alternatives considered

- **每个后端各自实现三套协议**：工作量随后端数乘以协议数增长，协议的细节（锚点、范围校验、出错格式）也会各不相同。
- **gateway 自己保存知识库到后端的映射表**：需要数据库和对账流程；用 ID 前缀可以让 gateway 不存状态。代价是 ID 里能看出后端，换后端等于重新入库。
- **只允许一次查询落在一个后端上**：实现最简单，但团队模式下 DA 和 Loop Forest 会把租户下全部知识库作为范围，必然跨后端，所以直接做了跨后端检索和排名融合。
- **gateway 为缺能力的后端兜底**（自己读 L1 全文做逐行查找、解析锚点、暂存上传分块）：后端可以少实现几项，但 gateway 会变成一个读正文、有状态的服务，职责和后端重叠，已放弃。
- **把 DA 原版知识管线移植成 Python 并入 kb-service**：估算 40–70 人日，而且没有测试可以用来核对语义；现在的做法是不改它的源码，只加适配层。

## Consequences

- 消费方（Swarm 插件、DA-java、Loop Forest、MCP）只连 kb-gateway；kb-service 退回到普通的基础后端。只部署 kb-service 时，行为与原来一致（DA 工具原样转发）。
- 每多一个后端，只需要写一个适配服务，并在 `backends.yaml` 里加一条。
- 跨后端检索按排名融合，各后端自己的分数只能在同一后端内比较。
- gateway 不依赖 kb-service，不保存任何文件；每多一项读取正文的能力，都要在统一接口集里定义，由后端实现。
- 非 kb-service 后端上的 DA 工具由各后端的统一接口能力拼出，没有 kb-service 原生实现里的生成问题召回、Wiki 链接扩展（`linkedFrom` 只支持 Wiki 页面）等能力；DA 原版的 `timeline_build`、`graph_build` 没有 HTTP 接口，本期没有接入。
- DA 原版知识管线没有单文档接口、没有分页，也不能取消处理；适配服务靠按知识库列出文档来定位文档、自己切分 L1 作为分块。它的后端以 `DA_AUTH_MODE=none` 运行，只放在它和适配服务专用的网络上。
- WeKnora 适配器不会接入已在使用的 WeKnora 实例，部署脚本另起一个独立实例；L1 正文由分块按偏移拼回，偏移不可用时以空行拼接。
- 增强服务的路由、事件轮询、能力合并等留到下一期实现，接口字段已经预留。
