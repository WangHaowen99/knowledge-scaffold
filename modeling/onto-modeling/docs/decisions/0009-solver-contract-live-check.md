# 0009 求解模型面板的实时契约检查：后端试算，不在前端复写规则

## Context

求解适配需求要求求解模型面板在保存前就提示契约问题（字段实时校验、「实时契约检查」区），
尤其是 model.yaml 中的 `required_facts`、`objectives`、`display_mapping`。这些规则
（Decision Model Profile 的锁定目标序、选项域、事实名与 Profile 一致）只在后端
`solver_contract` 中实现，保存时的结构校验与发布检查共用它（REQ-BLD-001）。
前端原有的本地预检只覆盖表单可见的结构项，model.yaml 的问题要保存后才能看到。

## Decision

- 新增 `POST /api/projects/{id}/package/solver-contract/check`，请求体与保存相同
  （`{contract: {operations}}`），不落盘、不产生版本点、不需要写权限，返回
  `{rejected, findings}`：
  - `rejected`：保存会被拒的结构问题，与 `stage_contracts` 同源（共用 `_stage_files` / `_load_staged`）；
  - `findings`：保存后发布检查仍会拦截的问题，与 `check_workspace_solver` 同源（共用 `_check_contracts`）；
    能读出的契约照常检查，一条契约的结构错误不掩盖其他契约的问题。
  - 端到端验证门禁跟随已保存修订，草稿检查不给出。
- 面板在草稿有改动时停顿 500 ms 后调用该接口，新请求中止旧请求，只采用与当前草稿一致的结果；
  草稿与已保存一致时直接展示已保存修订的发布检查。
- 结果按 finding 的 path/field 定位到表单字段与附属文件页签（model.yaml → Decision Model 页签，
  一个页签可列出多条）；本地预检已标出的位置不重复计数。
- 当前草稿的检查结果判定保存会被拒时禁用保存；结果尚未返回或接口不可用时不拦截，保存时仍由后端完整校验。

## Alternatives considered

- **在前端复写 Profile 规则**：响应最快，但两套实现会漂移，违背保存与发布同一套规则的要求。
- **只在保存时校验**：不满足「保存前提示」的要求，model.yaml 的问题要来回保存才能发现。
- **保存为临时修订再检查**：会产生版本点并干扰乐观并发的基线修订。

## Consequences

- 规则仍只有后端一份；新增的 Profile 检查自动出现在实时检查中。
- 编辑期间每次停顿产生一次请求；检查只在临时目录读写，开销与一次保存的结构校验相当。
- 检查结果对应提交时的草稿，网络延迟期间面板标注「检查中」，期间不据此阻止保存。
