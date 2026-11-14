根据已确认的确定性候选生成专用 Parser、唯一 Handler、自包含的输入/输出 JSON Schema 和合成测试向量。用户从面板点击“选择并让助手制作”或“让助手完善”即确认制作所选业务操作；读取命令参数中的 operation、handler、key 和失败原因，检查当前文件后处理，不要求用户提供技术方案。首先用业务语言说明正在处理什么，进展时说明已完成与下一步。只对尚不明确的业务口径追问。对于已具备唯一有效 Handler、且参数只含 string、integer、number、boolean 的候选，平台“制作草稿”会生成基础专用 Parser；它只保守识别 Operation 名和“字段名为值”等明确表达。复杂自然语言、对象或数组参数需要由助手在草稿中补充专用规则。

Parser 接收 `--request-json`，输出且只输出包含 `matched`、`arguments`、`issues` 的 JSON 对象。Handler 继续遵循代码生成脚本的 CLI 契约。`data_access: none` 不得调用 Provider；`workspace_provider` 只能使用 `DeterministicProviderClient` 的白名单只读能力，不能导入数据库驱动或生成任意查询语句。

本次用户选择、检查发现及失败用例（优先逐项处理）：

制作前按 `.claude/reference/deterministic-agent-draft.md` 的“制作时的独立验收设计”先设计预期，再实现：覆盖业务参数明确但夹有标题年份/编号等无关数字的语义等价表达，同时保留非法值与冲突值拒绝。Provider 的查询类别与字段必须按实际数据绑定确认，不能直接把显示名称当物理类别；夹具依据独立绑定设计。没有真实接入证据时明确列为待验，不把沙箱通过等同于真实库存查询通过。

$ARGUMENTS

生成 Handler 后按代码生成流程写入 `ontology/scripts/`。用户已选择候选时，必须读取 `.claude/reference/deterministic-agent-draft.md`，由助手完成专用 Parser、业务 Schema、真实预期测试向量并写入非受管候选草稿 `ext/deterministic-execution/drafts/<handler>.json`。用户只确认业务与点击按钮，不要求其改代码或占位测试值。提示用户到“代码生成与求解 → 确定性执行”面板操作：首次制作时重新扫描并点击“制作草稿”；已有草稿或正式契约的修复优先点击“读取助手新版草稿”，核对业务变化后确认保存，再运行沙箱验证。不要要求删除正式契约或重复制作来读取新版。平台读取助手草稿后用户确认保存、运行沙箱验证。不要直接写 Contract 目录，也不要声称候选草稿已经保存成正式契约或通过沙箱验证。

生成 workspace_provider Handler 前，先读取 `.claude/reference/deterministic-provider-sdk.md`，以其中的 import、白名单参数与 Broker 返回协议为准。不要从普通 Agent 图查询工具的包装推测 SDK 返回值；工作区缺少可 import 的 SDK 不代表真实沙箱未提供该能力。

验证遇到环境问题时先区分入口：普通 `script_verify.py` 的 local 回退环境不一定提供 Provider SDK 或工作空间连接。若实际失败明确属于组件缺失，保留失败记录并说明“普通脚本入口尚不能验证查询”，不要重复执行同一失败命令、伪造通过记录或为了消除错误改写业务逻辑。完成可执行的静态检查和草稿预检查后，明确提示用户确认保存并进入“确定性执行 → 验证执行闭包”。该入口用合成 Provider 数据验证真实沙箱中的执行链；通过后仍须在消费端绑定数据源并验证真实查询。不得将普通入口的依赖缺失扩大表述为平台所有沙箱不可用；其他类型失败仍须逐项定位和修复，不得用环境问题一概解释。

## 确定性 Handler 的 CLI 适配（优先于普通代码生成骨架）

确定性沙箱不提供 `sys`、`vars` 或任意文件访问。不得照搬普通代码生成模板中的 `import sys`、`sys.exit`、`sys.stdout/stderr` 和 `**vars(args)`；也不能通过别名、反射或其他模块间接访问它们。保留普通脚本的头部、argparse 声明、描述符及单 JSON 输出约定，但采用下面的专用骨架：

- 直接实例化 `argparse.ArgumentParser`，以便描述符静态提取参数；不要定义解析器子类。
- 在解析前把实例的 `error` 绑定到抛出 `ValueError` 的函数。缺参、类型错误和未知参数都转为退出码 0 的 `INVALID_INPUT` JSON；不回显原始输入。
- 显式按参数名调用业务函数，不使用 `vars`。成功结果和可修正的输入错误通过 `print(json.dumps(...))` 输出。
- 仅主入口可以 `raise SystemExit(_cli())`。`_cli` 正常或输入错误返回 0；程序自身异常返回带 `ONTO_PUBLIC_ERROR:` 的安全字符串。Python 会将该字符串写入 stderr 并以非 0 退出，无需导入 sys。不回显异常详情或敏感参数。
- 以下是人工合成示例，实际生成时按已确认的 Operation 修改参数、业务函数和头部；保留专用 CLI 结构。普通复审不得要求将这套结构改回 sys/vars 模板。

```python
from __future__ import annotations
import argparse
import json


def calculate_summary(amount: float) -> dict:
    return {"value": amount}


def _argument_error(_message: str) -> None:
    raise ValueError("参数缺失、类型错误或含未知参数；请按声明提供 --amount 数值")


def _cli() -> int | str:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.error = _argument_error
    parser.add_argument("--amount", type=float, required=True)
    try:
        args = parser.parse_args()
        result = calculate_summary(amount=args.amount)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": {"code": "INVALID_INPUT", "message": str(exc)[:240]}}, ensure_ascii=False))
        return 0
    except Exception as exc:
        return "ONTO_PUBLIC_ERROR:" + json.dumps({"code": "SCRIPT_FAILED", "message": "计算步骤异常（" + type(exc).__name__ + "）"}, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
```

生成后必须按草稿交接说明运行 `onto-deterministic-check --operation '所选业务操作名称'`，修复返回的全部问题后再交付。修复任务先核对命令携带的 findings 和 failed_cases，逐项确认已处理，不要求用户重新整理已提供的失败信息。普通脚本验证通过不代表满足确定性沙箱约束。保存执行契约后还须在确定性执行面板运行真实沙箱验证，不能把 local 记录或只读预检查视为 jiuwenbox 验证。
