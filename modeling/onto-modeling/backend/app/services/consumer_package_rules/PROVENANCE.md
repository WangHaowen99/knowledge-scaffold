# consumer 导入规则副本

发布前结构自检用 consumer 自己的导入规则检查建模导出的知识包（见 `docs/decisions/solver-validation-unified-entry.md`）。两个代码库在远端各自独立，规则以副本形式放在这里，由人工同步。

## 来源

- 上游：consumer 的 `common` 分支。提交号和每个源文件的 SHA-256 记录在 `upstream.json`。
- 许可：上游以 Apache License 2.0 发布，副本保留原文件头。
- 生成方式：除 `__init__.py`、`limits.py` 和本文件外，其余 `.py` 都由 `backend/scripts/sync_consumer_package_rules.py` 生成，不要手工修改。

| 副本 | 上游文件 | 内容 |
|---|---|---|
| `archive.py` | `consumer/backend/api/routes/ontologies.py` | ZIP 大小、条目数、路径安全、包根定位、ID 格式 |
| `ontologies.py` | `consumer/backend/ontology/ontologies.py` | 版本号格式 |
| `package_import.py` | `consumer/backend/ontology/package_import.py` | 清单检查、包布局识别 |
| `package_validation.py` | `consumer/backend/ontology/package_validation.py` | 核心校验、数量上限、别名与映射入口 |
| `ontology_catalog.py` | `consumer/backend/ontology/ontology_catalog.py` | 对象别名、字段别名、对象数据映射 |
| `package_snapshot.py` | `consumer/backend/ontology/package_snapshot.py` | 包内文件读取 |
| `solver_layout.py` | `plugins/solver/backend/solver_coordination/package/layout.py` | operation-centric 求解布局 |
| `solver_capability_catalog.py` | `plugins/solver/backend/solver_coordination/capability_catalog.py` | 是否声明求解布局、启用开关 |

每个文件只复制从入口可达的定义，函数体保持原样。入口、实际复制的定义、删去的方法和替换的定义都记录在 `upstream.json` 中。生成时的改动只有以下几处：

- 上游模块间的 import 改为指向本目录的副本；`plugin_api.packages` 改为指向 `package_validation`，它在上游转发的就是这个实现。
- `PackageReadSession` 删去 `from_snapshot` 和 `snapshot` 两个方法。它们构造上游的修订快照，自检用不到。
- `archive.py` 中的 `logger` 改用标准库 `logging`。
- `limits.py` 是手写的：字段名、默认值和环境变量名与上游相同，但读不到 consumer 的 `ops_config.json`。部署调整了 consumer 上限时，要在建模服务设置同名环境变量。

## 有意不复制的规则

| 上游 | 原因 |
|---|---|
| 确定性 Operation（`ontology/operation_execution.py`） | 建模导出不声明确定性执行 |
| 求解准入的契约部分（`capability_catalog.py` 的其余部分、`agent_tools/solver.py`） | 依赖插件运行环境；对应规则由建模现有的求解契约静态检查覆盖 |
| 工作空间相关检查（本体数量、名称冲突、版本冲突） | 取决于目标环境，发布前无法判断 |

## 人工同步

建议在 consumer 升级部署前和建模发版前各做一次。

1. 用 `git archive` 把上游 `common` 分支的 `common/knowledge-scaffold` 导出到临时目录，记下提交号。
2. 运行 `cd modeling/onto-modeling/backend && ./venv/bin/python scripts/sync_consumer_package_rules.py --upstream <导出目录> --commit <提交号>`。
3. 审查 diff。如果新复制进来的代码含客户信息、真实地址等内容，按 AGENTS.md 停止同步并上报。
4. 运行 `CONSUMER_UPSTREAM_ROOT=<导出目录> ./venv/bin/python -m pytest tests/test_consumer_package_rules.py tests/test_consumer_rules_parity.py`。对照测试用同一批合成包分别跑副本和真实上游，断言两者结论一致。
5. 如果上游求解准入的规则版本（`SOLVER_VALIDATOR_VERSION`）有变化，还要核对 `solver_contract.py` 的静态检查是否仍然覆盖。
