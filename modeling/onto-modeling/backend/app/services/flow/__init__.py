"""流程运行时（详设 4.7、第 6 章；方案 D12、D14）。

- assemble：把各包声明的流程组装成一张图，检查锚点、环、编号与输出冲突（详设 6.7 第 1 项）；
- view：`GET /api/v1/projects/{id}/flow` 的响应（契约 FL-01）。

主流程只有一份定义：core 包里的 flow.yaml。装配时组装成装配目录里的 flow.yaml（JSON 写法，
是合法的 YAML，工作区脚本只用标准库就能读），后端进度、state_helper、/ontology 的下一步建议、
`/flow` 都读它。探针怎么算在 core 包的 scripts/flow_state.py 里，后端与 state_helper 共用。
"""
