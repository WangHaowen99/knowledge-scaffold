# 测试命令执行安全复核

本说明对应 PR #493 / #494 的 CodeCheck 命令执行提示。安全加固不等于检查平台已批准：以下调用仍保留扫描，未增加 `NOSONAR`、规则排除或忽略设置。最终由仓库安全审核与 PR 审查确认。

## 调用范围与依据

| 调用位置 | 必要性 | 输入及执行边界 |
| --- | --- | --- |
| `benchmark_artifact_verification.py::cli` | 比较实际 CLI 的冷启动和回执复用，而非仅比较进程内函数 | 当前 Python 解释器；既有绝对脚本路径；参数数组；`shell=False`；60 秒超时。脚本只来自当前仓库或显式指定基线提交的工具箱。 |
| `benchmark_artifact_verification.py::git_output` | 读取基线提交及脚本内容 | 将 Git 解析为绝对路径；调用点只有 `rev-parse`、`ls-tree`、`cat-file`；基线参数经 `--end-of-options` 解析为提交 ID；仅读普通 blob，500 文件/16 MiB 上限；`shell=False`；60 秒超时。 |
| `test_artifact_verification.py::test_default_idle_ttl_and_explicit_override`（两处） | 以两个新解释器验证配置首次导入时的默认值与显式覆盖 | Python 源码为仓库内固定字符串；环境仅切换合成的 TTL 值；无网络请求或外部输入；`shell=False`；30 秒超时。进程隔离避免已导入模块缓存掩盖配置错误。 |
| `test_artifact_verification.py::test_diagram_import_without_timezone_database` | 隔离模拟缺失时区数据库，避免污染同进程后续用例 | 固定 Python 源码；工具箱路径单独作为参数传入，不拼入源码；`shell=False`；30 秒超时。 |
| `test_artifact_verification.py::git_workspace` | 为保存 hook 创建真实、独立 Git 测试工作区 | Git 绝对路径；调用参数均由本模块用例提供；工作目录为 pytest 临时目录；仅合成身份、文件与提交；`shell=False`；30 秒超时。不访问远端仓库。 |
| `test_artifact_verification.py::hook` | 验证真实 pre/post hook 的阻断与回执行为 | 仅允许两个既定 hook 名称；解析后必须处于临时工作区，文件与仓库 hook 逐字节相同；直接执行受核验脚本入口；消息走 JSON stdin，不作为 shell 源码；`shell=False`；60 秒超时。 |
| `test_review_integrity.py::test_source_cli_long_segment_continuation_is_lossless` | 验证实际 CLI 分页的中文输出及摘要一致性 | 当前 Python 解释器，`-I` 隔离；仓库脚本路径与临时合成文件路径独立传参；分页偏移来自合成测试数据；`shell=False`；30 秒超时。 |
| `test_review_integrity.py::test_utf8_cli_in_isolated_ascii_environment` | 验证实际 CLI 在 ASCII 环境设置下仍输出 UTF-8 | 当前 Python 解释器，`-I` 隔离；只有固定两个工具脚本；只读取临时合成 DOCX/场景文件；`shell=False`；30 秒超时。 |

## 前提与剩余边界

- 仓库、选定基线提交、当前 Python 解释器、测试机 Git 安装及测试进程 PATH 必须可信。本测试不是运行任意第三方脚本的沙箱，不对恶意测试机或恶意基线作安全承诺。
- Python `-c` 仅执行上述固定测试源码。不得把业务文件内容、模型输出或远程用户输入拼入代码、Git 参数或脚本入口。
- hook 的 JSON 消息是被测协议输入；真实 hook 仍在临时目录中检查合成产物。它不是关闭 shell 风险后即可执行任意用户命令的通用接口。
- `test_artifact_gate_safety.py` 验证非白名单/被替换 hook 被拒绝，基线 symlink/超限内容在读取 blob 前被拒绝，以及 JSON 标准输出协议。
- 这些证据支持逐项安全复核，不代表 CodeCheck 通过、全部后端用例通过、Windows 或线上验收完成。不得删除 CLI/进程隔离用例、改用更隐蔽的执行接口或全局忽略规则来获取绿色门禁。
