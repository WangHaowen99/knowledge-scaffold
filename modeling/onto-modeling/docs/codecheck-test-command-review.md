# 测试命令执行安全复核

本说明对应 PR #491 / #492 的 CodeCheck 命令执行提示。安全加固不等于检查平台已批准：以下调用仍保留扫描，未增加 `NOSONAR`、规则排除或忽略设置。最终由仓库安全审核与 PR 审查确认。

## 调用范围与依据

| 调用位置 | 必要性 | 输入及执行边界 |
| --- | --- | --- |
| `benchmark_artifact_verification.py::cli` | 比较实际 CLI 的冷启动和回执复用，而非仅比较进程内函数 | 仅允许三个检查器文件名，必须属于当前工具箱或调用方显式传入的已提取基线目录；拒绝符号链接和目录外脚本。当前 Python、参数数组、隔离环境、`shell=False`、60 秒超时。 |
| `benchmark_artifact_verification.py::git_output` | 读取基线提交及脚本内容 | 仓库固定为本 checkout；只接受三种只读操作的完整参数形状，引用/对象 ID/脚本目录逐项校验；Git 仅从系统默认路径解析；不继承宿主 Git 环境；普通 blob 限 500 文件/16 MiB；`shell=False`、60 秒超时。 |
| `test_artifact_verification.py::test_default_idle_ttl_and_explicit_override` | 验证配置首次导入时的默认值与显式覆盖 | 不再启动进程或传入 `-c` 源码；每次加载实际配置文件到全新的模块命名空间，不复用/替换 `sys.modules` 缓存；断言默认值、覆盖值及前一实例保持不变。 |
| `test_artifact_verification.py::test_diagram_import_without_timezone_database` | 模拟缺失时区数据库，避免污染后续用例 | 不再启动进程；局部替换时区构造器，独立加载实际检查器，断言 UTC+8 降级及补丁恢复、原模块缓存不变。 |
| `test_artifact_verification.py::git_workspace` | 为保存 hook 创建真实、独立 Git 测试工作区 | 强制限制初始化、两项合成身份配置、两类固定提交和两个产物路径的 add 操作；其他命令/参数拒绝。Git 绝对路径、临时工作区、隔离环境、`shell=False`、30 秒超时。不访问远端仓库。 |
| `test_artifact_verification.py::hook` | 验证真实 pre/post hook 的阻断与回执行为 | 仅允许两个既定 hook 名称；解析后必须处于临时工作区，文件与仓库 hook 逐字节相同；直接执行受核验脚本入口；消息走 JSON stdin，不作为 shell 源码；`shell=False`；60 秒超时。 |

## 执行环境加固与回归

`artifact_process_policy.py` 只构造环境和校验能力，不封装/隐藏进程调用。所有进程入口仍位于上表所列测试中。

- 不继承宿主 PATH、凭据、代理、`BASH_ENV`、`ENV`、`PYTHONPATH`、动态库注入变量和 `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` 等设置；仅保留 Windows 必需的 loader 环境键。
- PATH 仅由当前可信 Python 安装目录和系统默认路径组成；HOME、临时目录和配置目录均在临时工作区内。系统默认路径找不到 Git 时明确失败，不回退到用户 PATH。
- 禁止系统/全局 Git 配置，显式关闭 Git 自动 hooks、模板注入、文件监测、签名及传输协议。产品 pre/post hook 仍由测试显式调用，并执行原有字节一致性校验，不因此跳过覆盖。
- 拒绝测试覆盖目录外/非白名单/符号链接脚本、不允许的 Git 命令/参数和仓库。真实集成用例在污染的宿主 Git/PATH/`BASH_ENV` 环境下完成合成提交及 pre/post 检查，并证明命令消息里的替换语法不会执行。
- 当前及提取基线的真实 CLI、回执复用和核验器 JSON 输出继续测试；不以进程内模拟代替这些契约。本分支不包含后续来源抽取/场景输入工具的分页与编码用例。

## 前提与剩余边界

- 仓库、显式选择的基线提交、当前 Python 安装目录及系统工具安装必须可信。本测试不是运行第三方脚本的沙箱，不对恶意测试机或恶意基线作安全承诺。目录/参数白名单不能证明基线代码本身安全。
- 不得把业务文件内容、模型输出或远程用户输入拼入代码、Git 参数或脚本入口。环境隔离不是操作系统级沙箱。
- `shell=False` 只表示 Python 不隐式解析命令字符串；产品 `.sh` hook 仍由 Bash shebang 执行，其中固定的 Python 片段属于被测产品代码。JSON 消息只作为协议输入，不能声称整个调用链无 shell，也不能将其推广为任意命令执行接口。
- `test_artifact_gate_safety.py` 验证非白名单/被替换 hook 被拒绝，基线 symlink/超限内容在读取 blob 前被拒绝，以及 JSON 标准输出协议。
- 这些证据不代表 CodeCheck 通过、全部后端用例通过、Windows 或线上验收完成。先等待新提交的扫描结果，仍提示的必要调用再逐项判断；旧报告的审核申请不能代替新结果。不得删除真实 CLI/hook 测试、改用隐蔽的执行接口或全局忽略规则来获取绿色门禁。
