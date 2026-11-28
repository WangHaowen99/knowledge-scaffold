#查数脚本（涉及mindsdb查数的脚本才会加这个标签）
"""
关联operation：{本脚本对应的operation名称，即 ontology/operation/ 下已存在的文件名；一个operation只对应一个脚本，子操作并入主操作脚本；要被 modeling 或 consumer 执行的脚本必须填。“无”只用于被其他脚本 import 复用的库模块，它不会被单独导出执行}

功能描述：{一句话描述脚本功能；主脚本写明覆盖了哪些子操作}

详细逻辑：
{该脚本的详细实现逻辑；主脚本按子操作逐段写}

用法：
    CLI    ：python3 {script}.py --参数名 参数值 [--可选参数 值]
    import ：from {script} import function_name; function_name(参数名=值, ...)

输入参数：
    {参数名}    {类型}    {说明，标注是否必填及默认值}
    （只列 argparse 声明的 CLI 参数，与 add_argument 一一对应；类型写 consumer 调用时的类型：
    未写 type= 为 string，type=int 为 integer，type=float 为 number，action="store_true" 为 boolean，
    type=json.loads 按取值写 object 或 array，并在说明里写清字段、给一个示例。记录参数按来源对象分组：
    一个参数只装一个对象的查询结果，说明里写明 [[对象名]]，字段名逐字用对象属性清单的英文名称；
    顶层条件参数不与对象属性同名。编号类参数（*_id、*_no、*_code、*_name）与只含编号的记录参数不设必填：
    用户只描述情况、没给编号时，智能体只能编造编号，consumer 会拒绝调用。
    仅供 import 调用的函数形参不写在此处。纯库脚本填"无"。填写后删除本括号说明）

输出：
    {字段名}    {类型}    {说明}
    （stdout 那一个 JSON 对象的字段，与 return 结构一一对应；类型同上。主脚本必须有
    steps    object    各子操作的结论，键为子操作名。脚本只做纯计算，不写表、不生成文件。填写后删除本括号说明）

依赖：pip install {外部包，无则删除本行}
"""

from __future__ import annotations

import argparse
import json
import sys


def function_name(arg1: str, arg2: str | None = None) -> dict:
    # {在此实现业务逻辑，并替换函数名与签名}
    # 纯计算：禁用写法见 .claude/commands/ontology-codegen.md「脚本安全」，注释里也不要提这些模块名
    # 「不通过」「资料不足」等业务结论写在业务字段里，不要用 ok=False / error / status="failed" 表达
    # 操作明确要求缺参时提示补全的，在计算前用 _require_input_fields 校验相应记录和字段。
    # 缺可选资料、缺编号或无法关联时，按操作约定给出「资料不足」等业务结论。
    # 记录是否提供只看参数传没传：传了（包括 {} 和字段为空）就算提供，缺哪个字段按该字段的规则处理；
    # 只有两边都有编号时才核对。若缺记录属于资料不足，用 _missing_record 说明。
    # 记录之间的匹配、去重、分组和 steps 的键都用 _record_key：没有编号时按位置，两个空编号不算同一条
    # 「是/否」字段先用 _yes_no 归一再判断，不要直接写 value == "是"
    # 枚举字段先用 _check_choice 校验取值：不认识的取值不能当成「条件不满足」往下算
    # 每一步除了结论，还要输出算出的中间结果（分项取值与得分、命中的条件与阈值），用户常追问分项与原因
    # 主脚本每条结论最前面放「最终结论」：一句话直接回答问题；流程停止时写出停止对应的结论（如「不通过」）、
    # 停在哪一步和原因，不要只留 None
    return {}


def _record_key(record: dict, id_field: str, param: str, index: int) -> str:
    """记录的键：有编号用编号，没有编号用它在数组里的位置（如 events[1]）。
    拿空编号当键或比较相等，会把两条没有编号的记录当成同一条，匹配、去重和 steps 都会出错。"""
    value = str(record.get(id_field) or "").strip()
    return value or f"{param}[{index}]"


def _check_choice(value, allowed: tuple[str, ...], path: str) -> str | None:
    """枚举字段取值校验（allowed 取对象属性清单里的「取值：」）：不在范围内时抛 ValueError，由 _cli 转成
    ok=false，智能体按允许取值改正后重试。取值缺失返回 None，按资料不足的规则处理；operation 文档对非法值
    另有规定（如按资料不足处理）时按文档写，不用本函数。"""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if text not in allowed:
        raise ValueError(f"{path} 不是允许的取值，只能取：{' / '.join(allowed)}")
    return text


def _missing_record(param: str) -> str:
    """记录参数没传时的缺失说明：智能体在假设场景里常先漏传，写明补救办法它才会改传 {} 重新调用。"""
    return f"缺失（参数 {param} 未传；如果题面说明这条记录已提供、只是没给取值，请传 {param}={{}} 后重新调用）"


def _require_input_fields(record: object, param: str, fields: tuple[str, ...]) -> dict:
    """校验操作明确要求补全的记录和字段，再由业务函数核对值的类型和范围。"""
    if record is None:
        raise ValueError(f"请补全 {param} 记录")
    if not isinstance(record, dict):
        raise ValueError(f"{param} 应为对象")
    missing = [f"{param}.{field}" for field in fields if record.get(field) is None]
    if missing:
        raise ValueError(f"请补全 {'、'.join(missing)}")
    return record


def _yes_no(value) -> str | None:
    """「是/否」字段归一：图数据源把是/否列存成布尔值，查询结果原样拷贝过来就是 true/false。
    返回「是」「否」；取值缺失或认不出时返回 None，由调用方按资料不足处理。不需要时可删除。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    text = str(value).strip().casefold() if value is not None else ""
    if text in ("是", "true", "yes", "y"):
        return "是"
    if text in ("否", "false", "no", "n"):
        return "否"
    return None


def _cli() -> int:
    """仅在脚本被 CLI 直接调用时使用，返回进程退出码；纯库脚本可删除本函数与下方 __main__ 块。"""
    parser = argparse.ArgumentParser(description="{一句话业务描述}")
    # parser.add_argument("--arg1", required=True, help="{说明}")
    # parser.add_argument("--arg2", default=None, help="{说明}")
    # parser.add_argument("--flag", action="store_true", help="{布尔开关说明}")
    args = parser.parse_args()
    try:
        result = function_name(**vars(args))
    except ValueError as exc:
        # 入口校验给出要补全或更正的参数路径，避免回显取值。
        return _invalid_input(str(exc))
    except Exception as exc:  # 意料之外的异常：写清出错的步骤
        return _public_error("SCRIPT_FAILED", f"{{当前步骤}}出错（{type(exc).__name__}）")
    _emit(json.dumps(result, ensure_ascii=False, default=str))
    return 0


def _invalid_input(message: str) -> int:
    """参数不符合操作输入约定时输出 ok=false 与字段级原因，返回退出码 0。

    消费端据此提示用户补全或更正后再调用。允许缺失的资料按操作规则给出业务结论。
    """
    _emit(json.dumps({"ok": False, "error": {"code": "INVALID_INPUT", "message": message[:240]}}, ensure_ascii=False))
    return 0


def _public_error(code: str, message: str) -> int:
    """脚本自身异常：非 0 退出，consumer 会直接结束本轮对话，只记下这一行里的原因
    （code 为大写字母、数字、下划线，message 不超过 240 字）。返回退出码。"""
    _emit("ONTO_PUBLIC_ERROR:" + json.dumps({"code": code, "message": message[:240]}, ensure_ascii=False), sys.stderr)
    return 2


def _emit(text: str, stream=None) -> None:
    """把结果写到 stdout（或指定的流）。这是 consumer 读取的命令产物（stdout 一个 JSON、stderr 一行
    ONTO_PUBLIC_ERROR），不是运行日志，所以直接写流、不走 logging——日志级别或格式一变，consumer 就读不到。"""
    target = sys.stdout if stream is None else stream
    target.write(f"{text}\n")


if __name__ == "__main__":
    sys.exit(_cli())
