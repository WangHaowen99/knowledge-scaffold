"""DataAgent REST client service.

Provides call_query() and format_output() for the dataagent provider.

Usage:
    from agent.tools.business_data.providers.clients.dataagent import call_query, format_output

    result = call_query("http://localhost:9999", "token", "查询销售额")
    output = format_output(result, show_sql=False)
"""

from __future__ import annotations

import ast
import json
import re
import urllib.error
import urllib.request
import uuid

# DataAgent REST 协议常量
_DEFAULT_TIMEOUT = 300
_REST_PATH = "/a2a/rest/message:send"
_PROTOCOL_VERSION = "1.0"


def call_query(server: str, token: str, question: str, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """通过 A2A 1.0 REST 接口发起一次自然语言查询。

    返回归一化后的字典：
        {
          "success": bool,
          "data": {"columns": [...], "rows": [...]} | None,
          "sql": str | None,           # 始终带回，供 format_output 按 --with-sql 决定是否输出
          "row_count": int,
          "confidence": float | None,
          "error": str | None,
          "raw": {...}                  # 调试用：服务端返回的完整解析结果
        }
    """
    body = {
        "message": {
            "role": "ROLE_USER",
            "parts": [{"text": question}],
            "messageId": f"m-{uuid.uuid4().hex[:12]}",
            "contextId": f"c-{uuid.uuid4().hex[:8]}",
        },
        "configuration": {"returnImmediately": False},
    }

    resp = _post(server.rstrip("/") + _REST_PATH, token, body, timeout=timeout)
    if not resp.get("ok"):
        return {"success": False, "error": resp.get("error"), "data": None, "sql": None,
                "row_count": 0, "confidence": None, "raw": resp}

    return _parse_a2a_response(resp["body"])


def _post(url: str, token: str, body: dict, timeout: int) -> dict:
    """发送 HTTP POST 请求（A2A 1.0 必须带版本头）"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("A2A-Version", _PROTOCOL_VERSION)
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "body": json.loads(r.read().decode("utf-8"))}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(error_body)
            msg = detail.get("error", {}).get("message") or detail.get("detail") or detail
        except json.JSONDecodeError:
            msg = error_body
        return {"ok": False, "error": f"HTTP {e.code}: {msg}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"Connection failed: {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _parse_a2a_response(body: dict) -> dict:
    """解析 A2A REST 响应，提取 NL2SQL 的 sql/rows/columns/confidence。

    服务端把 NL2SQLAgent.chat() 返回的 state dict 字符串化后塞在
    task.status.message.parts[0].text 里。该字符串包含数据类对象的 repr
    （如 ValidationResult(score=...)），整体 ast.literal_eval 会失败，
    因此按字段名正则抽取关键值，逐字段解析。
    """
    task = body.get("task") or {}
    status = task.get("status") or {}
    state = status.get("state", "")
    err = status.get("error") or (body.get("error") or {}).get("message")

    # 失败态
    if state != "TASK_STATE_COMPLETED":
        msg_text = _extract_text(status.get("message") or {})
        reason = err or msg_text or f"task state: {state}"
        return {"success": False, "error": reason, "data": None, "sql": None,
                "row_count": 0, "confidence": None, "raw": body}

    text = _extract_text(status.get("message") or {})
    fields = _extract_fields(text)

    # 没抽到 sql，说明模型直接返回了自然语言答案（未走结构化结果）
    if not fields:
        return {"success": True, "data": None, "sql": None, "row_count": 0,
                "confidence": None, "answer": text[:500], "raw": body}

    sql = fields.get("sql")
    columns = fields.get("columns") or []
    rows = _normalize_rows(fields.get("rows_preview") or fields.get("rows") or [])
    confidence = fields.get("confidence")
    inner_error = fields.get("error")

    success = sql is not None and not inner_error
    return {
        "success": success,
        "data": {"columns": columns, "rows": rows} if columns or rows else None,
        "sql": sql,
        "row_count": len(rows),
        "confidence": confidence,
        "error": str(inner_error) if inner_error else None,
        "raw": body,
    }


# 需要从 state 字符串里抽取的字段及其值类型
_FIELD_PATTERNS = {
    "sql": re.compile(r"'sql':\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"),
    "columns": re.compile(r"'columns':\s*(\[[^\]]*\])"),
    "rows_preview": re.compile(r"'rows_preview':\s*(\[(?:[^\[\]]|\([^\(\)]*\))*\])"),
    "rows": re.compile(r"'rows':\s*(\[(?:[^\[\]]|\([^\(\)]*\))*\])"),
    "confidence": re.compile(r"'confidence':\s*([0-9]+\.?[0-9]*|None|True|False)"),
    "error": re.compile(r"'error':\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|None)"),
}


def _extract_fields(text: str) -> dict:
    """从 state 字符串里按字段名抽取值，逐字段 ast.literal_eval。"""
    out: dict = {}
    for key, pat in _FIELD_PATTERNS.items():
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1)
        val = _safe_literal_eval(raw)
        if val is None and key != "error":
            # 抽到了但解析失败：保留原始字符串便于排查
            val = raw
        out[key] = val
    return out


def _extract_text(message: dict) -> str:
    parts = message.get("parts") or []
    for p in parts:
        if isinstance(p, dict) and p.get("text"):
            return str(p["text"])
        if isinstance(p, str):
            return p
    return ""


def _safe_literal_eval(text: str):
    """安全解析 Python 字面量；失败返回 None。"""
    if not text:
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def _normalize_rows(rows) -> list[list]:
    """把 tuple/行记录统一成 list[list]，便于 JSON 序列化。"""
    out: list[list] = []
    for r in rows or []:
        if isinstance(r, (list, tuple)):
            out.append([_coerce(v) for v in r])
        else:
            out.append([_coerce(r)])
    return out


def _coerce(v):
    """Decimal/datetime 等不可 JSON 序列化的值转 str。"""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def format_output(result: dict, show_sql: bool = False) -> dict:
    """按参考客户端的风格归一化最终输出。"""
    output: dict = {
        "success": bool(result.get("success", False)),
        "data": result.get("data"),
    }
    if "row_count" in result:
        output["row_count"] = result["row_count"]
    if result.get("confidence") is not None:
        output["confidence"] = result["confidence"]
    if show_sql and result.get("sql"):
        output["sql"] = result["sql"]
    if not show_sql and result.get("sql"):
        # 即便不展示 SQL，也保留 sql 生成的标记，方便上游判断是否走通
        output["has_sql"] = True

    if output["success"]:
        n = output.get("row_count", 0)
        if n > 0:
            output["message"] = f"查询成功，返回 {n} 行数据"
        elif result.get("answer"):
            output["message"] = f"查询成功：{result['answer']}"
        else:
            output["message"] = "查询成功，无数据返回"
    else:
        output["message"] = f"查询失败: {result.get('error', '未知错误')}"
    return output
