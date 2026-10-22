#查数脚本（涉及mindsdb查数的脚本才会加这个标签）
"""
关联operation：{调用该脚本的operation名称，即 ontology/operation/ 下已存在的文件名；要被评测或 consumer 执行的脚本必须填。“无”只用于被其他脚本 import 复用的辅助模块，它不会被单独导出执行}

功能描述：{一句话描述脚本功能}

详细逻辑：
{该脚本的详细实现逻辑}

用法：
    CLI    ：python3 {script}.py --参数名 参数值 [--可选参数 值]
    import ：from {script} import function_name; function_name(参数名=值, ...)

输入参数：
    {参数名}    {类型}    {说明，标注是否必填及默认值}
    （只列 argparse 声明的 CLI 参数，与 add_argument 一一对应；类型写 argparse 实际接收的类型：
    未写 type= 为 str，action="store_true" 为 bool，type=json.loads 为 json。仅供 import 调用的
    函数形参不写在此处。纯库脚本填"无"。填写后删除本括号说明）

输出：
    {stdout 输出的 JSON 结构（即业务函数的 return 值）及字段说明；脚本只做纯计算，不写表、不生成文件}

依赖：pip install {外部包，无则删除本行}
"""

from __future__ import annotations

import argparse
import json


def function_name(arg1: str, arg2: str | None = None) -> dict:
    # {在此实现业务逻辑，并替换函数名与签名}
    # 纯计算：禁用写法见 .claude/commands/ontology-codegen.md「脚本安全」，注释里也不要提这些模块名
    return {}


def _cli() -> None:
    """仅在脚本被 CLI 直接调用时使用；纯库脚本可删除本函数与下方 __main__ 块。"""
    parser = argparse.ArgumentParser(description="{一句话业务描述}")
    # parser.add_argument("--arg1", required=True, help="{说明}")
    # parser.add_argument("--arg2", default=None, help="{说明}")
    # parser.add_argument("--flag", action="store_true", help="{布尔开关说明}")
    args = parser.parse_args()
    result = function_name(**vars(args))
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    _cli()
