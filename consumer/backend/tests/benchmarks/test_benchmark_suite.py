"""
==========================================================================
  test_benchmark_suite.py — Benchmark 自动化测试 (独立版)
==========================================================================

  【概述】
  对后端 Agent 服务进行批量基准测试，收集耗时、token、工具调用等指标，
  自动写入 Excel 和 Markdown 报告。

  【前置条件】
  1. 复制 .env.example 为 .env 并修改配置
  2. 确保后端服务已启动且可访问
  3. 安装依赖: pip install requests openpyxl langdetect openai

  【运行方式】
    python test_benchmark_suite.py

  【配置方式】
  所有参数通过 .env 文件或环境变量设置，无需修改源码:

    BASE_URL              后端服务地址 (默认 http://localhost:8000)
    STREAM_PATH           Agent 流式接口路径
    SESSIONS_PATH         Sessions 接口路径
    WORKSPACE_ID          工作区 ID
    RUNS_PER_QUESTION     每个问题重复次数 (默认 1)
    ACTIVE_TEST_GROUP     测试组: eval / flywheel / other / all
    EVAL_SAMPLE_SIZE      评估用例随机抽取数 (0=全部)
    TEST_LANGUAGE         测试语言: zh / en / ja / ko / 随机
    CHINESE_DETECT_MIN_SEVERITY  中文混入检测级别: info / warn / error
    LLM_API_KEY           LLM API Key (用于翻译/语言检测)
    LLM_BASE_URL          LLM Base URL
    LLM_MODEL_NAME        LLM 模型名称
==========================================================================
"""

from __future__ import annotations

import json
import time
import re
import random
import uuid
import requests
import openpyxl
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from copy import copy
import os
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from lang import translate_text, get_lang_display_name, SUPPORTED_LANGUAGES, get_env, _LANG_DISPLAY_NAMES

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_URL = get_env("BASE_URL", "http://localhost:5055")
API_MODE = get_env("API_MODE", "agentic")  # agentic | acp
TEST_QUESTION_LIMIT = int(get_env("TEST_QUESTION_LIMIT", "0"))  # 0=all, N=first N
STREAM_PATH = get_env("STREAM_PATH", "/agentic/runs/stream")
SESSIONS_PATH = get_env("SESSIONS_PATH", "/agentic/sessions")
ACP_EXECUTE_STREAM_PATH = get_env("ACP_EXECUTE_STREAM_PATH", "/acp/runs/execute_stream")
ACP_INTERACT_STREAM_PATH = get_env("ACP_INTERACT_STREAM_PATH", "/acp/runs/{run_id}/interact_stream")
ACP_SUBMODE = get_env("ACP_SUBMODE", "execute_stream")  # execute_stream | stream | execute
ACP_AUTO_REPLY = get_env("ACP_AUTO_REPLY", "继续")
ACP_LANG = get_env("ACP_LANG", None)  # 单独控制 ACP 传入的 lang 参数，不设置则使用 lang_code
ACP_SUPPRESS_SCRIPT_PATTERNS = get_env("ACP_SUPPRESS_SCRIPT_PATTERNS", "")  # ACP 输出过滤脚本模式, 逗号分隔
_RESULT_BASE_DIR = get_env("BENCHMARK_RESULT_DIR") or os.path.join(_TEST_DIR, "result")
_current_run_dir: str | None = None
_current_xlsx_path: str | None = None
RUNS_PER_QUESTION = int(get_env("RUNS_PER_QUESTION", "1"))
RUNS_EXECUTION_MODE = get_env("RUNS_EXECUTION_MODE", "serial")
RUNS_MAX_CONCURRENCY = max(1, int(get_env("RUNS_MAX_CONCURRENCY", "4")))

WORKSPACE_ID = get_env("WORKSPACE_ID", "default")

_TEST_CASE_DIR = _TEST_DIR

_CASES_JSON_PATH = os.path.join(_TEST_CASE_DIR, "original_cases", "cases.json")

_EMBEDDED_FLYWHEEL = [
    {"file": "数据飞轮用例 1 - 故障的实际根因在推荐根因清单中.json", "sheet": "问题1"},
    {"file": "数据飞轮用例 2 - 故障的实际根因不在推荐根因清单中.json", "sheet": "问题2"},
    {"file": "数据飞轮用例 3 - 故障的实际根因在推荐根因清单中.json", "sheet": "问题3"},
    {"file": "数据飞轮用例 4 - 故障的实际根因不在推荐根因清单中.json", "sheet": "问题4"},
]

_EMBEDDED_EVAL = [
    {"sheet": "问题5", "message": "请求故障诊断：发动机动力不足，设备编号是XX，车辆型号是XX"},
]

_EMBEDDED_OTHER = [
    {"sheet": "问题15", "message": "设备编码：XX，车不动了，电源继电器无工作声响，请求故障分析"},
]


def _load_cases_from_json() -> dict | None:
    if not os.path.exists(_CASES_JSON_PATH):
        return None
    try:
        with open(_CASES_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠ 读取 cases.json 失败: {e}，使用内嵌用例")
        return None


_loaded = _load_cases_from_json()

if _loaded:
    _DATA_FLYWHEEL_CASES = _loaded.get("flywheel", _EMBEDDED_FLYWHEEL)
    _EVAL_CASES = _loaded.get("eval", _EMBEDDED_EVAL)
    _OTHER_CASES = _loaded.get("other", _EMBEDDED_OTHER)
else:
    _DATA_FLYWHEEL_CASES = _EMBEDDED_FLYWHEEL
    _EVAL_CASES = _EMBEDDED_EVAL
    _OTHER_CASES = _EMBEDDED_OTHER

ACTIVE_TEST_GROUP = get_env("ACTIVE_TEST_GROUP", "eval")

_eval_sample = get_env("EVAL_SAMPLE_SIZE", "2")
EVAL_SAMPLE_SIZE = 0 if _eval_sample == "0" else int(_eval_sample or "2")

TEST_LANGUAGE = get_env("BENCHMARK_LANG") or get_env("TEST_LANGUAGE", "en")

CHINESE_DETECT_MIN_SEVERITY = get_env("CHINESE_DETECT_MIN_SEVERITY", "info")


def _char_count(text: str, lo: int, hi: int) -> int:
    return sum(1 for c in text if lo <= ord(c) <= hi)


def _llm_check_chinese_intrusion(text: str, target_lang: str) -> dict | None:
    try:
        from openai import OpenAI
        from lang import _get_llm_config_from_env
        llm_config = _get_llm_config_from_env()
        if not llm_config.get("api_key"):
            return None
        client = OpenAI(api_key=llm_config["api_key"], base_url=llm_config["base_url"])
        model_name = llm_config["model_name"]
    except Exception:
        return None

    lang_name = {"ja": "Japanese", "ko": "Korean"}.get(target_lang, target_lang)
    sample = text[:600]
    prompt = (
        "You are a language detection expert. Determine whether the following text contains "
        "Chinese (Mandarin) content that is NOT part of the expected language.\n"
        "Expected language: " + lang_name + " (" + target_lang + ").\n"
        "Note: " + lang_name + " may use CJK characters that look similar to Chinese. "
        "Distinguish based on vocabulary, grammar, and context.\n\n"
        "Respond in JSON format:\n"
        '{"has_chinese": true/false, "confidence": 0.0-1.0, "reason": "brief explanation"}\n\n'
        "Text:\n" + sample
    )
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
            timeout=30,
        )
        raw = response.choices[0].message.content.strip()
        json_match = re.search(r'\{[^}]+\}', raw)
        if json_match:
            result = json.loads(json_match.group())
            return {
                "has_chinese": bool(result.get("has_chinese", False)),
                "confidence": float(result.get("confidence", 0.5)),
                "reason": result.get("reason", ""),
            }
    except Exception:
        pass
    return None


def _heuristic_cjk_classify(text: str, target_lang: str) -> dict | None:
    if target_lang == "ja":
        kana_count = _char_count(text, 0x3040, 0x309F) + _char_count(text, 0x30A0, 0x30FF)
        if kana_count > 0:
            cjk_count = _char_count(text, 0x4E00, 0x9FFF)
            if kana_count / (cjk_count + kana_count) > 0.1:
                return {"category": "none", "severity": "none", "confidence": 0.9,
                        "description": "日文汉字(含假名，非中文)"}

    if target_lang == "ko":
        hangul_count = _char_count(text, 0xAC00, 0xD7AF)
        if hangul_count > 0:
            cjk_count = _char_count(text, 0x4E00, 0x9FFF)
            if hangul_count / (cjk_count + hangul_count) > 0.1:
                return {"category": "none", "severity": "none", "confidence": 0.9,
                        "description": "韩文汉字(含韩文，非中文)"}

    return None


def _classify_chinese_finding(text: str, event_type: str, target_lang: str = "") -> dict:
    has_cjk = any("\u4e00" <= c <= "\u9fff" for c in text)
    if not has_cjk:
        return {"category": "none", "severity": "none", "confidence": 1.0, "description": "无中文"}

    if target_lang in ("ja", "ko"):
        heuristic = _heuristic_cjk_classify(text, target_lang)
        if heuristic:
            return heuristic

        llm_result = _llm_check_chinese_intrusion(text, target_lang)
        if llm_result is not None:
            if not llm_result["has_chinese"]:
                return {
                    "category": "none",
                    "severity": "none",
                    "confidence": llm_result["confidence"],
                    "description": "LLM判定非中文 (%s)" % llm_result.get("reason", ""),
                }
            cjk_ratio = _char_count(text, 0x4E00, 0x9FFF) / max(len(text.replace(" ", "")), 1)
            if cjk_ratio < 0.15:
                return {
                    "category": "minor_mix",
                    "severity": "warn",
                    "confidence": llm_result["confidence"],
                    "description": "LLM判定含中文: %s" % llm_result.get("reason", ""),
                }
            elif cjk_ratio > 0.6:
                return {
                    "category": "pure_chinese",
                    "severity": "error",
                    "confidence": llm_result["confidence"],
                    "description": "LLM判定中文混入: %s" % llm_result.get("reason", ""),
                }
            else:
                return {
                    "category": "mixed_language",
                    "severity": "error",
                    "confidence": llm_result["confidence"],
                    "description": "LLM判定中文混入: %s" % llm_result.get("reason", ""),
                }

    filename_pattern = re.compile(r'[\u4e00-\u9fff\w\-]+\.(?:md|txt|json|py|yaml|yml|csv|xml|docx|xlsx|pdf)')
    quoted_chinese_pattern = re.compile(r'[""\u201c\u201d`](?:[^"`\u201c\u201d]*[\u4e00-\u9fff][^"`\u201c\u201d]*)[""\u201d`\u201c]')
    markdown_bold_pattern = re.compile(r'\*\*[^*]*[\u4e00-\u9fff][^*]*\*\*')

    chinese_chars = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    total_chars = len(text.replace(" ", ""))
    if total_chars == 0:
        total_chars = 1
    chinese_ratio = len(chinese_chars) / total_chars

    filename_matches = filename_pattern.findall(text)
    quoted_matches = quoted_chinese_pattern.findall(text)
    bold_matches = markdown_bold_pattern.findall(text)

    if chinese_ratio > 0.6:
        return {
            "category": "pure_chinese",
            "severity": "error",
            "confidence": 0.95,
            "description": "文本主体为中文",
        }

    if filename_matches:
        non_filename_text = filename_pattern.sub("", text)
        non_filename_chinese = sum(1 for c in non_filename_text if "\u4e00" <= c <= "\u9fff")
        if non_filename_chinese <= 2:
            return {
                "category": "filename_reference",
                "severity": "info",
                "confidence": 0.9,
                "description": "引用中文文件名: %s" % ", ".join(filename_matches),
            }

    all_quoted = quoted_matches + bold_matches
    if all_quoted:
        cleaned = text
        for qm in all_quoted:
            cleaned = cleaned.replace(qm, "")
        remaining_chinese = sum(1 for c in cleaned if "\u4e00" <= c <= "\u9fff")
        if remaining_chinese <= 2:
            return {
                "category": "quoted_instruction",
                "severity": "warn",
                "confidence": 0.85,
                "description": "引用中文指令/流程步骤",
            }

    if chinese_ratio < 0.15:
        return {
            "category": "minor_mix",
            "severity": "warn",
            "confidence": 0.8,
            "description": "少量中文混入 (占比 %.1f%%)" % (chinese_ratio * 100),
        }

    return {
        "category": "mixed_language",
        "severity": "error",
        "confidence": 0.9,
        "description": "中英混杂 (中文占比 %.1f%%)" % (chinese_ratio * 100),
    }


def _sample_eval_cases() -> list[dict]:
    if not EVAL_SAMPLE_SIZE or EVAL_SAMPLE_SIZE >= len(_EVAL_CASES):
        return list(_EVAL_CASES)
    return random.sample(_EVAL_CASES, EVAL_SAMPLE_SIZE)

def _resolve_test_language() -> str | None:
    if not TEST_LANGUAGE or TEST_LANGUAGE == "原语言":
        return None
    if TEST_LANGUAGE == "随机":
        return random.choice(SUPPORTED_LANGUAGES)
    return TEST_LANGUAGE

def _translate_json_values(obj, target_lang: str):
    if isinstance(obj, str):
        return translate_text(obj, target_lang, source_lang="zh")
    elif isinstance(obj, list):
        return [_translate_json_values(item, target_lang) for item in obj]
    elif isinstance(obj, dict):
        return {k: _translate_json_values(v, target_lang) for k, v in obj.items()}
    return obj


_TRANSLATED_CACHE_DIR = os.path.join(_TEST_CASE_DIR, "translated_cases")


def _load_translated_cache(lang: str) -> dict | None:
    cache_path = os.path.join(_TRANSLATED_CACHE_DIR, lang, f"cases_{lang}.json")
    if not os.path.exists(cache_path):
        return None
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {case["sheet"]: case for case in data.get("cases", [])}


def _translate_question(q: dict, cache: dict | None = None, target_lang_override: str | None = None) -> dict:
    target_lang = target_lang_override or _resolve_test_language()
    if not target_lang:
        return {**q, "lang": "zh", "lang_display": "中文"}

    lang_name = get_lang_display_name(target_lang)
    original_message = q["message"]
    original_sheet = q["sheet"]

    if target_lang in ("zh", "zh-cn"):
        print(f"  🌐 {original_sheet}: 中文 (zh)")
        return {**q, "lang": "zh", "lang_display": "中文", "original_sheet": original_sheet, "original_message": original_message}

    if cache and original_sheet in cache:
        cached = cache[original_sheet]
        print(f"  🌐 {original_sheet}: {lang_name} ({target_lang}) [cached]")
        return {
            "sheet": original_sheet,
            "message": cached["message"],
            "original_sheet": original_sheet,
            "original_message": original_message,
            "lang": target_lang,
            "lang_display": lang_name,
        }

    if target_lang in ("zh", "zh-cn"):
        translated_message = original_message
    else:
        json_data = q.get("json_data")
        text_to_translate = q.get("text_suffix", original_message)

        try:
            translated_text = translate_text(text_to_translate, target_lang, source_lang="zh")
        except Exception as e:
            print(f"  ⚠ 翻译失败 [{original_sheet}]: {e}，使用原文")
            translated_text = text_to_translate

        if json_data is not None:
            try:
                translated_json = _translate_json_values(json_data, target_lang)
                json_prefix = json.dumps(translated_json, ensure_ascii=False)
            except Exception as e:
                print(f"  ⚠ JSON翻译失败 [{original_sheet}]: {e}，使用原JSON")
                json_prefix = json.dumps(json_data, ensure_ascii=False)
            translated_message = f"{json_prefix}\n{translated_text}"
        else:
            translated_message = translated_text

    print(f"  🌐 {original_sheet}: {lang_name} ({target_lang}) [online]")
    return {
        "sheet": original_sheet,
        "message": translated_message,
        "original_sheet": original_sheet,
        "original_message": original_message,
        "lang": target_lang,
        "lang_display": lang_name,
    }

def _load_flywheel_questions() -> list[dict]:
    questions = []
    original_dir = os.path.join(_TEST_CASE_DIR, "original_cases")
    fallback_dir = _TEST_CASE_DIR
    for tc in _DATA_FLYWHEEL_CASES:
        filepath = os.path.join(original_dir, tc["file"])
        if not os.path.exists(filepath):
            filepath = os.path.join(fallback_dir, tc["file"])
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        json_str = json.dumps(data, ensure_ascii=False)
        text_suffix = "调用故障数据飞轮流程，严格按照故障数据飞轮的输出格式输出"
        questions.append({
            "sheet": tc["sheet"],
            "message": f"{json_str}\n{text_suffix}",
            "json_data": data,
            "text_suffix": text_suffix,
        })
    return questions

def _resolve_questions() -> list[dict]:
    eval_cases = _sample_eval_cases()
    if ACTIVE_TEST_GROUP == "flywheel":
        base_questions = _load_flywheel_questions()
    elif ACTIVE_TEST_GROUP == "other":
        base_questions = list(_OTHER_CASES)
    elif ACTIVE_TEST_GROUP == "all":
        base_questions = _load_flywheel_questions() + eval_cases + list(_OTHER_CASES)
    else:
        base_questions = eval_cases

    if TEST_LANGUAGE and TEST_LANGUAGE != "原语言":
        print(f"\n🌐 测试语言模式: {TEST_LANGUAGE} — 每个用例独立翻译")
        if TEST_LANGUAGE == "随机":
            _cache_pool: dict[str, dict] = {}
            results = []
            for q in base_questions:
                lang = _resolve_test_language()
                if lang and lang not in _cache_pool:
                    _cache_pool[lang] = _load_translated_cache(lang)
                results.append(_translate_question(q, _cache_pool.get(lang), target_lang_override=lang))
            return results
        cache = _load_translated_cache(TEST_LANGUAGE)
        return [_translate_question(q, cache) for q in base_questions]
    return [{**q, "lang": "zh", "lang_display": "中文"} for q in base_questions]


QUESTIONS = _resolve_questions()


AUTH_USERNAME = get_env("AUTH_USERNAME")
AUTH_PASSWORD = get_env("AUTH_PASSWORD")

_AUTH_TOKEN: str | None = None


def _ensure_auth() -> str:
    global _AUTH_TOKEN
    if _AUTH_TOKEN:
        return _AUTH_TOKEN
    if not AUTH_USERNAME or not AUTH_PASSWORD:
        raise RuntimeError("AUTH_USERNAME and AUTH_PASSWORD must be set for benchmark login")
    resp = requests.post(
        f"{BASE_URL}/auth/login",
        json={"username": AUTH_USERNAME, "password": AUTH_PASSWORD},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    _AUTH_TOKEN = resp.json()["token"]
    return _AUTH_TOKEN


def _headers() -> dict:
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {_ensure_auth()}"}
    if WORKSPACE_ID:
        h["X-Workspace-Id"] = WORKSPACE_ID
    return h


def create_session() -> str:
    resp = requests.post(
        f"{BASE_URL}{SESSIONS_PATH}",
        json={"title": "benchmark-test-session"},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def generate_session_title(session_id: str, title: str):
    resp = requests.post(
        f"{BASE_URL}{SESSIONS_PATH}/{session_id}/generate-title",
        json={"message": title},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("title", title)


_lang_follow_enabled: bool | None = None


def _check_lang_follow_enabled() -> bool | None:
    global _lang_follow_enabled
    if _lang_follow_enabled is not None:
        return _lang_follow_enabled
    try:
        ws_id = WORKSPACE_ID or "default"
        resp = requests.get(
            f"{BASE_URL}/agentic/workspaces/{ws_id}/runtime-config",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            config = resp.json()
            _lang_follow_enabled = bool(config.get("lang", {}).get("enabled", False))
            return _lang_follow_enabled
    except Exception as e:
        print(f"  ⚠ 无法获取 runtime_config ({e})，默认开启中文混入检测")
    return None


def _set_acp_suppress_patterns(patterns: str) -> bool:
    """设置工作区的 ACP 输出过滤脚本模式 (acp_suppress_script_patterns)。
    patterns: 逗号分隔的脚本路径关键字，如 "mindsdb,work_order_sync"
    返回是否设置成功。
    """
    try:
        ws_id = WORKSPACE_ID or "default"
        resp = requests.patch(
            f"{BASE_URL}/agentic/workspaces/{ws_id}/runtime-config",
            json={"acp_suppress_script_patterns": patterns},
            headers=_headers(),
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"  ⚠ 设置 acp_suppress_script_patterns 失败: {e}")
        return False


def _get_acp_suppress_patterns() -> str:
    """获取当前工作区的 ACP 输出过滤脚本模式"""
    try:
        ws_id = WORKSPACE_ID or "default"
        resp = requests.get(
            f"{BASE_URL}/agentic/workspaces/{ws_id}/runtime-config",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("acp_suppress_script_patterns", "")
    except Exception:
        pass
    return ""


_OPERATION_SCRIPT_MAP = {
    "故障知识更新": "work_order_sync.py",
    "Fault knowledge update": "work_order_sync.py",
    "调用产品研发机理接口": "knowledge_graph_tool.py",
    "Call product R&D mechanism interface": "knowledge_graph_tool.py",
    "call product research and development mechanism interface": "knowledge_graph_tool.py",
    "调用故障历史记录接口": "knowledge_graph_tool.py",
    "Call fault history record interface": "knowledge_graph_tool.py",
    "call historical fault record interface": "knowledge_graph_tool.py",
    "业务数据查询": "business_data_search.py",
    "Business data query": "business_data_search.py",
    "查询业务数据": "business_data_search.py",
    "Query business data": "business_data_search.py",
}


_KG_OP_KEYWORDS = ["产品研发机理", "故障历史记录", "product R&D mechanism", "historical fault record"]


def _extract_kg_input_from_text(text: str, kg_tool_inputs: list) -> None:
    import re as _re
    for pm in _re.finditer(r"输入参数:", text):
        brace_start = text.find("{", pm.end())
        if brace_start == -1:
            continue
        balance = 1
        escaped = False
        in_string = False
        idx = brace_start + 1
        end = -1
        while idx < len(text) and balance > 0:
            c = text[idx]
            if escaped:
                escaped = False
            elif c == '\\' and in_string:
                escaped = True
            elif c == '"':
                in_string = not in_string
            elif not in_string:
                if c == '{':
                    balance += 1
                elif c == '}':
                    balance -= 1
                    if balance == 0:
                        end = idx + 1
                        break
            idx += 1
        if end == -1:
            continue
        candidate = text[brace_start:end]
        try:
            params = json.loads(candidate)
        except Exception:
            try:
                import ast
                params = ast.literal_eval(candidate)
            except Exception:
                continue
        if not isinstance(params, dict):
            continue
        op_key = params.get("operation_key", "")
        if any(kw in op_key for kw in _KG_OP_KEYWORDS):
            kg_tool_inputs.append({
                "tool_call_id": "",
                "kind": "input",
                "raw_message": candidate,
                "parameters": params,
            })


def parse_acp_sse_stream(response_text: str) -> dict:
    """
    解析 ACP execute_stream 协议的 SSE 响应文本。

    支持两种 ACP 响应格式:
    1. ACP 原生协议: event: thinking | data: {"text": "...", "done": false}
    2. 嵌套 agent.v2 协议: event: | data: {"type":"", "data":{"event":"agent.v2", "data":{...}}}

    当检测到嵌套 agent.v2 格式时，提取内层事件数据，委托给 parse_sse_stream 处理，
    并补充 ACP 特有字段 (acp_completeness 等)。
    """
    outer_events: list[tuple[str, dict]] = []
    lines = response_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
            i += 1
            data_str = ""
            while i < len(lines) and lines[i].startswith("data:"):
                data_str += lines[i][len("data:"):].strip()
                i += 1
            if not data_str:
                continue
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            outer_events.append((event_name, data))
        else:
            i += 1

    is_nested_v2 = False
    if outer_events:
        for _ename, edata in outer_events:
            if isinstance(edata, dict):
                inner = edata.get("data")
                if isinstance(inner, dict) and inner.get("event") == "agent.v2":
                    is_nested_v2 = True
                    break
            if isinstance(edata, dict) and edata.get("protocol") == "agent-stream.v2":
                is_nested_v2 = True
                break

    if is_nested_v2:
        inner_lines: list[str] = []
        for _ename, edata in outer_events:
            inner_data = edata
            if isinstance(edata, dict) and "data" in edata:
                candidate = edata.get("data")
                if isinstance(candidate, dict) and candidate.get("event") == "agent.v2":
                    inner_data = candidate.get("data", edata)
            inner_lines.append("event: agent.v2")
            inner_lines.append("data: " + json.dumps(inner_data, ensure_ascii=False))
        inner_response_text = "\n".join(inner_lines)
        result = parse_sse_stream(inner_response_text)

        acp_awaiting: list[dict] = []

        result["acp_outer_raw_events"] = [{"event": en, "data": ed} for en, ed in outer_events]
        result["acp_completeness"] = {
            "has_thinking": bool(result.get("final_output")),
            "has_result": bool(result.get("final_output")),
            "has_answer": bool(result.get("final_output")),
            "answer_done": bool(result.get("final_output")),
            "run_completed": result.get("error") is None,
            "run_failed": result.get("error") is not None,
            "content_complete": result.get("error") is None and bool(result.get("final_output")),
            "completeness_label": "complete" if result.get("error") is None and result.get("final_output") else "incomplete",
        }
        result["acp_thinking_text"] = ""
        result["acp_result_text"] = ""
        result["acp_answer_text"] = result.get("final_output", "")
        result["acp_awaiting_events"] = acp_awaiting
        result["acp_progress_events"] = []
        result["acp_tool_events"] = result.get("tool_calls", [])
        return result

    run_id = None
    session_id = None
    thinking_parts: list[str] = []
    result_parts: list[str] = []
    answer_parts: list[str] = []
    error = None
    raw_events: list[dict] = []
    progress_events: list[dict] = []
    tool_events: list[dict] = []

    run_completed = False
    run_failed = False
    awaiting_events: list[dict] = []

    first_event_ts = None
    first_answer_ts = None

    script_stats: dict[str, dict] = {}
    script_call_details: list[dict] = []
    kg_tool_outputs: list[dict] = []
    kg_tool_inputs: list[dict] = []

    for event_name, data in outer_events:
        raw_events.append({"event": event_name, "data": data})
        event_ts = data.get("ts") or time.time() * 1000
        if first_event_ts is None:
            first_event_ts = event_ts

        if event_name in ("run.created", "run.started"):
            run_id = data.get("run_id") or run_id
            session_id = data.get("session_id") or session_id

        elif event_name == "thinking":
            text = data.get("text", "")
            done = data.get("done", False)
            if text:
                thinking_parts.append(text)
                _extract_kg_input_from_text(text, kg_tool_inputs)
            if done:
                thinking_text = "".join(thinking_parts)
                progress_events.append({
                    "type": "thinking",
                    "text": thinking_text,
                    "done": True,
                })
                tool_events.append({
                    "type": "thinking",
                    "text": thinking_text,
                    "done": True,
                })

        elif event_name == "result":
            text = data.get("text", "")
            done = data.get("done", False)
            tool_name = data.get("tool_name", "")
            tool_result = data.get("result")
            tool_status = data.get("tool_status", "")
            elapsed_ms = data.get("elapsed_ms", 0)
            operation_key = data.get("operation_key", "")
            if text:
                result_parts.append(text)
            if tool_name:
                tool_events.append({
                    "type": "tool.finished",
                    "name": tool_name,
                    "status": tool_status,
                    "elapsed_ms": elapsed_ms,
                    "result": tool_result,
                    "operation_key": operation_key,
                })
                matched_script = None
                match_source = operation_key or tool_name
                for op_key, script_file in _OPERATION_SCRIPT_MAP.items():
                    if op_key in match_source:
                        matched_script = script_file
                        break
                if not matched_script and tool_name.endswith(".py"):
                    matched_script = tool_name
                if matched_script:
                    stats = script_stats.setdefault(matched_script, {"count": 0, "total_result_length": 0})
                    stats["count"] += 1
                    result_val = tool_result if isinstance(tool_result, str) else (json.dumps(tool_result, ensure_ascii=False) if tool_result else "")
                    stats["total_result_length"] += len(result_val)
                    script_call_details.append({
                        "name": matched_script,
                        "status": tool_status or "success",
                        "result_length": len(result_val),
                    })
                if matched_script == "knowledge_graph_tool.py" or tool_name == "knowledge_graph_tool.py":
                    if tool_result:
                        raw_output = tool_result if isinstance(tool_result, str) else json.dumps(tool_result, ensure_ascii=False)
                        kg_tool_outputs.append({
                            "tool_call_id": "",
                            "raw_output": raw_output,
                        })
            if done:
                progress_events.append({
                    "type": "result",
                    "text": "".join(result_parts),
                    "done": True,
                })

        elif event_name == "answer":
            text = data.get("text", "")
            done = data.get("done", False)
            if text:
                answer_parts.append(text)
            if first_answer_ts is None and text:
                first_answer_ts = event_ts
            if done:
                progress_events.append({
                    "type": "answer",
                    "text": "".join(answer_parts),
                    "done": True,
                })

        elif event_name == "run.awaiting":
            question = data.get("question", "")
            tool_call_id = data.get("tool_call_id", "")
            options = data.get("options", [])
            awaiting_events.append({
                "question": question,
                "tool_call_id": tool_call_id,
                "options": options,
            })

        elif event_name == "run.completed":
            run_completed = True

        elif event_name == "run.failed":
            run_failed = True
            error = data.get("error", "Unknown error")

        elif event_name == "progress":
            progress_events.append({
                "type": data.get("type", "progress"),
                "text": data.get("text", ""),
                "done": data.get("done", False),
            })

        elif event_name == "tool_call":
            tool_name = data.get("name", "")
            tool_args = data.get("args", {})
            tool_result = data.get("result")
            tool_events.append({
                "type": "tool_call",
                "name": tool_name,
                "args": tool_args,
                "result": tool_result,
            })

            matched_script = None
            script_match = re.search(r"执行脚本[:\s]+(\S+\.py)", tool_name)
            if script_match:
                matched_script = script_match.group(1)
            else:
                for op_key, script_file in _OPERATION_SCRIPT_MAP.items():
                    if op_key in tool_name or (isinstance(tool_args, dict) and op_key in json.dumps(tool_args, ensure_ascii=False)):
                        matched_script = script_file
                        break

            if not matched_script and tool_name.endswith(".py"):
                matched_script = tool_name

            if matched_script:
                stats = script_stats.setdefault(matched_script, {"count": 0, "total_result_length": 0})
                stats["count"] += 1
                result_val = tool_result if isinstance(tool_result, str) else (json.dumps(tool_result, ensure_ascii=False) if tool_result else "")
                stats["total_result_length"] += len(result_val)
                script_call_details.append({
                    "name": matched_script,
                    "status": "success" if tool_result is not None else "pending",
                    "result_length": len(result_val),
                })

            if matched_script == "knowledge_graph_tool.py" or tool_name == "knowledge_graph_tool.py":
                if tool_args:
                    kg_tool_inputs.append({
                        "tool_call_id": "",
                        "kind": "input",
                        "raw_message": json.dumps(tool_args, ensure_ascii=False),
                    })
                if tool_result:
                    raw_output = tool_result if isinstance(tool_result, str) else json.dumps(tool_result, ensure_ascii=False)
                    kg_tool_outputs.append({
                        "tool_call_id": "",
                        "raw_output": raw_output,
                    })

    result_text = "".join(thinking_parts + result_parts)
    answer_text = "".join(answer_parts)
    all_text = "".join(thinking_parts + result_parts + answer_parts)

    completeness_label = "incomplete"
    has_thinking = len(thinking_parts) > 0
    has_result = len(result_parts) > 0
    has_answer = len(answer_parts) > 0
    answer_done = any(e.get("type") == "answer" and e.get("done") for e in progress_events)
    content_complete = has_answer and answer_done and (run_completed or (not run_failed))

    if run_completed and has_answer and answer_done:
        completeness_label = "complete"
    elif has_answer and answer_done and not run_completed:
        completeness_label = "content_complete_run_not_finished"
    elif run_failed and has_answer and answer_done:
        completeness_label = "failed_but_answer_done"
    elif run_failed:
        completeness_label = "failed_no_answer"
    elif has_answer and not answer_done:
        completeness_label = "partial_answer"

    completeness = {
        "has_thinking": has_thinking,
        "has_result": has_result,
        "has_answer": has_answer,
        "answer_done": answer_done,
        "run_completed": run_completed,
        "run_failed": run_failed,
        "content_complete": content_complete,
        "completeness_label": completeness_label,
    }

    turn_count = len([e for e in progress_events if e.get("type") in ("thinking", "result") and e.get("done")])

    for te in tool_events:
        name = te.get("name", "")
        if name and name not in script_stats:
            stats = script_stats.setdefault(name, {"count": 0, "total_result_length": 0})
            stats["count"] += 1
            result_val = te.get("result")
            if isinstance(result_val, str):
                stats["total_result_length"] += len(result_val)

    final_output = answer_text.strip() or None

    return {
        "run_id": run_id,
        "session_id": session_id,
        "error": error,
        "raw_events": raw_events,
        "final_output": final_output,
        "answer_text": answer_text,
        "result_text": result_text,
        "all_text": all_text,
        "turn_count": turn_count,
        "script_stats": script_stats,
        "tool_calls": tool_events,
        "progress_events": progress_events,
        "dag_perf": {},
        "llm_call_count": 0,
        "acp_completeness": completeness,
        "acp_thinking_text": "".join(thinking_parts),
        "acp_result_text": "".join(result_parts),
        "acp_answer_text": answer_text,
        "acp_awaiting_events": awaiting_events,
        "acp_progress_events": progress_events,
        "acp_tool_events": tool_events,
        "script_call_details": script_call_details,
        "kg_tool_outputs": kg_tool_outputs,
        "kg_tool_inputs": kg_tool_inputs,
    }


def _consume_sse_stream(resp: requests.Response) -> str:
    terminal_events = {"run.completed", "run.failed", "run.awaiting"}
    buf = ""
    current_event = ""
    try:
        while True:
            raw_line = resp.raw.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            buf += line + "\n"
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
            elif not line and current_event in terminal_events:
                break
    except Exception:
        pass
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return buf


def call_skill_execution_acp(message: str, session_id: str | None = None, lang: str | None = None) -> dict:
    """
    通过 ACP execute_stream 接口调用 Agent，自动处理 run.awaiting 交互。

    与 call_skill_execution 返回格式兼容，使 _execute_single_run 无需修改。
    """
    payload = {
        "messages": [
            {
                "role": "user",
                "parts": [{"type": "text", "content": message}],
            }
        ],
        "context": {"session_id": session_id} if session_id else {},
        "metadata": {},
    }
    if lang:
        payload["lang"] = lang

    current_url = f"{BASE_URL}{ACP_EXECUTE_STREAM_PATH}"
    current_data = payload
    combined_response_text = ""
    run_id = None

    while True:
        resp = requests.post(current_url, json=current_data, headers=_headers(), timeout=(10, 180), stream=True)
        resp.raise_for_status()
        combined_response_text += _consume_sse_stream(resp)

        partial = parse_acp_sse_stream(combined_response_text)
        if not run_id:
            run_id = partial.get("run_id")

        awaiting = partial.get("acp_awaiting_events", [])
        if awaiting:
            last_await = awaiting[-1]
            tool_call_id = last_await.get("tool_call_id", "")
            if not run_id:
                break
            interact_path = ACP_INTERACT_STREAM_PATH.replace("{run_id}", run_id)
            current_url = f"{BASE_URL}{interact_path}"
            current_data = {
                "run_id": run_id,
                "await_id": tool_call_id,
                "response": {
                    "role": "user",
                    "parts": [{"type": "text", "content": ACP_AUTO_REPLY}],
                },
            }
            combined_response_text = ""
            print(f"  [ACP] 自动回复 run.awaiting: {ACP_AUTO_REPLY}")
            continue
        else:
            break

    return parse_acp_sse_stream(combined_response_text)


def call_skill_execution_acp_stream(message: str, session_id: str | None = None, lang: str | None = None) -> dict:
    """
    通过 ACP 两步流式接口 (create + stream) 调用 Agent。

    步骤 1: POST /acp/runs  创建 run
    步骤 2: POST /acp/runs/{id}/stream  流式执行
    如遇到 run.awaiting 则通过 interact_stream 自动回复。
    """
    create_payload = {
        "messages": [
            {
                "role": "user",
                "parts": [{"type": "text", "content": message}],
            }
        ],
        "context": {"session_id": session_id} if session_id else {},
        "metadata": {},
    }
    if lang:
        create_payload["lang"] = lang

    create_resp = requests.post(
        f"{BASE_URL}/acp/runs",
        json=create_payload,
        headers=_headers(),
        timeout=30,
    )
    create_resp.raise_for_status()
    run_data = create_resp.json()
    run_id = run_data.get("run_id")
    if not run_id:
        return {"error": "No run_id from create", "run_id": None}

    stream_resp = requests.post(
        f"{BASE_URL}/acp/runs/{run_id}/stream",
        json={},
        headers=_headers(),
        timeout=(10, 180),
        stream=True,
    )
    stream_resp.raise_for_status()
    combined_response_text = _consume_sse_stream(stream_resp)

    partial = parse_acp_sse_stream(combined_response_text)
    partial["run_id"] = run_id

    awaiting = partial.get("acp_awaiting_events", [])
    if awaiting:
        last_await = awaiting[-1]
        tool_call_id = last_await.get("tool_call_id", "")
        interact_path = ACP_INTERACT_STREAM_PATH.replace("{run_id}", run_id)
        interact_data = {
            "run_id": run_id,
            "await_id": tool_call_id,
            "response": {
                "role": "user",
                "parts": [{"type": "text", "content": ACP_AUTO_REPLY}],
            },
        }
        interact_resp = requests.post(
            f"{BASE_URL}{interact_path}",
            json=interact_data,
            headers=_headers(),
            timeout=(10, 180),
            stream=True,
        )
        interact_resp.raise_for_status()
        print(f"  [ACP stream] 自动回复 run.awaiting: {ACP_AUTO_REPLY}")
        interact_text = _consume_sse_stream(interact_resp)
        partial = parse_acp_sse_stream(interact_text)
        partial["run_id"] = run_id

    return partial


def call_skill_execution_acp_execute(message: str, session_id: str | None = None, lang: str | None = None) -> dict:
    """
    通过 ACP 两步同步接口 (create + execute) 调用 Agent。

    步骤 1: POST /acp/runs  创建 run
    步骤 2: POST /acp/runs/{id}/execute  同步执行，返回 JSON
    """
    create_payload = {
        "messages": [
            {
                "role": "user",
                "parts": [{"type": "text", "content": message}],
            }
        ],
        "context": {"session_id": session_id} if session_id else {},
        "metadata": {},
    }
    if lang:
        create_payload["lang"] = lang

    create_resp = requests.post(
        f"{BASE_URL}/acp/runs",
        json=create_payload,
        headers=_headers(),
        timeout=30,
    )
    create_resp.raise_for_status()
    run_data = create_resp.json()
    run_id = run_data.get("run_id")
    if not run_id:
        return {"error": "No run_id from create", "run_id": None}

    execute_resp = requests.post(
        f"{BASE_URL}/acp/runs/{run_id}/execute",
        json={},
        headers=_headers(),
        timeout=(10, 180),
    )
    execute_resp.raise_for_status()
    result = execute_resp.json()

    messages = result.get("messages", [])
    final_output = ""
    for msg in messages:
        parts = msg.get("parts", [])
        for part in parts:
            if part.get("type") == "text":
                final_output += part.get("content", "")

    return {
        "run_id": run_id,
        "final_output": final_output,
        "tool_calls": [],
        "raw_events": [],
        "error": result.get("error"),
        "dag_perf": {},
        "llm_call_count": 0,
        "first_token_latency": 0.0,
        "script_stats": {},
        "script_call_details": [],
    }


def parse_sse_stream(response_text: str) -> dict:
    """
    解析 agent-stream.v2 协议的 SSE 响应文本。

    本项目 SSE 格式:
      event: agent.v2
      data: {"protocol":"agent-stream.v2","type":"<event_type>","run_id":"...","payload":{...},...}

    关键事件类型:
      - turn.started        : 开始一轮
      - turn.finished       : 结束一轮, payload.status ∈ {success, error, awaiting_input}
      - tool.started        : 工具调用开始, payload.tool_name / message
      - tool.finished       : 工具调用结束, payload.status / elapsed_ms
      - token.delta         : 流式输出文本片段, payload.text
      - dag_performance     : 性能统计, payload.worker_time / total_tokens
    """
    run_id = None
    turn_count = 0
    tool_calls: list[dict] = []
    final_text_parts: list[str] = []
    error = None
    dag_perf = {}
    raw_events: list[dict] = []

    llm_call_count = 0
    in_reasoning = False
    script_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_ms": 0})
    pending_script_map: dict[str, str] = {}
    script_call_details: list[dict] = []
    kg_tool_outputs: list[dict] = []
    pending_script_tc_id: dict[str, str] = {}
    kg_stream_buffers: dict[str, list[str]] = {}
    kg_tool_inputs: list[dict] = []

    turn_started_ts = None
    first_token_ts = None
    first_token_latency = 0.0
    token_segments: list[list[int]] = [[]]

    lines = response_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("event:"):
            i += 1
            if i < len(lines) and lines[i].startswith("data:"):
                data_str = lines[i][len("data:"):].strip()
                i += 1
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                raw_events.append(data)
                event_ts = data.get("ts")

                if not run_id:
                    run_id = data.get("run_id")

                event_type = data.get("type", "")
                payload = data.get("payload", {}) if isinstance(data.get("payload"), dict) else {}

                if event_type == "turn.started":
                    turn_count += 1
                    if turn_started_ts is None and event_ts:
                        turn_started_ts = event_ts

                elif event_type == "turn.finished":
                    status = payload.get("status", "")
                    if status == "error":
                        error = payload.get("error", "unknown error")

                elif event_type == "iteration.completed":
                    llm_call_count += 1
                    in_reasoning = False
                    token_segments.append([])

                elif event_type == "reasoning.delta":
                    if not in_reasoning:
                        in_reasoning = True
                        token_segments.append([])
                    if event_ts:
                        token_segments[-1].append(event_ts)

                elif event_type == "tool.started":
                    in_reasoning = False
                    token_segments.append([])
                    tool_call_id = payload.get("tool_call_id", "")
                    msg = payload.get("message", "")

                    _extract_kg_input_from_text(msg, kg_tool_inputs)

                    tool_input = payload.get("input", {})
                    if isinstance(tool_input, dict) and tool_input.get("operation_key"):
                        op_key = tool_input["operation_key"]
                        if any(kw in op_key for kw in _KG_OP_KEYWORDS):
                            kg_tool_inputs.append({
                                "tool_call_id": tool_call_id,
                                "kind": "input",
                                "raw_message": json.dumps(tool_input, ensure_ascii=False),
                                "parameters": tool_input,
                            })

                    # 原有的脚本匹配逻辑
                    script_match = re.search(r"执行脚本[:\s]+(\S+\.py)", msg)
                    if script_match:
                        pending_script_map[tool_call_id] = script_match.group(1)
                        if script_match.group(1) == "knowledge_graph_tool.py":
                            pending_script_tc_id[tool_call_id] = script_match.group(1)
                    else:
                        for op_key, script_file in _OPERATION_SCRIPT_MAP.items():
                            if op_key in msg:
                                pending_script_map[tool_call_id] = script_file
                                if script_file == "knowledge_graph_tool.py":
                                    pending_script_tc_id[tool_call_id] = script_file
                                break

                elif event_type == "tool.finished":
                    in_reasoning = False
                    token_segments.append([])
                    tool_name = payload.get("tool_name", "")
                    elapsed_ms = payload.get("elapsed_ms", 0)
                    tool_calls.append({
                        "name": tool_name,
                        "status": payload.get("status", ""),
                        "elapsed_ms": elapsed_ms,
                    })
                    tc_id = payload.get("tool_call_id", "")
                    if tc_id in pending_script_map:
                        script_name = pending_script_map.pop(tc_id)
                        script_stats[script_name]["count"] += 1
                        script_stats[script_name]["total_ms"] += elapsed_ms
                        script_call_details.append({
                            "name": script_name,
                            "status": payload.get("status", ""),
                            "elapsed_ms": elapsed_ms,
                        })
                    pending_script_tc_id.pop(tc_id, None)
                    if tc_id in kg_stream_buffers:
                        stream_text = "".join(kg_stream_buffers.pop(tc_id)).strip()
                        if stream_text:
                            kg_tool_outputs.append({
                                "tool_call_id": tc_id,
                                "raw_output": stream_text,
                            })

                elif event_type == "special_tool.finished":
                    in_reasoning = False
                    token_segments.append([])
                    tool_calls.append({
                        "name": payload.get("tool_name", ""),
                        "status": payload.get("status", ""),
                        "elapsed_ms": 0,
                    })

                elif event_type == "tool.progress":
                    kind = payload.get("kind", "")
                    tc_id = payload.get("tool_call_id", "")
                    if tc_id in pending_script_tc_id:
                        if kind == "input":
                            msg = payload.get("message", "")
                            if msg:
                                kg_tool_inputs.append({
                                    "tool_call_id": tc_id,
                                    "kind": "input",
                                    "raw_message": msg,
                                })
                        elif kind == "output":
                            msg = payload.get("message", "")
                            output_match = re.search(r"执行结果[:\s]*", msg)
                            output_text = msg[output_match.end():].strip() if output_match else msg.strip()
                            if output_text:
                                kg_tool_outputs.append({
                                    "tool_call_id": tc_id,
                                    "raw_output": output_text,
                                })
                        elif kind == "stream":
                            msg = payload.get("message", "")
                            if tc_id not in kg_stream_buffers:
                                kg_stream_buffers[tc_id] = []
                            kg_stream_buffers[tc_id].append(msg)

                elif event_type == "token.delta":
                    text = payload.get("text", "")
                    if text:
                        final_text_parts.append(text)
                    if first_token_ts is None and event_ts:
                        first_token_ts = event_ts
                    if event_ts:
                        token_segments[-1].append(event_ts)

                elif event_type == "dag_performance":
                    dag_perf = payload
            else:
                i += 1
        else:
            i += 1

    final_output = "".join(final_text_parts).strip() or None

    if turn_started_ts and first_token_ts:
        first_token_latency = (first_token_ts - turn_started_ts) / 1000.0

    avg_inter_token_latency = 0.0
    all_diffs: list[float] = []
    for seg in token_segments:
        if len(seg) >= 2:
            for j in range(1, len(seg)):
                all_diffs.append(seg[j] - seg[j - 1])
    if all_diffs:
        avg_inter_token_latency = sum(all_diffs) / len(all_diffs) / 1000.0

    dag_first_token_latency = dag_perf.get("first_token_latency", 0.0)
    dag_avg_inter_token_latency = dag_perf.get("avg_inter_token_latency", 0.0)
    effective_first_token_latency = dag_first_token_latency if dag_first_token_latency > 0 else first_token_latency
    effective_avg_inter_token_latency = dag_avg_inter_token_latency if dag_avg_inter_token_latency > 0 else avg_inter_token_latency

    return {
        "run_id": run_id,
        "final_output": final_output,
        "error": error,
        "turn_count": turn_count,
        "tool_calls": tool_calls,
        "dag_perf": dag_perf,
        "raw_events": raw_events,
        "llm_call_count": llm_call_count,
        "script_stats": dict(script_stats),
        "script_call_details": script_call_details,
        "first_token_latency": round(effective_first_token_latency, 3),
        "avg_inter_token_latency": round(effective_avg_inter_token_latency, 3),
        "kg_tool_outputs": kg_tool_outputs,
        "kg_tool_inputs": kg_tool_inputs,
    }


def _parse_otel_trace_log(run_id: str) -> dict:
    import glob

    runtime_root = os.getenv("PROJECT_ROOT", "").strip()
    workspace_id = os.getenv("WORKSPACE_ID", "").strip()
    log_candidates = []
    if runtime_root and workspace_id:
        log_candidates.append(os.path.join(runtime_root, "workspaces", workspace_id, "runtime", "observability"))
    elif runtime_root:
        log_candidates.extend(glob.glob(os.path.join(runtime_root, "workspaces", "*", "runtime", "observability")))
    log_candidates.extend([
        os.path.join(_TEST_DIR, "..", "backend", "logs"),
        os.path.join(_TEST_DIR, "..", "logs"),
    ])
    for log_dir in log_candidates:
        candidates = sorted(glob.glob(os.path.join(log_dir, "otel_trace-*.log")), reverse=True)
        for perf_file in candidates:
            if not os.path.exists(perf_file):
                continue
            llm_call_count = 0
            llm_total_time_ms = 0
            worker_time_ms = 0
            with open(perf_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("run_id") != run_id:
                        continue
                    event = record.get("event", "")
                    kind = record.get("kind", "")
                    metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
                    if kind == "llm_call" and event == "llm.finished":
                        llm_call_count += 1
                        llm_total_time_ms += (
                            metrics.get("wall_duration_ms")
                            or metrics.get("duration_ms")
                            or metrics.get("stream_duration_ms")
                            or 0
                        )
                    elif kind == "run_summary":
                        worker_time_ms = max(worker_time_ms, metrics.get("total_duration_ms") or 0)
                    elif kind == "run" and event in {"run.finished", "run.failed", "run.cancelled"}:
                        worker_time_ms = max(worker_time_ms, metrics.get("duration_ms") or 0)
            if llm_call_count > 0 or worker_time_ms > 0:
                return {
                    "llm_call_count": llm_call_count,
                    "llm_total_time": llm_total_time_ms / 1000.0,
                    "worker_time": worker_time_ms / 1000.0,
                }
    return {}


_SEVERITY_ORDER = {"none": 0, "info": 1, "warn": 2, "error": 3}


def _should_report_finding(severity: str) -> bool:
    min_level = CHINESE_DETECT_MIN_SEVERITY
    return _SEVERITY_ORDER.get(severity, 0) >= _SEVERITY_ORDER.get(min_level, 1)


def _detect_chinese_in_events(raw_events: list[dict], target_lang: str) -> dict:
    _THINKING_TYPES_ACP = {"thinking"}
    _OUTPUT_TYPES_ACP = {"answer", "result"}
    _THINKING_TYPES_AGENTIC = {"reasoning.delta", "thinking"}
    _OUTPUT_TYPES_AGENTIC = {"token.delta"}
    _TEXT_FIELDS_AGENTIC = {
        "reasoning.delta": ["payload.text"],
        "token.delta": ["payload.text"],
        "thinking": ["payload.text", "payload.message"],
    }
    thinking_findings: list[dict] = []
    output_findings: list[dict] = []
    seen_texts: set[str] = set()
    for evt in raw_events:
        if "event" in evt and "data" in evt:
            event_name = evt.get("event", "")
            data = evt.get("data", {})
            text = data.get("text", "")
            if not text:
                continue
            event_type = event_name
            if event_name in _THINKING_TYPES_ACP:
                pass
            elif event_name in _OUTPUT_TYPES_ACP:
                if event_name == "answer" and target_lang not in ("zh", "zh-cn"):
                    continue
                pass
            else:
                continue
            classification = _classify_chinese_finding(text, event_type, target_lang)
            if classification["severity"] == "none":
                continue
            if not _should_report_finding(classification["severity"]):
                continue
            dedup_key = (event_type, text.strip())
            if dedup_key in seen_texts:
                continue
            seen_texts.add(dedup_key)
            finding = {
                "event_type": event_type,
                "text": text.strip(),
                "category": classification["category"],
                "severity": classification["severity"],
                "description": classification["description"],
            }
            if event_name in _THINKING_TYPES_ACP:
                thinking_findings.append(finding)
            else:
                output_findings.append(finding)
        else:
            event_type = evt.get("type", "")
            paths = _TEXT_FIELDS_AGENTIC.get(event_type)
            if not paths:
                continue
            for path in paths:
                parts = path.split(".")
                obj = evt
                for p in parts:
                    if isinstance(obj, dict):
                        obj = obj.get(p)
                    else:
                        obj = None
                        break
                if not isinstance(obj, str) or not obj:
                    continue
                classification = _classify_chinese_finding(obj, event_type, target_lang)
                if classification["severity"] == "none":
                    continue
                if not _should_report_finding(classification["severity"]):
                    continue
                dedup_key = (event_type, obj.strip())
                if dedup_key in seen_texts:
                    continue
                seen_texts.add(dedup_key)
                finding = {
                    "event_type": event_type,
                    "text": obj.strip(),
                    "category": classification["category"],
                    "severity": classification["severity"],
                    "description": classification["description"],
                }
                if event_type in _THINKING_TYPES_AGENTIC:
                    thinking_findings.append(finding)
                else:
                    output_findings.append(finding)
    return {"thinking": thinking_findings, "output": output_findings}


def _detect_chinese_in_kg_outputs(kg_tool_outputs: list[dict], target_lang: str) -> list[dict]:
    if target_lang in ("zh", "zh-cn", "ja"):
        return []
    findings: list[dict] = []
    for output_entry in kg_tool_outputs:
        raw_output = output_entry.get("raw_output", "")
        if not raw_output:
            continue
        chinese_chars = sum(1 for c in raw_output if "\u4e00" <= c <= "\u9fff")
        if chinese_chars == 0:
            continue
        total_chars = len(raw_output.replace(" ", ""))
        if total_chars == 0:
            continue
        chinese_ratio = chinese_chars / total_chars
        parsed = None
        try:
            parsed = json.loads(raw_output)
        except ValueError:
            pass
        chinese_keys: list[str] = []
        chinese_values: list[str] = []
        if isinstance(parsed, dict):
            _extract_chinese_kv(parsed, chinese_keys, chinese_values)
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    _extract_chinese_kv(item, chinese_keys, chinese_values)
        findings.append({
            "chinese_char_count": chinese_chars,
            "chinese_ratio": round(chinese_ratio, 3),
            "chinese_keys": chinese_keys,
            "chinese_values": chinese_values,
            "raw_output_preview": raw_output[:300],
        })
    return findings


def _extract_chinese_kv(obj: dict, keys_out: list, values_out: list):
    for k, v in obj.items():
        if any("\u4e00" <= c <= "\u9fff" for c in k):
            keys_out.append(k)
        if isinstance(v, str) and any("\u4e00" <= c <= "\u9fff" for c in v):
            values_out.append(v[:80])
        elif isinstance(v, dict):
            _extract_chinese_kv(v, keys_out, values_out)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    _extract_chinese_kv(item, keys_out, values_out)


_KNOWN_LANG_KEYS = {
    "zh", "zh-cn", "zh-tw", "en", "ja", "ko", "ru", "de", "fr", "es", "pt",
    "ar", "it", "nl", "pl", "tr", "vi", "th", "hi", "id", "uk", "cs",
    "et", "lv", "lt", "sv", "da", "fi", "hu", "ro", "bg", "hr", "sk",
    "sl", "el", "he", "ms", "tl", "sw", "bn", "ta", "ur", "fa", "ca",
    "sr", "no", "af",
}


def _validate_kg_language_param(kg_tool_inputs: list[dict], expected_lang: str) -> dict:
    if not kg_tool_inputs:
        return {"status": "no_input", "description": "未捕获到输入参数"}

    exec_cmd = None
    input_params_raw = None
    for inp in kg_tool_inputs:
        msg = inp.get("raw_message", "")
        if msg.startswith("执行命令:"):
            exec_cmd = msg[len("执行命令:"):].strip()
        elif msg.startswith("输入参数:"):
            input_params_raw = msg[len("输入参数:"):].strip()

    search_text = ""
    source = None
    if exec_cmd:
        search_text = exec_cmd
        source = "exec_cmd"
    elif input_params_raw:
        search_text = input_params_raw
        source = "input_params"

    if not search_text:
        return {"status": "no_exec_cmd", "description": "未捕获到执行命令或输入参数"}

    parsed_lang = None

    json_lang_match = re.search(r'["\']language["\']\s*:\s*["\']([^"\']+)["\']', search_text)
    if json_lang_match:
        parsed_lang = json_lang_match.group(1)

    if parsed_lang is None:
        flag_match = re.search(r'(?:--language|-l)\s+(\S+)', search_text)
        if flag_match:
            candidate = flag_match.group(1)
            if candidate in _KNOWN_LANG_KEYS:
                parsed_lang = candidate
    if parsed_lang is None:
        tokens = search_text.split()
        if tokens:
            last = tokens[-1].strip(",;\"'")
            if last in _KNOWN_LANG_KEYS:
                parsed_lang = last

    if parsed_lang is None:
        if expected_lang in ("zh", "zh-cn"):
            return {
                "status": "ok",
                "parsed_lang": None,
                "expected_lang": expected_lang,
                "description": "中文场景无需language参数",
            }
        return {
            "status": "missing",
            "parsed_lang": None,
            "expected_lang": expected_lang,
            "description": f"执行命令/输入参数中未找到language参数 (期望: {expected_lang})",
            "exec_cmd": search_text,
        }

    _ZH_VARIANTS = {"zh", "zh-cn", "zh-tw"}
    if (parsed_lang == expected_lang
            or (parsed_lang in _ZH_VARIANTS and expected_lang in _ZH_VARIANTS)):
        return {
            "status": "ok",
            "parsed_lang": parsed_lang,
            "expected_lang": expected_lang,
            "description": f"language参数正确: {parsed_lang}",
        }

    _LANG_DISPLAY_VALUES = set(_LANG_DISPLAY_NAMES.values())

    if parsed_lang in _LANG_DISPLAY_VALUES:
        return {
            "status": "is_display_name",
            "parsed_lang": parsed_lang,
            "expected_lang": expected_lang,
            "description": f"language参数传入的是显示名称(value)而非key: '{parsed_lang}' (期望key: '{expected_lang}')",
            "exec_cmd": search_text,
        }

    return {
        "status": "mismatch",
        "parsed_lang": parsed_lang,
        "expected_lang": expected_lang,
        "description": f"language参数不匹配: 传入='{parsed_lang}', 期望='{expected_lang}'",
        "exec_cmd": search_text,
    }


def _llm_detect_language(text: str, target_lang: str) -> dict | None:
    try:
        from openai import OpenAI
        from lang import _get_llm_config_from_env
        llm_config = _get_llm_config_from_env()
        if not llm_config.get("api_key"):
            return None
        client = OpenAI(api_key=llm_config["api_key"], base_url=llm_config["base_url"])
        model_name = llm_config["model_name"]
    except Exception:
        return None

    lang_name = _LANG_DISPLAY_NAMES.get(target_lang, target_lang)
    sample = text[:800]
    prompt = (
        "You are a language detection expert. Determine the PRIMARY language of the following text.\n"
        f"The text is supposed to be in {lang_name} (language code: {target_lang}).\n\n"
        "Respond in this exact JSON format:\n"
        '{"detected_lang": "<language_code>", "confidence": 0.0-1.0, "reason": "brief explanation"}\n\n'
        "Use ISO 639-1 language codes (e.g., en, zh, ja, ko, fr, de, es, pt, ru, ar, etc.).\n"
        "If the text contains multiple languages, identify the dominant one.\n\n"
        "Text to analyze:\n" + sample
    )
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
            timeout=30,
        )
        raw = response.choices[0].message.content.strip()
        json_match = re.search(r'\{[^}]+\}', raw)
        if json_match:
            result = json.loads(json_match.group())
            return {
                "detected_lang": result.get("detected_lang", "unknown"),
                "confidence": float(result.get("confidence", 0.5)),
                "reason": result.get("reason", ""),
            }
    except Exception:
        pass
    return None


def _detect_kg_output_lang_match(kg_tool_outputs: list[dict], target_lang: str) -> dict:
    if not kg_tool_outputs:
        return {"status": "no_output", "description": "未捕获到KG工具输出"}

    if target_lang in ("zh", "zh-cn"):
        return {"status": "skip_zh", "description": "中文场景跳过输出语言匹配检测"}

    detected_langs: list[str] = []
    match_details: list[dict] = []
    for out in kg_tool_outputs:
        raw = out.get("raw_output", "")
        if not raw:
            continue

        heuristic_lang = _detect_output_lang(raw)
        is_latin_lang = target_lang in ("en", "fr", "de", "es", "pt", "it", "nl", "pl",
                                         "sv", "da", "fi", "hu", "ro", "bg", "hr",
                                         "sk", "sl", "cs", "uk", "no", "af", "ca")

        llm_result = _llm_detect_language(raw, target_lang)
        if llm_result:
            detected = llm_result["detected_lang"]
            is_match = (detected == target_lang)
            method = f"llm (conf={llm_result['confidence']:.1f})"
        else:
            detected = heuristic_lang
            if is_latin_lang:
                is_match = (detected == target_lang) or (heuristic_lang == "en")
                method = "heuristic_fallback"
            else:
                is_match = (detected == target_lang)
                method = "heuristic"

        match_details.append({
            "detected_lang": detected,
            "target_lang": target_lang,
            "is_match": is_match,
            "method": method,
            "preview": raw[:200],
        })
        if detected:
            detected_langs.append(detected)

    if not match_details:
        return {"status": "no_output", "description": "未捕获到KG工具输出"}

    all_match = all(d["is_match"] for d in match_details)
    if all_match:
        return {
            "status": "ok",
            "description": f"KG工具输出语言与目标语言匹配 (target={target_lang})",
            "details": match_details,
        }

    return {
        "status": "mismatch",
        "description": f"KG工具输出语言与目标语言不匹配 (target={target_lang}, detected={detected_langs})",
        "details": match_details,
    }


_LANG_CHAR_RANGES = {
    "zh": (0x4E00, 0x9FFF),
    "ja": (0x3040, 0x30FF),
    "ko": (0xAC00, 0xD7AF),
    "en": None,
    "ru": (0x0400, 0x04FF),
    "fr": None,
    "de": None,
    "es": None,
    "pt": None,
    "it": None,
    "ar": (0x0600, 0x06FF),
    "th": (0x0E00, 0x0E7F),
    "vi": None,
}


def _detect_output_lang(text: str) -> str | None:
    if not text or not text.strip():
        return None

    has_hiragana = any(0x3040 <= ord(c) <= 0x309F for c in text)
    has_katakana = any(0x30A0 <= ord(c) <= 0x30FF for c in text)
    if has_hiragana or has_katakana:
        return "ja"

    lang_char_counts: dict[str, int] = {}
    for lang_code, char_range in _LANG_CHAR_RANGES.items():
        if char_range is None:
            continue
        lo, hi = char_range
        count = sum(1 for c in text if lo <= ord(c) <= hi)
        if count > 0:
            lang_char_counts[lang_code] = count
    if not lang_char_counts:
        ascii_count = sum(1 for c in text if c.isascii() and c.isalpha())
        if ascii_count > 0:
            return "en"
        return None
    return max(lang_char_counts, key=lang_char_counts.get)


def call_skill_execution(message: str, session_id: str) -> dict:
    payload = {
        "request": message,
        "session_id": session_id,
        "context": {},
    }
    resp = requests.post(
        f"{BASE_URL}{STREAM_PATH}",
        json=payload,
        headers=_headers(),
        timeout=(10, 180),
        stream=True,
    )
    resp.raise_for_status()
    collected_lines: list[str] = []
    for line in resp.iter_lines(decode_unicode=True):
        if line is not None:
            collected_lines.append(line)
    resp.close()
    result = parse_sse_stream("\n".join(collected_lines))
    run_id = result.get("run_id")
    sse_llm_call_count = result.get("llm_call_count", 0)
    if run_id:
        perf_data = _parse_otel_trace_log(run_id)
        if perf_data:
            dag = result.get("dag_perf", {})
            perf_llm_count = perf_data.get("llm_call_count", 0)
            perf_llm_time = perf_data.get("llm_total_time", 0)
            perf_worker_time = perf_data.get("worker_time", 0)
            dag["llm_call_count"] = perf_llm_count if perf_llm_count >= sse_llm_call_count else sse_llm_call_count
            if perf_llm_time > 0:
                dag["llm_total_time"] = perf_llm_time
            elif not dag.get("llm_total_time"):
                dag["llm_total_time"] = perf_worker_time
            if not dag.get("worker_time"):
                dag["worker_time"] = perf_worker_time
            result["dag_perf"] = dag
    return result


def _extract_intermediate_summary(result: dict) -> dict:
    """从 result 中提取中间过程摘要，用于 Markdown 报告展示"""
    raw_events = result.get("raw_events", [])
    if not raw_events:
        return {"thinking": "", "tool_steps": [], "progress": []}

    is_acp = len(raw_events) > 0 and "event" in raw_events[0] and "data" in raw_events[0]
    thinking_parts: list[str] = []
    tool_steps: list[str] = []
    progress_parts: list[str] = []
    user_visible_parts: list[str] = []
    current_thinking: list[str] = []

    if is_acp:
        for evt in raw_events:
            en = evt.get("event", "")
            ed = evt.get("data", {})
            if en == "thinking":
                t = ed.get("text", "")
                if t:
                    thinking_parts.append(t)
                    user_visible_parts.append(f"\n🧠 {t}\n")
                    _is_tool_thinking = any(kw in t for kw in (
                        "加载本体", "执行脚本", "调用工具", "调用故障", "调用产品",
                        "ontology_context", "execute_script", "tool_call",
                    ))
                    if _is_tool_thinking:
                        current_thinking.append(t)
            elif en == "answer":
                t = ed.get("text", "")
                if t:
                    user_visible_parts.append(t)
            elif en == "tool_call":
                tn = ed.get("name", "")
                args = ed.get("args", {})
                tr = ed.get("result")
                args_str = json.dumps(args, ensure_ascii=False) if args else ""
                result_str = (tr if isinstance(tr, str) else json.dumps(tr, ensure_ascii=False)) if tr else ""
                step = tn
                if args_str:
                    step += f" | 入参: {args_str}"
                if result_str:
                    step += f" | 结果: {result_str}"
                tool_steps.append(step)
                current_thinking.clear()
            elif en == "result":
                t = ed.get("text", "")
                _tn = ed.get("tool_name", "")
                _tr = ed.get("result")
                _ts = ed.get("tool_status", "")
                _te = ed.get("elapsed_ms", 0)
                if _tn:
                    step = f"{_tn} | {_ts} | {_te}ms"
                    _ok = ed.get("operation_key", "")
                    if _ok:
                        step = f"[{_ok}] {step}"
                    if _tr:
                        tr_str = _tr if isinstance(_tr, str) else json.dumps(_tr, ensure_ascii=False)
                        step += f" | 输出: {tr_str}"
                        _ok_label = f"{_tn}({_ok})" if _ok else _tn
                        user_visible_parts.append(f"\n🔧 {_ok_label}\n{tr_str}\n")
                    tool_steps.append(step)
                    current_thinking.clear()
                elif t:
                    if current_thinking:
                        desc = current_thinking[-1]
                        tool_steps.append(f"{desc} → {t}")
                        current_thinking.clear()
                    else:
                        progress_parts.append(t)
            elif en == "progress":
                t = ed.get("text", "")
                if t:
                    progress_parts.append(t)
    else:
        for evt in raw_events:
            et = evt.get("type", "")
            pl = evt.get("payload", {})
            if et == "tool.started":
                tn = pl.get("tool_name", "")
                msg = pl.get("message", "")
                step = tn
                if msg:
                    step += f" — {msg[:200]}"
                tool_steps.append(step)
            elif et == "tool.finished":
                tn = pl.get("tool_name", "")
                st = pl.get("status", "")
                ms = pl.get("elapsed_ms", 0)
                tool_steps.append(f"{tn} | {st} | {ms}ms")
            elif et == "tool.progress":
                kind = pl.get("kind", "")
                msg = pl.get("message", "")
                if msg:
                    progress_parts.append(f"[{kind}] {msg[:200]}")

    return {
        "thinking": "".join(thinking_parts),
        "tool_steps": tool_steps,
        "progress": progress_parts,
        "user_visible": "".join(user_visible_parts),
    }


def format_duration(seconds: float) -> str:
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m{s}s"


def _save_run_log(
    sheet_name: str,
    run_index: int,
    message: str,
    session_id: str,
    elapsed: float,
    result: dict,
    start_time: datetime,
):
    log_dir = os.path.join(_current_run_dir or _RESULT_BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = start_time.strftime("%Y%m%d_%H%M%S")
    run_id = result.get("run_id", "unknown")
    filename = f"{sheet_name}_第{run_index}次_{ts}_{run_id}.txt"
    filepath = os.path.join(log_dir, filename)

    lines: list[str] = []
    lines.append(f"{'='*60}")
    lines.append(f"问题: {sheet_name}")
    lines.append(f"第 {run_index} 次执行")
    lines.append(f"开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Session ID: {session_id}")
    lines.append(f"Run ID: {run_id}")
    lines.append(f"耗时: {format_duration(elapsed)} ({elapsed:.2f}s)")
    lines.append(f"用户问题: {message}")
    lines.append(f"{'='*60}")

    if result.get("error"):
        lines.append(f"\n[错误] {result.get('error')}")
    else:
        dag = result.get("dag_perf", {})
        lines.append(f"\n--- 统计摘要 ---")
        lines.append(f"Turns: {result.get('turn_count', 0)}")
        lines.append(f"Tool Calls: {len(result.get('tool_calls', []))}")
        lines.append(f"Tokens: {dag.get('total_tokens', 0)}")
        lines.append(f"Worker: {dag.get('worker_time', 0):.2f}s")

        tool_calls = result.get("tool_calls", [])
        if tool_calls:
            lines.append(f"\n--- 工具调用详情 ({len(tool_calls)}) ---")
            for idx, tc in enumerate(tool_calls, 1):
                lines.append(f"  [{idx}] {tc.get('name', '?')} | 状态: {tc.get('status', '?')} | 耗时: {tc.get('elapsed_ms', 0)}ms")

        # --- 中间过程 (可读重建) ---
        _raw = result.get("raw_events", [])
        if _raw:
            _is_acp = len(_raw) > 0 and "event" in _raw[0] and "data" in _raw[0]
            _thinking_parts: list[str] = []
            _answer_parts: list[str] = []
            _tool_steps: list[str] = []
            _progress_parts: list[str] = []

            if _is_acp:
                # 按轮次重组事件，将 thinking→result 配对为工具步骤
                _current_thinking: list[str] = []
                for evt in _raw:
                    en = evt.get("event", "")
                    ed = evt.get("data", {})
                    if en == "thinking":
                        t = ed.get("text", "")
                        if t:
                            _thinking_parts.append(t)
                            # 判断是否为工具调用相关的思考（非 token 级推理）
                            _is_tool_thinking = any(kw in t for kw in (
                                "加载本体", "执行脚本", "调用工具", "调用故障", "调用产品",
                                "ontology_context", "execute_script", "tool_call",
                            ))
                            if _is_tool_thinking:
                                _current_thinking.append(t)
                    elif en == "answer":
                        t = ed.get("text", "")
                        if t:
                            _answer_parts.append(t)
                    elif en == "tool_call":
                        tn = ed.get("name", "")
                        args = ed.get("args", {})
                        tr = ed.get("result")
                        args_str = json.dumps(args, ensure_ascii=False) if args else ""
                        result_str = (tr if isinstance(tr, str) else json.dumps(tr, ensure_ascii=False)) if tr else ""
                        step = f"[工具调用] {tn}"
                        if args_str:
                            step += f"\n  入参: {args_str}"
                        if result_str:
                            step += f"\n  结果: {result_str}"
                        _tool_steps.append(step)
                        _current_thinking.clear()
                    elif en == "result":
                        t = ed.get("text", "")
                        _tn = ed.get("tool_name", "")
                        _tr = ed.get("result")
                        _ts = ed.get("tool_status", "")
                        _te = ed.get("elapsed_ms", 0)
                        if _tn:
                            step = f"[工具完成] {_tn} | 状态: {_ts} | 耗时: {_te}ms"
                            _ok = ed.get("operation_key", "")
                            if _ok:
                                step = step.replace(f"{_tn}", f"{_tn}({_ok})")
                            if _tr:
                                tr_str = _tr if isinstance(_tr, str) else json.dumps(_tr, ensure_ascii=False)
                                step += f"\n  输出: {tr_str}"
                            _tool_steps.append(step)
                            _current_thinking.clear()
                        elif t:
                            # 旧格式: 只有摘要文本
                            if _current_thinking:
                                desc = _current_thinking[-1]
                                _tool_steps.append(f"[工具结果] {desc} → {t}")
                                _current_thinking.clear()
                            else:
                                _progress_parts.append(f"[结果] {t}")
                    elif en == "progress":
                        t = ed.get("text", "")
                        if t:
                            _progress_parts.append(f"[进度] {t}")
            else:
                for evt in _raw:
                    et = evt.get("type", "")
                    pl = evt.get("payload", {})
                    if et == "reasoning.delta":
                        t = pl.get("text", "")
                        if t:
                            _thinking_parts.append(t)
                    elif et == "token.delta":
                        ch = pl.get("channel", "content")
                        t = pl.get("text", "")
                        if t:
                            if ch == "reasoning":
                                _thinking_parts.append(t)
                            else:
                                _answer_parts.append(t)
                    elif et == "tool.started":
                        tn = pl.get("tool_name", "")
                        msg = pl.get("message", "")
                        step = f"[工具开始] {tn}"
                        if msg:
                            step += f" — {msg[:200]}"
                        _tool_steps.append(step)
                    elif et == "tool.finished":
                        tn = pl.get("tool_name", "")
                        st = pl.get("status", "")
                        ms = pl.get("elapsed_ms", 0)
                        _tool_steps.append(f"[工具结束] {tn} | 状态: {st} | 耗时: {ms}ms")
                    elif et == "tool.progress":
                        kind = pl.get("kind", "")
                        msg = pl.get("message", "")
                        if msg:
                            _progress_parts.append(f"[工具进度/{kind}] {msg[:200]}")
                    elif et == "special_tool.started":
                        tn = pl.get("tool_name", "")
                        msg = pl.get("message", "")
                        step = f"[特殊工具] {tn}"
                        if msg:
                            step += f" — {msg[:200]}"
                        _tool_steps.append(step)

            lines.append("\n--- 中间过程 ---")
            if _thinking_parts:
                lines.append("\n[思考过程]")
                lines.append("".join(_thinking_parts))
            if _tool_steps:
                lines.append("\n[工具调用流程]")
                for s in _tool_steps:
                    lines.append(s)
            if _progress_parts:
                lines.append("\n[进度/结果通知]")
                for p in _progress_parts:
                    lines.append(p)
            if _answer_parts and not result.get("final_output"):
                lines.append("\n[回答片段 (未合并为最终输出)]")
                lines.append("".join(_answer_parts))
            if not _thinking_parts and not _tool_steps and not _progress_parts:
                lines.append("(无中间过程事件)")

        final_output = result.get("final_output", "")
        if final_output:
            lines.append(f"\n--- 最终输出 ---")
            lines.append(final_output)

        # --- 用户实际可见内容 (客户看到的内容：LLM回答 + 工具结果) ---
        if _raw:
            _user_visible_parts: list[str] = []
            for evt in _raw:
                if _is_acp:
                    en = evt.get("event", "")
                    ed = evt.get("data", {})
                    if en == "answer":
                        t = ed.get("text", "")
                        if t:
                            _user_visible_parts.append(t)
                    elif en == "result":
                        _tn = ed.get("tool_name", "")
                        _tr = ed.get("result")
                        _ok = ed.get("operation_key", "")
                        if _tn and _tr is not None:
                            label = f"{_tn}({_ok})" if _ok else _tn
                            tr_str = _tr if isinstance(_tr, str) else json.dumps(_tr, ensure_ascii=False)
                            _user_visible_parts.append(f"\n🔧 {label}\n{tr_str}\n")
                else:
                    et = evt.get("type", "")
                    pl = evt.get("payload", {})
                    ch = pl.get("channel", "content")
                    t = pl.get("text", "")
                    if et == "token.delta" and t:
                        if ch == "reasoning":
                            _user_visible_parts.append(t)
                        else:
                            _user_visible_parts.append(t)
                    elif et == "reasoning.delta" and t:
                        _user_visible_parts.append(t)
            if _user_visible_parts:
                lines.append(f"\n--- 用户实际可见内容 ---")
                lines.append("".join(_user_visible_parts))

    raw_events = result.get("raw_events", [])
    acp_outer_events = result.get("acp_outer_raw_events", [])
    if acp_outer_events:
        lines.append(f"\n--- ACP 外层事件 ({len(acp_outer_events)} 条) ---")
        for idx, evt in enumerate(acp_outer_events, 1):
            event_name = evt.get("event", "?")
            data = evt.get("data", {})
            lines.append(f"  [{idx}] {event_name} | data: {json.dumps(data, ensure_ascii=False)[:120]}")
    if raw_events:
        is_acp = len(raw_events) > 0 and "event" in raw_events[0] and "data" in raw_events[0]
        lines.append(f"\n--- 原始 SSE 事件流 ({len(raw_events)} 条) [{('ACP' if is_acp else 'Agentic')}] ---")
        for idx, evt in enumerate(raw_events, 1):
            if is_acp:
                event_name = evt.get("event", "?")
                data = evt.get("data", {})
                if event_name == "thinking":
                    text = data.get("text", "")
                    done = data.get("done", False)
                    lines.append(f"  [{idx}] {event_name} | done={done} | text: {text[:120]}")
                elif event_name == "result":
                    text = data.get("text", "")
                    done = data.get("done", False)
                    tn = data.get("tool_name", "")
                    tr = data.get("result")
                    ts = data.get("tool_status", "")
                    te = data.get("elapsed_ms", 0)
                    if tn:
                        tr_preview = (tr if isinstance(tr, str) else json.dumps(tr, ensure_ascii=False)) if tr else ""
                        lines.append(f"  [{idx}] {event_name} | done={done} | tool: {tn} | status: {ts} | elapsed: {te}ms | text: {text[:80]}")
                        if tr_preview:
                            lines.append(f"         | result: {tr_preview}")
                    else:
                        lines.append(f"  [{idx}] {event_name} | done={done} | text: {text[:120]}")
                elif event_name == "answer":
                    text = data.get("text", "")
                    done = data.get("done", False)
                    lines.append(f"  [{idx}] {event_name} | done={done} | text: {text[:120]}")
                elif event_name == "tool_call":
                    name = data.get("name", "")
                    args = data.get("args", {})
                    tool_result = data.get("result")
                    args_str = json.dumps(args, ensure_ascii=False)[:120] if args else ""
                    result_str = (tool_result if isinstance(tool_result, str) else json.dumps(tool_result, ensure_ascii=False))[:120] if tool_result else ""
                    lines.append(f"  [{idx}] {event_name} | name: {name} | args: {args_str}")
                    if result_str:
                        lines.append(f"         | result: {result_str}")
                elif event_name == "run.created":
                    lines.append(f"  [{idx}] {event_name} | run_id: {data.get('run_id', '')} | session_id: {data.get('session_id', '')}")
                elif event_name == "run.awaiting":
                    question = data.get("question", "")
                    tool_call_id = data.get("tool_call_id", "")
                    lines.append(f"  [{idx}] {event_name} | tool_call_id: {tool_call_id} | question: {question[:120]}")
                elif event_name in ("run.completed", "run.failed", "run.started"):
                    lines.append(f"  [{idx}] {event_name} | data: {json.dumps(data, ensure_ascii=False)[:120]}")
                elif event_name == "progress":
                    text = data.get("text", "")
                    lines.append(f"  [{idx}] {event_name} | text: {text[:120]}")
                else:
                    lines.append(f"  [{idx}] {event_name} | data: {json.dumps(data, ensure_ascii=False)[:120]}")
            else:
                event_type = evt.get("type", "?")
                payload = evt.get("payload", {})
                seq = evt.get("seq", "?")
                source = evt.get("source", {})
                node = source.get("node", "?")
                component = source.get("component", "?")

                if event_type == "token.delta":
                    text = payload.get("text", "")
                    lines.append(f"  [{idx}] seq={seq} type={event_type} | {node}/{component} | text: {text[:80]}")
                elif event_type == "tool.started":
                    lines.append(f"  [{idx}] seq={seq} type={event_type} | {node}/{component} | tool: {payload.get('tool_name', '?')} | msg: {payload.get('message', '')[:80]}")
                elif event_type == "tool.finished":
                    lines.append(f"  [{idx}] seq={seq} type={event_type} | {node}/{component} | tool: {payload.get('tool_name', '?')} | status: {payload.get('status', '?')} | elapsed: {payload.get('elapsed_ms', 0)}ms")
                elif event_type == "tool.progress":
                    kind = payload.get("kind", "")
                    msg = payload.get("message", "")
                    lines.append(f"  [{idx}] seq={seq} type={event_type} | {node}/{component} | kind: {kind} | msg: {msg[:80]}")
                elif event_type == "dag_performance":
                    lines.append(f"  [{idx}] seq={seq} type={event_type} | worker={payload.get('worker_time', 0):.2f}s tokens={payload.get('total_tokens', 0)}")
                elif event_type == "special_tool.started":
                    lines.append(f"  [{idx}] seq={seq} type={event_type} | tool: {payload.get('tool_name', '?')} | msg: {payload.get('message', '')[:80]}")
                elif event_type == "special_tool.ui":
                    ui_payload = payload.get("ui_payload", {})
                    lines.append(f"  [{idx}] seq={seq} type={event_type} | tool: {payload.get('tool_name', '?')} | question: {ui_payload.get('question', '')[:80]}")
                elif event_type == "special_tool.finished":
                    lines.append(f"  [{idx}] seq={seq} type={event_type} | tool: {payload.get('tool_name', '?')} | status: {payload.get('status', '?')}")
                elif event_type in ("turn.started", "turn.finished"):
                    lines.append(f"  [{idx}] seq={seq} type={event_type} | {node}/{component} | status: {payload.get('status', '')}")
                else:
                    lines.append(f"  [{idx}] seq={seq} type={event_type} | {node}/{component}")

    lines.append(f"\n{'='*60}")
    lines.append(f"记录结束")
    lines.append(f"{'='*60}")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return filepath


_CHATS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".onto-runtime", "workspaces", WORKSPACE_ID, "runtime", "users", AUTH_USERNAME or "benchmark-user", "chats",
)

def _read_snapshot_failures(session_id: str) -> list[dict]:
    """读取 session 的 snapshot.json，提取所有失败事件详情"""
    snap_path = os.path.join(_CHATS_DIR, session_id, "snapshot.json")
    if not os.path.exists(snap_path):
        return []
    try:
        with open(snap_path, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:
        return []
    failures = []
    events = snap.get("events", [])
    for ev in events:
        if ev.get("event") != "agent.v2":
            continue
        data = ev.get("data", {})
        ev_type = data.get("type", "")
        payload = data.get("payload", {})
        if ev_type == "tool.finished":
            status = payload.get("status", "")
            if status in ("failure", "error"):
                failures.append({
                    "type": "tool.finished",
                    "tool_name": payload.get("tool_name", ""),
                    "error": str(payload.get("error") or payload.get("result", ""))[:500],
                })
        elif ev_type == "turn.finished":
            status = payload.get("status", "")
            if status in ("failure", "error"):
                failures.append({
                    "type": "turn.finished",
                    "error": str(payload.get("error") or payload.get("final_output", ""))[:500],
                })
    return failures


def _apply_style(cell, style_cell):
    cell.font = copy(style_cell.font)
    cell.alignment = copy(style_cell.alignment)
    cell.border = copy(style_cell.border)
    cell.fill = copy(style_cell.fill)
    cell.number_format = style_cell.number_format


_DEFAULT_STYLE_CELL = None


def _get_default_style_cell():
    global _DEFAULT_STYLE_CELL
    if _DEFAULT_STYLE_CELL is None:
        _DEFAULT_STYLE_CELL = type("StyleCell", (), {})()
        _DEFAULT_STYLE_CELL.font = Font(name="微软雅黑", size=11)
        _DEFAULT_STYLE_CELL.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        _DEFAULT_STYLE_CELL.border = Border(
            left=Side(style="thin"), right=Side(style="thin"),
            top=Side(style="thin"), bottom=Side(style="thin"),
        )
        _DEFAULT_STYLE_CELL.fill = PatternFill()
        _DEFAULT_STYLE_CELL.number_format = "General"
    return _DEFAULT_STYLE_CELL


def _get_style_template(ws, header_row=1):
    for col in range(1, ws.max_column + 1):
        cell = ws.cell(row=header_row, column=col)
        if cell.font and cell.font.name:
            return cell
    return None


def _write_cell(ws, row, col, value, style_cell=None):
    cell = ws.cell(row=row, column=col, value=value)
    if style_cell is not None:
        _apply_style(cell, style_cell)
    return cell


def _ensure_xlsx(wb: openpyxl.Workbook):
    default_style = _get_default_style_cell()
    headers = [
        "执行轮次", "执行开始时间", "执行耗时（XmXs）", "执行耗时（s）",
        "tokens消耗", "LLM执行次数", "LLM总耗时（s）", "LLM平均耗时（s）", "脚本调用统计",
        "首token时延（s）", "增量token平均时延（s）", "测试语言",
    ]
    for q in QUESTIONS:
        sheet_name = q["sheet"]
        if sheet_name not in wb.sheetnames:
            ws = wb.create_sheet(sheet_name)
            for col_idx, header in enumerate(headers, 1):
                _write_cell(ws, 1, col_idx, header, default_style)
        else:
            ws = wb[sheet_name]
            for col_idx, header in enumerate(headers, 1):
                if ws.cell(row=1, column=col_idx).value is None:
                    _write_cell(ws, 1, col_idx, header, default_style)
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]


def _find_backend_log() -> str | None:
    log_candidates = [
        os.path.join(_TEST_DIR, "..", "backend", "logs"),
        os.path.join(_TEST_DIR, "..", "logs"),
    ]
    today = datetime.now().strftime("%Y-%m-%d")
    for log_dir in log_candidates:
        if not os.path.isdir(log_dir):
            continue
        dated = os.path.join(log_dir, f"agent-{today}.log")
        if os.path.exists(dated):
            return dated
        import glob as _glob
        candidates = sorted(_glob.glob(os.path.join(log_dir, "agent-*.log")), reverse=True)
        if candidates:
            return candidates[0]
    return None


def _get_log_line_count() -> int:
    log_path = _find_backend_log()
    if not log_path:
        return 0
    count = 0
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for _ in f:
            count += 1
    return count


def _analyze_think_process(run_ids: list[str], log_baseline: int):
    log_path = _find_backend_log()
    if not log_path or not run_ids:
        print(f"  ⚠ 无法分析: 日志文件不存在或无成功执行的 run_id")
        return {}

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = []
        for i, line in enumerate(f, 1):
            if i > log_baseline:
                lines.append((i, line.rstrip()))

    run_data = {}
    for rid in run_ids:
        run_data[rid] = {
            "final_output": None,
            "final_conclusion": None,
            "has_think_tag": False,
            "think_content": "",
            "clean": False,
        }

    for idx, (_, line) in enumerate(lines):
        for rid in run_ids:
            if f"run={rid}" not in line:
                continue
            if "executor:execute:1130" in line and "[最终输出]" in line:
                collected = []
                for j in range(idx, min(idx + 80, len(lines))):
                    _, text = lines[j]
                    if j > idx and ("executor:execute:" in text or "app.api.consume" in text or "app.agent.nodes" in text):
                        break
                    collected.append(text)
                full_text = "\n".join(collected)
                out_idx = full_text.find("[最终输出]")
                if out_idx >= 0:
                    run_data[rid]["final_output"] = full_text[out_idx:]
                think_match = re.search(r'<think_process>([\s\S]*?)(?:</think_process>|$)', full_text)
                if think_match:
                    run_data[rid]["has_think_tag"] = True
                    run_data[rid]["think_content"] = think_match.group(1).strip()
            elif "executor:execute:1349" in line and "最终结论：" in line:
                ci = line.index("最终结论：")
                run_data[rid]["final_conclusion"] = line[ci:]

    for rid in run_ids:
        d = run_data[rid]
        conclusion = d["final_conclusion"] or ""
        d["clean"] = conclusion.startswith("最终结论：##") or conclusion.startswith("最终结论：\n##")

    tag_count = sum(1 for d in run_data.values() if d["has_think_tag"])
    clean_count = sum(1 for d in run_data.values() if d["clean"])
    total = len(run_ids)

    print(f"\n{'='*70}")
    print(f"📊 <think_process> 标签分析")
    print(f"{'='*70}")
    print(f"  成功执行: {total} 次")
    print(f"  <think_process> 标签使用: {tag_count}/{total} ({tag_count/total*100:.0f}%)")
    print(f"  最终结论干净度: {clean_count}/{total} ({clean_count/total*100:.0f}%)")
    print()

    for rid in run_ids:
        d = run_data[rid]
        status_icon = "✅" if d["clean"] else "⚠️"
        tag_icon = "🏷️" if d["has_think_tag"] else "  "
        conclusion_preview = (d["final_conclusion"] or "无")[5:80]
        print(f"  {status_icon} {tag_icon} {rid}")
        print(f"       最终结论: {conclusion_preview}...")
        if d["has_think_tag"]:
            preview = d["think_content"][:150].replace("\n", " ")
            print(f"       think内容: {preview}...")
        print()

    tag_rate = tag_count / total * 100 if total > 0 else 0
    clean_rate = clean_count / total * 100 if total > 0 else 0
    both_rate = sum(1 for d in run_data.values() if d["has_think_tag"] and d["clean"]) / total * 100 if total > 0 else 0
    print(f"  📋 汇总:")
    print(f"    标签使用率: {tag_rate:.0f}% ({tag_count}/{total})")
    print(f"    结论干净率: {clean_rate:.0f}% ({clean_count}/{total})")
    print(f"    标签+干净双达标: {both_rate:.0f}%")
    print(f"{'='*70}")

    return {
        "run_data": run_data,
        "tag_count": tag_count,
        "clean_count": clean_count,
        "total": total,
        "tag_rate": tag_rate,
        "clean_rate": clean_rate,
        "both_rate": both_rate,
    }


def _get_system_info() -> dict:
    info = {}
    try:
        from lang import _get_llm_config_from_env
        llm_config = _get_llm_config_from_env()
        info["llm_provider"] = get_env("LLM_PROVIDER", "custom")
        info["llm_provider_id"] = get_env("LLM_PROVIDER", "custom")
        info["llm_base_url"] = llm_config.get("base_url", "N/A")
        info["llm_model"] = llm_config.get("model_name", "N/A")
    except Exception:
        pass

    info["base_url"] = BASE_URL
    info["test_language"] = TEST_LANGUAGE
    info["active_group"] = ACTIVE_TEST_GROUP
    info["eval_sample_size"] = EVAL_SAMPLE_SIZE
    info["runs_per_question"] = RUNS_PER_QUESTION
    info["workspace_id"] = WORKSPACE_ID
    info["execution_mode"] = RUNS_EXECUTION_MODE
    info["api_mode"] = API_MODE
    if ACP_SUPPRESS_SCRIPT_PATTERNS:
        info["acp_suppress_script_patterns"] = ACP_SUPPRESS_SCRIPT_PATTERNS
    if RUNS_EXECUTION_MODE == "parallel":
        info["execution_mode_display"] = f"并行 (最大并发 {RUNS_MAX_CONCURRENCY})"
    else:
        info["execution_mode_display"] = "串行"
    return info


def _generate_time_breakdown_chart(
    avg_seconds: float,
    total_runs: int,
    script_stats: dict,
    lang_code: str,
    lang_display: str,
):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print(f"  ⚠ matplotlib 未安装，跳过图表生成")
        return

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    tool_names = ["knowledge_graph_tool.py", "mindsdb_tool.py", "work_order_sync.py"]
    tool_labels = ["KG 工具", "MindsDB", "WOS"]
    tool_colors = ["#DD8452", "#55A868", "#C44E52"]

    tool_per_run = []
    for sn in tool_names:
        sd = script_stats.get(sn, {})
        count = sd.get("count", 0)
        avg_ms = sd.get("avg", 0)
        tool_per_run.append(count * (avg_ms / 1000) / total_runs if total_runs else 0)

    tool_total = sum(tool_per_run)
    llm_time = avg_seconds - tool_total

    run_dir = _current_run_dir or os.path.join(_RESULT_BASE_DIR, "misc")

    fig1, ax1 = plt.subplots(figsize=(8, 5))
    labels = ["LLM 时间"] + tool_labels
    vals = [llm_time] + tool_per_run
    colors = ["#4C72B0"] + tool_colors

    bars = ax1.barh(labels, vals, color=colors)
    for bar, v in zip(bars, vals):
        pct = v / avg_seconds * 100 if avg_seconds else 0
        ax1.text(bar.get_width() + 0.2, bar.get_y() + bar.get_height() / 2,
                 f"{v:.2f}s ({pct:.0f}%)", ha="left", va="center", fontsize=10)

    ax1.set_xlabel("时间 (秒)")
    ax1.set_title(f"{lang_display}({lang_code}) 每次执行时间分解 (总耗时 {avg_seconds:.2f}s)")
    ax1.set_xlim(0, avg_seconds * 1.3)
    plt.tight_layout()
    p1 = os.path.join(run_dir, "time_breakdown.png")
    fig1.savefig(p1, dpi=150, bbox_inches="tight")
    plt.close(fig1)
    print(f"  📊 时间分解图已保存: {p1}")

    json_path = os.path.join(run_dir, "script_call_details.json")
    if not os.path.exists(json_path):
        return
    with open(json_path, "r", encoding="utf-8") as f:
        raw_details = json.load(f)

    has_data = False
    for sn in tool_names:
        items = raw_details.get(sn, [])
        success_items = [d for d in items if d.get("status") != "error"]
        if success_items:
            has_data = True
            break
    if not has_data:
        return

    fig2, axes = plt.subplots(1, 3, figsize=(16, 5))
    for idx, (sn, label, color) in enumerate(zip(tool_names, tool_labels, tool_colors)):
        ax = axes[idx]
        items = raw_details.get(sn, [])
        success_items = [d for d in items if d.get("status") != "error"]
        elapsed_s = [d["elapsed_ms"] / 1000 for d in success_items if d.get("elapsed_ms")]

        if not elapsed_s:
            ax.text(0.5, 0.5, "无数据", ha="center", va="center", transform=ax.transAxes, fontsize=14, color="gray")
            ax.set_title(f"{label}\n(无调用)", fontsize=11)
            ax.set_xticks([])
            ax.set_yticks([])
            continue

        n_bins = min(20, max(5, len(elapsed_s) // 3))
        ax.hist(elapsed_s, bins=n_bins, color=color, edgecolor="white", alpha=0.85)

        avg_v = sum(elapsed_s) / len(elapsed_s)
        max_v = max(elapsed_s)
        ax.axvline(avg_v, color="red", linestyle="--", linewidth=1.5, label=f"平均 {avg_v:.1f}s")

        ax.set_xlabel("耗时 (秒)", fontsize=10)
        ax.set_ylabel("调用次数", fontsize=10)
        ax.set_title(f"{label}\n{len(elapsed_s)} 次调用 | 平均 {avg_v:.1f}s | 最慢 {max_v:.1f}s", fontsize=10)
        ax.legend(fontsize=9)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    fig2.suptitle(f"{lang_display}({lang_code}) 工具调用耗时分布", fontsize=13, fontweight="bold", y=1.02)
    plt.tight_layout()
    p2 = os.path.join(run_dir, "tool_latency_dist.png")
    fig2.savefig(p2, dpi=150, bbox_inches="tight")
    plt.close(fig2)
    print(f"  📊 工具耗时分布图已保存: {p2}")


def _generate_md_report(
    system_info: dict,
    stats: dict | None,
    global_stats: dict,
    think_analysis: dict | None,
    extra: dict | None = None,
):
    run_dir = _current_run_dir or os.path.join(_RESULT_BASE_DIR, "misc")
    os.makedirs(run_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = "benchmark_%s.md" % ts
    filepath = os.path.join(run_dir, filename)

    extra = extra or {}

    lines = []
    lines.append("# Benchmark 测试报告")
    lines.append("")
    lines.append("> 自动生成于 %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 环境配置")
    lines.append("")
    lines.append("| 配置项 | 值 |")
    lines.append("|--------|-----|")
    lines.append("| LLM 厂商 | %s |" % system_info.get("llm_provider", "N/A"))
    lines.append("| LLM 模型 | %s |" % system_info.get("llm_model", "N/A"))
    lines.append("| LLM Base URL | %s |" % system_info.get("llm_base_url", "N/A"))
    lines.append("| 后端地址 | %s |" % system_info.get("base_url", "N/A"))
    lines.append("| 工作空间 | %s |" % system_info.get("workspace_id", "N/A"))
    lines.append("")
    lines.append("## 测试参数")
    lines.append("")
    lines.append("| 参数 | 值 |")
    lines.append("|------|-----|")
    lines.append("| 测试语言 | %s |" % system_info.get("test_language", "N/A"))
    lines.append("| 测试组 | %s |" % system_info.get("active_group", "N/A"))
    lines.append("| 执行模式 | %s |" % system_info.get("execution_mode_display", "N/A"))
    lines.append("| API 模式 | %s |" % system_info.get("api_mode", "N/A"))
    lines.append("| 评估抽样数 | %s |" % system_info.get("eval_sample_size", "N/A"))
    lines.append("| 每问题执行次数 | %s |" % system_info.get("runs_per_question", "N/A"))
    if system_info.get("acp_suppress_script_patterns"):
        lines.append("| ACP 输出过滤 | `%s` |" % system_info["acp_suppress_script_patterns"])
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 全局统计")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|------|-----|")
    lines.append("| 总问题数 | %d |" % global_stats.get("total_questions", 0))
    lines.append("| 总执行次数 | %d |" % global_stats.get("total_runs", 0))
    lines.append("| 成功率 | %d/%d (%.1f%%) |" % (
        global_stats.get("success_count", 0),
        global_stats.get("total_runs", 1),
        global_stats.get("success_rate", 0),
    ))
    lines.append("| 平均耗时 | %s |" % global_stats.get("avg_duration", "N/A"))
    lines.append("| 平均 Tokens | %.0f |" % global_stats.get("avg_tokens", 0))
    lines.append("| 平均首Token时延 | %.3fs |" % global_stats.get("avg_first_token_latency", 0))
    lines.append("| 平均增量Token时延 | %.3fs |" % global_stats.get("avg_inter_token_latency", 0))
    lines.append("| 平均 LLM 调用次数 | %.1f |" % global_stats.get("avg_llm_call_count", 0))
    lines.append("| 平均 LLM 总耗时 | %.1fs |" % global_stats.get("avg_llm_time", 0))
    lines.append("| 平均每次 LLM 调用耗时 | %.2fs |" % global_stats.get("avg_llm_avg_time", 0))
    lines.append("")

    # 逐次执行详情
    run_details = extra.get("run_details_log", [])
    if run_details:
        lines.append("---")
        lines.append("")
        lines.append("## 逐次执行详情")
        lines.append("")
        for entry in run_details:
            sheet = entry.get("sheet", "?")
            run_idx = entry.get("run", 1)
            lang = entry.get("lang", "?")
            elapsed = entry.get("elapsed", 0)
            success = entry.get("success", False)
            run_id = entry.get("run_id", "")
            final_output = entry.get("final_output", "")
            user_visible = entry.get("user_visible", "")
            thinking = entry.get("thinking", "")
            tool_steps = entry.get("tool_steps", [])
            progress = entry.get("progress", [])

            status_icon = "✅" if success else "❌"
            lines.append("### %s — 第%d次 (%s) %s" % (sheet, run_idx, lang, status_icon))
            lines.append("")
            lines.append("- **耗时**: %.1fs" % elapsed)
            if run_id:
                lines.append("- **Run ID**: %s" % run_id)

            if tool_steps:
                lines.append("")
                lines.append("**工具调用流程**:")
                lines.append("")
                for step in tool_steps:
                    lines.append("1. %s" % step)

            if user_visible:
                lines.append("")
                lines.append("**用户实际可见内容 (thinking + answer 按顺序拼接)**:")
                lines.append("")
                lines.append("```")
                lines.append(user_visible)
                lines.append("```")

            if thinking:
                lines.append("")
                lines.append("<details>")
                lines.append("<summary>思考过程</summary>")
                lines.append("")
                lines.append(thinking)
                lines.append("")
                lines.append("</details>")

            if progress:
                lines.append("")
                lines.append("<details>")
                lines.append("<summary>进度/结果通知 (%d 条)</summary>" % len(progress))
                lines.append("")
                for p in progress:
                    lines.append("- %s" % p)
                lines.append("")
                lines.append("</details>")

            if final_output:
                lines.append("")
                lines.append("<details>")
                lines.append("<summary>最终输出</summary>")
                lines.append("")
                lines.append(final_output)
                lines.append("")
                lines.append("</details>")

            lines.append("")

    if stats:
        lines.append("### 脚本执行统计")
        lines.append("")
        lines.append("| 脚本 | 总执行 | 平均耗时 | 最慢 | 失败 |")
        lines.append("|------|--------|----------|------|------|")
        for sn, sd in stats.get("scripts", {}).items():
            lines.append("| %s | %d | %.1fs | %.1fs | %d |" % (
                sn, sd.get("count", 0), sd.get("avg", 0) / 1000, sd.get("max", 0) / 1000, sd.get("fail", 0),
            ))
        lines.append("")

    failure_details = extra.get("failure_details", [])
    if failure_details:
        lines.append("### 失败详情")
        lines.append("")
        lines.append("| 问题 | 执行次 | 语言 | 类型 | 工具/步骤 | 错误信息 |")
        lines.append("|------|--------|------|------|----------|----------|")
        for entry in failure_details:
            sheet = entry.get("sheet", "?")
            run_idx = entry.get("run", 0)
            lang = entry.get("lang", "?")
            for fd in entry.get("failures", []):
                fd_type = fd.get("type", "?")
                tool_name = fd.get("tool_name", "")
                error = fd.get("error", "")[:120]
                lines.append("| %s | %d | %s | %s | %s | %s |" % (
                    sheet, run_idx, lang, fd_type, tool_name, error))
        lines.append("")
    else:
        lines.append("### 失败详情")
        lines.append("")
        lines.append("> ✅ 所有执行均未捕获到失败事件")
        lines.append("")

    wos_track = extra.get("flywheel_wos_track")
    if wos_track:
        lines.append("### 飞轮问题 work_order_sync.py 覆盖检测")
        lines.append("")
        flywheel_sheets = ["问题%d" % i for i in range(1, 5)]
        has_data = any(s in wos_track for s in flywheel_sheets)
        if has_data:
            lines.append("| 问题 | 执行次数 | WOS调用 | 覆盖率 | 状态 |")
            lines.append("|------|----------|--------|--------|------|")
            for sheet in flywheel_sheets:
                track = wos_track.get(sheet)
                if not track:
                    lines.append("| %s | - | - | - | 🔴 未执行 |" % sheet)
                elif track["wos_calls"] == 0:
                    lines.append("| %s | %d | 0 | 0%% | 🔴 未调用 |" % (sheet, track["runs"]))
                else:
                    lines.append("| %s | %d | %d | 100%% | ✅ |" % (sheet, track["runs"], track["wos_calls"]))
            lines.append("")
        else:
            lines.append("> 无飞轮问题执行数据")
            lines.append("")

    chinese_log = extra.get("chinese_intrusion_log", [])
    non_zh_total = extra.get("non_zh_total", 0)
    lang_follow_enabled = extra.get("lang_follow_enabled")
    if lang_follow_enabled is False:
        lines.append("### 中文混入检测")
        lines.append("")
        lines.append("> ⏭️ 语言跟随未开启，跳过中文混入检测")
        lines.append(">")
        lines.append("> 💡 开启前端「语言跟随」后，Agent 输出将跟随用户语言，此时再进行中文混入检测")
        lines.append("")
    elif non_zh_total > 0:
        lines.append("### 中文混入检测")
        lines.append("")
        non_zh_clean = non_zh_total - len(chinese_log)

        method_counts: dict[str, int] = defaultdict(int)
        for entry in chinese_log:
            for finding in entry.get("thinking_findings", []):
                desc = finding.get("description", "")
                method = "LLM" if "LLM" in desc else ("启发式" if "假名" in desc or "谚文" in desc else "规则")
                method_counts[method] += 1
            for finding in entry.get("output_findings", []):
                desc = finding.get("description", "")
                method = "LLM" if "LLM" in desc else ("启发式" if "假名" in desc or "谚文" in desc else "规则")
                method_counts[method] += 1
        method_summary = " | ".join("**%s** %d次" % (m, c) for m, c in sorted(method_counts.items()))

        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append("| 非中文场景总执行 | %d |" % non_zh_total)
        lines.append("| 无中文混入 | %d/%d (%.1f%%) |" % (non_zh_clean, non_zh_total, non_zh_clean / non_zh_total * 100 if non_zh_total else 0))
        lines.append("")
        lines.append("> 本次实际使用: %s" % method_summary)
        lines.append("")
        if chinese_log:
            lines.append("#### 中文混入详情")
            lines.append("")
            lines.append("| 问题 | 执行次 | 语言 | 通道 | Round | 级别 | 类别 | 检测方式 | 中文片段 |")
            lines.append("|------|--------|------|------|-------|------|------|----------|----------|")
            for entry in chinese_log:
                sheet = entry.get("sheet", "?")
                run_idx = entry.get("run", 0)
                lang = entry.get("lang", "?")
                for finding in entry.get("thinking_findings", []):
                    round_match = re.search(r'Round (\d+)', finding.get("text", ""))
                    round_label = "R%s" % round_match.group(1) if round_match else "?"
                    desc = finding.get("description", "")
                    method = "LLM" if "LLM" in desc else ("启发式" if "假名" in desc or "谚文" in desc else "规则")
                    snippets = re.findall(r'[\u4e00-\u9fff][\u4e00-\u9fff：，、（）\s\w]*[\u4e00-\u9fff]', finding.get("text", ""))
                    snippet_str = "; ".join(snippets[:2]) if snippets else ""
                    lines.append("| %s | %d | %s | 思考 | %s | %s | %s | %s | %s |" % (
                        sheet, run_idx, lang, round_label, finding.get("severity", ""), finding.get("category", ""), method, snippet_str))
                for finding in entry.get("output_findings", []):
                    desc = finding.get("description", "")
                    method = "LLM" if "LLM" in desc else ("启发式" if "假名" in desc or "谚文" in desc else "规则")
                    snippets = re.findall(r'[\u4e00-\u9fff][\u4e00-\u9fff：，、（）\s\w]*[\u4e00-\u9fff]', finding.get("text", ""))
                    snippet_str = "; ".join(snippets[:2]) if snippets else ""
                    lines.append("| %s | %d | %s | 报告 | - | %s | %s | %s | %s |" % (
                        sheet, run_idx, lang, finding.get("severity", ""), finding.get("category", ""), method, snippet_str))
            lines.append("")
        else:
            lines.append("> ✅ 所有非中文场景均未检测到中文混入")
            lines.append("")
    else:
        lines.append("### 中文混入检测")
        lines.append("")
        lines.append("> 无非中文场景执行, 跳过中文混入统计")
        lines.append("")

    kg_log = extra.get("kg_chinese_log", [])
    lines.append("### knowledge_graph_tool 返回中文检测")
    lines.append("")
    lines.append("> 本次实际使用: Unicode CJK 字符范围扫描 (U+4E00-U+9FFF), 统计 JSON 键值中的中文字符占比")
    lines.append("")
    if kg_log:
        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append("| 检测到含中文的执行次数 | %d |" % len(kg_log))
        for entry in kg_log:
            for kf in entry.get("findings", []):
                lines.append("| %s 第%d次 (%s) | 中文字符 %d, 占比 %.1f%% |" % (
                    entry["sheet"], entry["run"], entry["lang"],
                    kf.get("chinese_char_count", 0), kf.get("chinese_ratio", 0) * 100))
        lines.append("")
    else:
        lines.append("> ✅ knowledge_graph_tool 返回数据中未检测到中文 (或未调用该工具)")
        lines.append("")

    kg_lang_param_entries = extra.get("kg_lang_param_log", [])
    lines.append("### knowledge_graph_tool language参数检测")
    lines.append("")
    lines.append("> 本次实际使用: 解析输入参数 JSON 中的 `language` 字段, 与期望语言对比")
    lines.append("")
    if kg_lang_param_entries:
        ok_count = sum(1 for e in kg_lang_param_entries if e["check"]["status"] == "ok")
        issue_entries = [e for e in kg_lang_param_entries if e["check"]["status"] != "ok"]
        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append("| 总调用检测数 | %d |" % len(kg_lang_param_entries))
        lines.append("| ✅ 参数正确 | %d |" % ok_count)
        lines.append("| 🔴 参数异常 | %d |" % len(issue_entries))
        lines.append("")
        if issue_entries:
            lines.append("#### 参数异常详情")
            lines.append("")
            lines.append("| 问题 | 执行次 | 语言 | 状态 | 描述 |")
            lines.append("|------|--------|------|------|------|")
            for entry in issue_entries:
                check = entry["check"]
                status_label = {
                    "missing": "缺少language参数",
                    "mismatch": "参数不匹配",
                    "is_display_name": "传入显示名称",
                    "no_exec_cmd": "未捕获执行命令",
                }.get(check["status"], check["status"])
                lines.append("| %s | %d | %s | %s | %s |" % (
                    entry["sheet"], entry["run"], entry["lang"], status_label, check["description"]))
            lines.append("")
    else:
        lines.append("> (未捕获到 KG 工具输入参数，跳过检测)")
        lines.append("")

    kg_output_lang_entries = extra.get("kg_output_lang_log", [])
    lines.append("### knowledge_graph_tool 输出语言匹配检测")
    lines.append("")
    lines.append("> 本次实际使用: 检测 KG 工具输出文本的语言, 与目标语言对比 (拉丁语系统一视为 en)")
    lines.append("")
    if kg_output_lang_entries:
        ok_count = sum(1 for e in kg_output_lang_entries if e["check"]["status"] in ("ok", "skip_zh"))
        mismatch_entries = [e for e in kg_output_lang_entries if e["check"]["status"] == "mismatch"]
        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append("| 总调用检测数 | %d |" % len(kg_output_lang_entries))
        lines.append("| ✅ 语言匹配 | %d |" % ok_count)
        lines.append("| 🔴 语言不匹配 | %d |" % len(mismatch_entries))
        lines.append("")
        if mismatch_entries:
            lines.append("#### 语言不匹配详情")
            lines.append("")
            lines.append("| 问题 | 执行次 | 目标语言 | 检测语言 | 描述 |")
            lines.append("|------|--------|----------|----------|------|")
            for entry in mismatch_entries:
                check = entry["check"]
                detected_langs = []
                for d in check.get("details", []):
                    if d.get("detected_lang"):
                        detected_langs.append(d["detected_lang"])
                lines.append("| %s | %d | %s | %s | %s |" % (
                    entry["sheet"], entry["run"], entry["lang"],
                    ",".join(detected_langs) if detected_langs else "未知",
                    check["description"]))
            lines.append("")
    else:
        lines.append("> (未捕获到 KG 工具输出，跳过检测)")
        lines.append("")

    if think_analysis:
        lines.append("---")
        lines.append("")
        lines.append("## <think_process> 标签分析")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append("| 标签使用率 | %d/%d (%.0f%%) |" % (
            think_analysis["tag_count"], think_analysis["total"], think_analysis["tag_rate"]))
        lines.append("| 最终结论干净度 | %d/%d (%.0f%%) |" % (
            think_analysis["clean_count"], think_analysis["total"], think_analysis["clean_rate"]))
        lines.append("| 标签+干净双达标 | %.0f%% |" % think_analysis["both_rate"])
        lines.append("")

        non_clean = [(rid, d) for rid, d in think_analysis.get("run_data", {}).items() if not d["clean"]]
        if non_clean:
            lines.append("### 未达标执行")
            lines.append("")
            lines.append("| Run ID | 问题 | 执行次 | 语言 | 原因 |")
            lines.append("|--------|------|--------|------|------|")
            run_id_meta = extra.get("run_id_meta", {})
            for rid, d in non_clean:
                tag_status = "未使用标签" if not d["has_think_tag"] else "结论不干净"
                meta = run_id_meta.get(rid, {})
                lines.append("| %s | %s | %d | %s | %s |" % (
                    rid, meta.get("sheet", "?"), meta.get("run_idx", 0), meta.get("lang", "?"), tag_status))
            lines.append("")
        else:
            lines.append("> ✅ 所有执行均达标")
            lines.append("")

    # ACP 输出过滤检测报告
    acp_suppress_log = extra.get("acp_suppress_log", [])
    if acp_suppress_log:
        lines.append("---")
        lines.append("")
        lines.append("## ACP 输出过滤检测")
        lines.append("")
        lines.append("> 后端通过 `acp_suppress_script_patterns` 配置项抑制匹配工具/脚本的 `tool.finished` 结果事件。")
        lines.append("> 本节验证抑制是否按预期生效：工具被调用（thinking 事件可见）但结果被隐藏（无 result 事件）。")
        lines.append("")
        ok_entries = [e for e in acp_suppress_log if e["result"]["status"] == "ok"]
        leak_entries = [e for e in acp_suppress_log if e["result"]["status"] == "leak"]
        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append("| 过滤模式 | `%s` |" % ACP_SUPPRESS_SCRIPT_PATTERNS)
        lines.append("| 总执行次数 | %d |" % len(acp_suppress_log))
        lines.append("| ✅ 过滤生效 | %d |" % len(ok_entries))
        lines.append("| ⚠️ 过滤泄漏 | %d |" % len(leak_entries))
        lines.append("")
        if leak_entries:
            lines.append("### 过滤泄漏详情")
            lines.append("")
            lines.append("| 问题 | 执行次 | 语言 | 已过滤 | 未过滤 |")
            lines.append("|------|--------|------|--------|--------|")
            for entry in leak_entries:
                r = entry["result"]
                lines.append("| %s | %d | %s | %s | %s |" % (
                    entry["sheet"], entry["run"], entry["lang"],
                    ", ".join(r["suppressed"]) if r["suppressed"] else "无",
                    ", ".join(r["unsuppressed"]) if r["unsuppressed"] else "无"))
            lines.append("")

    content = "\n".join(lines)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"\n📄 测试报告已保存: {filepath}")
    return filepath


def _execute_single_run(
    message: str,
    sheet_name: str,
    lang_code: str,
    lang_display: str,
    run_index: int,
    total_runs: int,
    question_idx: int,
) -> dict:
    print(f"\n--- 第 {run_index}/{total_runs} 次执行 ---")

    if API_MODE == "acp":
        # ACP 模式不保存消息内容到 ChatStore，因此也不应创建会话/标题
        session_id = f"sess_{uuid.uuid4().hex[:8]}"
        print(f"  [ACP] 跳过会话创建（不保存内容），使用临时 session_id: {session_id}")
    else:
        session_id = create_session()
        display_title = f"{sheet_name}: {message[:50]}"
        generate_session_title(session_id, display_title)
        print(f"  Session ID: {session_id}")

    start_time = datetime.now()
    start_ts = time.time()

    rd: dict = {
        "run_index": run_index,
        "session_id": session_id,
        "start_time": start_time,
        "elapsed": 0.0,
        "duration_str": "",
        "total_tokens": 0,
        "llm_count": 0,
        "llm_total_time": 0.0,
        "llm_avg_time": 0.0,
        "run_script_stats": {},
        "run_first_token_latency": 0.0,
        "run_avg_inter_token_latency": 0.0,
        "success": False,
        "run_id": None,
        "result": None,
        "error": None,
        "thinking_findings": [],
        "output_findings": [],
        "kg_findings": None,
        "kg_lang_param_check": None,
        "kg_output_lang_check": None,
        "wos_count": 0,
        "script_call_details": [],
        "failure_details": [],
        "acp_suppress_result": None,  # ACP 输出过滤检测结果
    }

    try:
        if API_MODE == "acp":
            acp_lang = ACP_LANG or lang_code
            if ACP_SUBMODE == "stream":
                result = call_skill_execution_acp_stream(message, session_id, lang=acp_lang)
            elif ACP_SUBMODE == "execute":
                result = call_skill_execution_acp_execute(message, session_id, lang=acp_lang)
            else:
                result = call_skill_execution_acp(message, session_id, lang=acp_lang)
        else:
            result = call_skill_execution(message, session_id)
        elapsed = time.time() - start_ts
        run_id = result.get("run_id")

        dag = result.get("dag_perf", {})
        llm_count = dag.get("llm_call_count", 0) or result.get("llm_call_count", 0)
        llm_total_time = dag.get("llm_total_time", 0)
        worker_time = dag.get("worker_time", 0)
        if llm_total_time == 0:
            llm_total_time = worker_time
        llm_avg_time = llm_total_time / llm_count if llm_count > 0 else 0
        run_script_stats = result.get("script_stats", {})

        rd["elapsed"] = elapsed
        rd["result"] = result

        if result.get("error"):
            print(f"  执行错误: {result.get('error')}")
            rd["error"] = result.get("error")
        else:
            rd["success"] = True
            rd["run_id"] = run_id
            total_tokens = dag.get("total_tokens", 0)
            tool_count = len(result.get("tool_calls", []))

            rd["total_tokens"] = total_tokens
            rd["llm_count"] = llm_count
            rd["llm_total_time"] = llm_total_time
            rd["llm_avg_time"] = llm_avg_time
            rd["run_script_stats"] = run_script_stats

            print(f"  成功 | 耗时: {format_duration(elapsed)} | Turns: {result.get('turn_count', 0)}")
            print(f"  Run ID: {run_id}")
            print(f"  Tool Calls: {tool_count} | Tokens: {total_tokens}")
            print(f"  LLM调用: {llm_count}次 | LLM总耗时: {llm_total_time:.1f}s | LLM平均耗时: {llm_avg_time:.1f}s")
            print(f"  Worker: {worker_time:.1f}s")
            run_ftl = result.get("first_token_latency", 0.0)
            run_aitl = result.get("avg_inter_token_latency", 0.0)
            rd["run_first_token_latency"] = run_ftl
            rd["run_avg_inter_token_latency"] = run_aitl
            if run_ftl > 0 or run_aitl > 0:
                print(f"  首Token时延: {run_ftl:.3f}s | 增量Token平均时延: {run_aitl:.3f}s")
            if run_script_stats:
                for sn, ss in run_script_stats.items():
                    if ss['count'] > 0:
                        total_ms = ss.get('total_ms', 0)
                        if total_ms > 0:
                            print(f"  脚本 {sn}: {ss['count']}次 | 总耗时: {total_ms/1000:.1f}s | 平均: {total_ms/ss['count']/1000:.1f}s")
                        else:
                            total_len = ss.get('total_result_length', 0)
                            print(f"  脚本 {sn}: {ss['count']}次 | 结果总长: {total_len}")
                    else:
                        print(f"  脚本 {sn}: 0次")
            if result.get("final_output"):
                output_preview = str(result["final_output"])[:100]
                print(f"  输出预览: {output_preview}...")

            _lang_follow = _check_lang_follow_enabled()
            if lang_code not in ("zh", "zh-cn") and _lang_follow is not False:
                classified = _detect_chinese_in_events(result.get("raw_events", []), lang_code)
                thinking_findings = classified["thinking"]
                output_findings = classified["output"]
                final_output_text = result.get("final_output") or ""
                if final_output_text.strip():
                    final_classification = _classify_chinese_finding(final_output_text, "final_output", lang_code)
                    if final_classification["severity"] != "none" and _should_report_finding(final_classification["severity"]):
                        output_findings.append({
                            "event_type": "final_output",
                            "text": final_output_text.strip(),
                            "category": final_classification["category"],
                            "severity": final_classification["severity"],
                            "description": final_classification["description"],
                        })
                rd["thinking_findings"] = thinking_findings
                rd["output_findings"] = output_findings

                all_findings = thinking_findings + output_findings
                if all_findings:
                    t_err = sum(1 for finding in thinking_findings if finding.get("severity") == "error")
                    t_warn = sum(1 for finding in thinking_findings if finding.get("severity") == "warn")
                    o_err = sum(1 for finding in output_findings if finding.get("severity") == "error")
                    o_warn = sum(1 for finding in output_findings if finding.get("severity") == "warn")
                    print(f"  ⚠️ 检测到中文 (思考过程 {len(thinking_findings)} 处: 严重{t_err}/警告{t_warn}; 最终报告 {len(output_findings)} 处: 严重{o_err}/警告{o_warn}):")
                    for finding in thinking_findings:
                        sev_icon = {"error": "🔴", "warn": "🟡", "info": "ℹ️"}.get(finding.get("severity", ""), "❓")
                        cat_label = {
                            "filename_reference": "文件引用",
                            "quoted_instruction": "引用指令",
                            "minor_mix": "少量混入",
                            "mixed_language": "中英混杂",
                            "pure_chinese": "纯中文",
                        }.get(finding.get("category", ""), finding.get("category", ""))
                        round_match = re.search(r'Round (\d+)', finding['text'])
                        round_label = f"Round {round_match.group(1)}" if round_match else "?"
                        chinese_snippets = re.findall(r'[\u4e00-\u9fff][\u4e00-\u9fff：，、（）\s\w]*[\u4e00-\u9fff]', finding['text'])
                        snippet_display = '; '.join(chinese_snippets[:3]) if chinese_snippets else ''
                        print(f"    {sev_icon} [思考] {round_label} | {cat_label} | {finding.get('description', '')}")
                        if snippet_display:
                            print(f"      中文片段: {snippet_display}")
                    for finding in output_findings:
                        sev_icon = {"error": "🔴", "warn": "🟡", "info": "ℹ️"}.get(finding.get("severity", ""), "❓")
                        cat_label = {
                            "filename_reference": "文件引用",
                            "quoted_instruction": "引用指令",
                            "minor_mix": "少量混入",
                            "mixed_language": "中英混杂",
                            "pure_chinese": "纯中文",
                        }.get(finding.get("category", ""), finding.get("category", ""))
                        chinese_snippets = re.findall(r'[\u4e00-\u9fff][\u4e00-\u9fff：，、（）\s\w]*[\u4e00-\u9fff]', finding['text'])
                        snippet_display = '; '.join(chinese_snippets[:3]) if chinese_snippets else ''
                        print(f"    {sev_icon} [报告] | {cat_label} | {finding.get('description', '')}")
                        if snippet_display:
                            print(f"      中文片段: {snippet_display}")

                kg_outputs = result.get("kg_tool_outputs", [])
                if kg_outputs and lang_code not in ("zh", "zh-cn"):
                    kg_findings = _detect_chinese_in_kg_outputs(kg_outputs, lang_code)
                    if kg_findings:
                        print(f"  🔍 knowledge_graph_tool 返回含中文 ({len(kg_findings)} 次):")
                        for kf in kg_findings:
                            print(f"    中文键: {kf['chinese_keys'][:5]}")
                            print(f"    中文值示例: {kf['chinese_values'][:3]}")
                            print(f"    中文占比: {kf['chinese_ratio']:.1%}")
                        rd["kg_findings"] = kg_findings

                final_output_text = result.get("final_output") or ""
                if lang_code not in ("zh", "zh-cn") and final_output_text:
                    finding = _classify_chinese_finding(final_output_text, "final_output", lang_code)
                    if finding["severity"] not in ("none",):
                        print(f"  ❌ 最终输出含中文 (期望=%s, %s)" % (lang_code, finding.get("description", "")))
            elif lang_code not in ("zh", "zh-cn") and _lang_follow is False:
                print(f"  ⏭️ 语言跟随未开启，跳过中文混入检测")

            # 直接用result的kg_tool_inputs，parse_acp_sse_stream里已经正确提取了！
            kg_inputs = result.get("kg_tool_inputs", [])
            if kg_inputs:
                lang_param_check = _validate_kg_language_param(kg_inputs, lang_code)
                rd["kg_lang_param_check"] = lang_param_check
                status_icon = {"ok": "✅", "missing": "🔴", "mismatch": "🔴", "is_display_name": "🔴", "no_exec_cmd": "🟡", "no_input": "🟡"}.get(lang_param_check["status"], "❓")
                print(f"  {status_icon} KG工具language参数检测: {lang_param_check['description']}")
                if lang_param_check.get("exec_cmd"):
                    print(f"    执行命令: {lang_param_check['exec_cmd']}")

            kg_outputs_all = result.get("kg_tool_outputs", [])
            if kg_outputs_all:
                output_lang_check = _detect_kg_output_lang_match(kg_outputs_all, lang_code)
                rd["kg_output_lang_check"] = output_lang_check
                if output_lang_check["status"] not in ("no_output", "skip_zh"):
                    ol_icon = {"ok": "✅", "mismatch": "🔴"}.get(output_lang_check["status"], "❓")
                    print(f"  {ol_icon} KG工具输出语言匹配: {output_lang_check['description']}")

            rd["wos_count"] = run_script_stats.get("work_order_sync.py", {}).get("count", 0)
            rd["script_call_details"] = result.get("script_call_details", [])

            # ACP 输出过滤验证
            if API_MODE == "acp" and ACP_SUPPRESS_SCRIPT_PATTERNS:
                suppress_keywords = [kw.strip().lower() for kw in ACP_SUPPRESS_SCRIPT_PATTERNS.split(",") if kw.strip()]
                raw_evts = result.get("raw_events", [])

                tools_started: list[dict] = []
                tools_finished: list[dict] = []
                for evt in raw_evts:
                    en = evt.get("event", "")
                    ed = evt.get("data", {})
                    if en == "thinking":
                        t = ed.get("text", "")
                        if t and ("调用工具" in t or "业务数据查询" in t):
                            _tn = ""
                            if "调用工具:" in t:
                                _tn = t.split("调用工具:")[-1].strip().split("\n")[0].strip()
                            elif "业务数据查询" in t:
                                _tn = "business_data_search"
                            if _tn:
                                tools_started.append({"tool_name": _tn, "text": t})
                    elif en == "result":
                        _tn = ed.get("tool_name", "")
                        _ok = ed.get("operation_key", "")
                        if _tn:
                            tools_finished.append({"tool_name": _tn, "operation_key": _ok})

                suppressed_tools_found: list[str] = []
                unsuppressed_tools_found: list[str] = []
                for ts in tools_started:
                    tn = ts["tool_name"].lower()
                    matched_kw = None
                    for kw in suppress_keywords:
                        if kw in tn:
                            matched_kw = kw
                            break
                    if matched_kw is None:
                        continue
                    has_result = any(
                        tf["tool_name"].lower() == tn
                        for tf in tools_finished
                    )
                    label = f"{ts['tool_name']}({matched_kw})"
                    if has_result:
                        unsuppressed_tools_found.append(label)
                    else:
                        suppressed_tools_found.append(label)

                for tf in tools_finished:
                    _ok = (tf.get("operation_key") or "").lower()
                    for kw in suppress_keywords:
                        if kw in _ok:
                            label = f"{tf['tool_name']}({tf.get('operation_key', '')})"
                            unsuppressed_tools_found.append(label)
                            break

                rd["acp_suppress_result"] = {
                    "patterns": ACP_SUPPRESS_SCRIPT_PATTERNS,
                    "suppressed": suppressed_tools_found,
                    "unsuppressed": unsuppressed_tools_found,
                    "status": "ok" if not unsuppressed_tools_found else "leak",
                }
                if suppressed_tools_found:
                    print(f"  🔇 ACP输出过滤生效: {', '.join(suppressed_tools_found)} 的输出已被抑制")
                if unsuppressed_tools_found:
                    print(f"  ⚠️ ACP输出过滤泄漏: {', '.join(unsuppressed_tools_found)} 的输出未被过滤")

    except Exception as e:
        elapsed = time.time() - start_ts
        print(f"  请求异常: {e}")
        rd["elapsed"] = elapsed
        rd["error"] = str(e)

    rd["failure_details"] = _read_snapshot_failures(session_id)
    if rd["failure_details"]:
        print(f"  ❌ 捕获到 {len(rd['failure_details'])} 个失败事件:")
        for fd in rd["failure_details"]:
            print(f"     [{fd['type']}] {fd.get('tool_name', '')}: {fd['error'][:120]}")

    rd["duration_str"] = format_duration(rd["elapsed"])

    _save_run_log(
        sheet_name=sheet_name,
        run_index=run_index,
        message=message,
        session_id=session_id,
        elapsed=rd["elapsed"],
        result=rd["result"] or {},
        start_time=start_time,
    )

    return rd


def run_tests():
    if API_MODE == "acp":
        mode_label = f"ACP/{ACP_SUBMODE}"
    else:
        mode_label = "Agentic"
    print(f"开始测试... [{mode_label}] -> {BASE_URL}")

    lang_follow = _check_lang_follow_enabled()
    target_lang_code = ACP_LANG or _resolve_test_language() or "zh"
    target_lang_display = get_lang_display_name(target_lang_code)
    if lang_follow is True:
        print(f"🌐 语言跟随: 已开启 — 输出目标语言为 {target_lang_display} ({target_lang_code})，将执行中文混入检测")
    elif lang_follow is False:
        print(f"🌐 语言跟随: 未开启 — 跳过中文混入检测 (Agent 输出可能包含中文)")
    else:
        print(f"🌐 语言跟随: 无法检测 — 默认执行中文混入检测")

    # ACP 输出过滤设置
    acp_suppress_original = ""
    acp_suppress_set = False
    if API_MODE == "acp" and ACP_SUPPRESS_SCRIPT_PATTERNS:
        acp_suppress_original = _get_acp_suppress_patterns()
        acp_suppress_set = _set_acp_suppress_patterns(ACP_SUPPRESS_SCRIPT_PATTERNS)
        if acp_suppress_set:
            print(f"🔧 ACP 输出过滤: 已设置 acp_suppress_script_patterns = '{ACP_SUPPRESS_SCRIPT_PATTERNS}' (原值: '{acp_suppress_original}')")
        else:
            print(f"⚠️ ACP 输出过滤: 设置失败，将使用服务端默认值")
    elif API_MODE == "acp":
        current_patterns = _get_acp_suppress_patterns()
        print(f"🔧 ACP 输出过滤: 未配置 ACP_SUPPRESS_SCRIPT_PATTERNS，使用服务端当前值: '{current_patterns}'")

    log_baseline = _get_log_line_count()
    successful_run_ids: list[str] = []
    run_id_meta: dict[str, dict] = {}

    global_total_seconds = 0.0
    global_total_tokens = 0
    global_success_count = 0
    global_total_runs = 0
    global_total_llm_count = 0
    global_total_llm_time = 0.0

    first4_total_seconds = 0.0
    first4_total_runs = 0
    first4_success_count = 0
    first4_total_first_token_latency = 0.0
    first4_total_avg_inter_token_latency = 0.0

    rest_total_seconds = 0.0
    rest_total_runs = 0
    rest_success_count = 0
    rest_total_first_token_latency = 0.0
    rest_total_avg_inter_token_latency = 0.0

    global_script_run_details: dict[str, list[dict]] = defaultdict(list)
    global_failure_details: list[dict] = []
    chinese_intrusion_log: list[dict] = []
    kg_chinese_log: list[dict] = []
    kg_lang_param_log: list[dict] = []
    kg_output_lang_log: list[dict] = []
    acp_suppress_log: list[dict] = []
    run_details_log: list[dict] = []  # 逐次执行详情，用于 Markdown 报告
    flywheel_wos_track: dict[str, dict] = {}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if get_env("BENCHMARK_RESULT_DIR"):
        run_dir = _RESULT_BASE_DIR
    else:
        run_dir = os.path.join(_RESULT_BASE_DIR, ts)
    os.makedirs(run_dir, exist_ok=True)
    global _current_run_dir, _current_xlsx_path
    _current_run_dir = run_dir

    xlsx_path = os.path.join(run_dir, "benchmark_%s.xlsx" % ts)
    wb = openpyxl.Workbook()
    _ensure_xlsx(wb)
    wb.save(xlsx_path)
    _current_xlsx_path = xlsx_path
    print(f"📁 本次测试结果目录: {run_dir}")
    print(f"📊 Excel: {xlsx_path}")

    for question_idx, q in enumerate(QUESTIONS):
        if TEST_QUESTION_LIMIT > 0 and question_idx >= TEST_QUESTION_LIMIT:
            print(f"\n  达到问题数量限制 ({TEST_QUESTION_LIMIT})，跳过剩余问题。")
            break
        sheet_name = q["sheet"]
        lang_code = q.get("lang", "zh")
        lang_display = q.get("lang_display", "中文")
        message = q["message"]
        ws = wb[sheet_name]
        style_cell = _get_style_template(ws) or _get_default_style_cell()

        print(f"\n{'='*70}")
        print(f"开始测试: {sheet_name} [{lang_display} ({lang_code})]")
        print(f"问题: {message}")
        print(f"{'='*70}")

        total_seconds = 0.0
        success_count = 0
        total_llm_count = 0
        total_llm_time = 0.0
        agg_script_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_ms": 0})

        start_row = 2
        for r in range(2, ws.max_row + 2):
            if ws.cell(row=r, column=2).value is None and ws.cell(row=r, column=4).value is None:
                start_row = r
                break

        total_tokens_sum = 0
        total_first_token_latency = 0.0
        total_avg_inter_token_latency = 0.0

        if RUNS_EXECUTION_MODE == "parallel" and RUNS_PER_QUESTION > 1:
            print(f"\n  ⚡ 并行执行模式: {RUNS_PER_QUESTION} 次, 最大并发 {RUNS_MAX_CONCURRENCY}")
            excel_lock = threading.Lock()
            run_results: list[dict] = []

            with ThreadPoolExecutor(max_workers=RUNS_MAX_CONCURRENCY) as executor:
                future_map = {}
                for i in range(1, RUNS_PER_QUESTION + 1):
                    future = executor.submit(
                        _execute_single_run,
                        message, sheet_name, lang_code, lang_display,
                        i, RUNS_PER_QUESTION, question_idx,
                    )
                    future_map[future] = i

                for future in as_completed(future_map):
                    try:
                        rd = future.result()
                        run_results.append(rd)
                    except Exception as e:
                        i = future_map[future]
                        print(f"  ❌ 第 {i} 次执行线程异常: {e}")

            run_results.sort(key=lambda x: x["run_index"])

            for rd in run_results:
                i = rd["run_index"]
                row_idx = start_row + i - 1
                elapsed = rd["elapsed"]
                total_tokens = rd["total_tokens"]
                llm_count = rd["llm_count"]
                llm_total_time = rd["llm_total_time"]
                llm_avg_time = rd["llm_avg_time"]
                run_script_stats = rd["run_script_stats"]
                run_first_token_latency = rd["run_first_token_latency"]
                run_avg_inter_token_latency = rd["run_avg_inter_token_latency"]

                with excel_lock:
                    _write_cell(ws, row_idx, 1, i, style_cell)
                    _write_cell(ws, row_idx, 2, rd["start_time"].strftime("%H:%M:%S"), style_cell)
                    _write_cell(ws, row_idx, 3, rd["duration_str"], style_cell)
                    _write_cell(ws, row_idx, 4, round(elapsed, 2), style_cell)
                    _write_cell(ws, row_idx, 5, total_tokens or 0, style_cell)
                    _write_cell(ws, row_idx, 6, llm_count, style_cell)
                    _write_cell(ws, row_idx, 7, round(llm_total_time, 2), style_cell)
                    _write_cell(ws, row_idx, 8, round(llm_avg_time, 2), style_cell)
                    script_summary_parts = []
                    for sn, ss in run_script_stats.items():
                        total_ms_val = ss.get("total_ms", 0)
                        avg_ms = total_ms_val / ss["count"] if ss["count"] > 0 else 0
                        if total_ms_val > 0:
                            script_summary_parts.append(f"{sn}: {ss['count']}次/{total_ms_val/1000:.1f}s/avg{avg_ms/1000:.1f}s")
                        else:
                            script_summary_parts.append(f"{sn}: {ss['count']}次")
                    _write_cell(ws, row_idx, 9, "; ".join(script_summary_parts) if script_summary_parts else "", style_cell)
                    _write_cell(ws, row_idx, 10, run_first_token_latency, style_cell)
                    _write_cell(ws, row_idx, 11, run_avg_inter_token_latency, style_cell)
                    _write_cell(ws, row_idx, 12, f"{lang_display}({lang_code})", style_cell)
                    wb.save(_current_xlsx_path)

                print(f"  数据已写入第 {row_idx} 行")

                if rd["success"]:
                    success_count += 1
                    if rd["run_id"]:
                        successful_run_ids.append(rd["run_id"])
                        run_id_meta[rd["run_id"]] = {"sheet": sheet_name, "lang": lang_code, "run_idx": i}

                total_tokens_sum += total_tokens
                total_seconds += elapsed
                total_llm_count += llm_count
                total_llm_time += llm_total_time
                total_first_token_latency += run_first_token_latency
                total_avg_inter_token_latency += run_avg_inter_token_latency

                for sn, ss in run_script_stats.items():
                    agg_script_stats[sn]["count"] += ss["count"]
                    agg_script_stats[sn]["total_ms"] += ss.get("total_ms", 0)
                for detail in rd["script_call_details"]:
                    global_script_run_details[detail["name"]].append(detail)

                if rd["failure_details"]:
                    global_failure_details.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "session_id": rd["session_id"],
                        "failures": rd["failure_details"],
                    })

                if rd["thinking_findings"] or rd["output_findings"]:
                    chinese_intrusion_log.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "thinking_findings": rd["thinking_findings"],
                        "output_findings": rd["output_findings"],
                        "findings": rd["thinking_findings"] + rd["output_findings"],
                    })
                if rd["kg_findings"]:
                    kg_chinese_log.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "findings": rd["kg_findings"],
                    })
                if rd.get("kg_lang_param_check"):
                    kg_lang_param_log.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "check": rd["kg_lang_param_check"],
                    })
                if rd.get("kg_output_lang_check"):
                    kg_output_lang_log.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "check": rd["kg_output_lang_check"],
                    })

                if rd.get("acp_suppress_result"):
                    acp_suppress_log.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "result": rd["acp_suppress_result"],
                    })

                # 收集逐次执行详情
                _inter = _extract_intermediate_summary(rd.get("result") or {})
                run_details_log.append({
                    "sheet": sheet_name, "run": i, "lang": lang_code,
                    "elapsed": rd["elapsed"], "success": rd["success"],
                    "run_id": rd["run_id"],
                    "final_output": (rd.get("result") or {}).get("final_output", ""),
                    "thinking": _inter["thinking"],
                    "tool_steps": _inter["tool_steps"],
                    "progress": _inter["progress"],
                    "user_visible": _inter.get("user_visible", ""),
                })

                if question_idx < 4:
                    if sheet_name not in flywheel_wos_track:
                        flywheel_wos_track[sheet_name] = {"runs": 0, "wos_calls": 0}
                    flywheel_wos_track[sheet_name]["runs"] += 1
                    if rd["wos_count"] > 0:
                        flywheel_wos_track[sheet_name]["wos_calls"] += rd["wos_count"]

        else:
            if RUNS_PER_QUESTION > 1:
                print(f"\n  🔗 串行执行模式: {RUNS_PER_QUESTION} 次")

            for i in range(1, RUNS_PER_QUESTION + 1):
                row_idx = start_row + i - 1
                rd = _execute_single_run(
                    message, sheet_name, lang_code, lang_display,
                    i, RUNS_PER_QUESTION, question_idx,
                )

                elapsed = rd["elapsed"]
                total_tokens = rd["total_tokens"]
                llm_count = rd["llm_count"]
                llm_total_time = rd["llm_total_time"]
                llm_avg_time = rd["llm_avg_time"]
                run_script_stats = rd["run_script_stats"]
                run_first_token_latency = rd["run_first_token_latency"]
                run_avg_inter_token_latency = rd["run_avg_inter_token_latency"]

                _write_cell(ws, row_idx, 1, i, style_cell)
                _write_cell(ws, row_idx, 2, rd["start_time"].strftime("%H:%M:%S"), style_cell)
                _write_cell(ws, row_idx, 3, rd["duration_str"], style_cell)
                _write_cell(ws, row_idx, 4, round(elapsed, 2), style_cell)
                _write_cell(ws, row_idx, 5, total_tokens or 0, style_cell)
                _write_cell(ws, row_idx, 6, llm_count, style_cell)
                _write_cell(ws, row_idx, 7, round(llm_total_time, 2), style_cell)
                _write_cell(ws, row_idx, 8, round(llm_avg_time, 2), style_cell)
                script_summary_parts = []
                for sn, ss in run_script_stats.items():
                    total_ms_val = ss.get("total_ms", 0)
                    avg_ms = total_ms_val / ss["count"] if ss["count"] > 0 else 0
                    if total_ms_val > 0:
                        script_summary_parts.append(f"{sn}: {ss['count']}次/{total_ms_val/1000:.1f}s/avg{avg_ms/1000:.1f}s")
                    else:
                        script_summary_parts.append(f"{sn}: {ss['count']}次")
                _write_cell(ws, row_idx, 9, "; ".join(script_summary_parts) if script_summary_parts else "", style_cell)
                _write_cell(ws, row_idx, 10, run_first_token_latency, style_cell)
                _write_cell(ws, row_idx, 11, run_avg_inter_token_latency, style_cell)
                _write_cell(ws, row_idx, 12, f"{lang_display}({lang_code})", style_cell)
                wb.save(_current_xlsx_path)
                print(f"  数据已写入第 {row_idx} 行")

                if rd["success"]:
                    success_count += 1
                    if rd["run_id"]:
                        successful_run_ids.append(rd["run_id"])
                        run_id_meta[rd["run_id"]] = {"sheet": sheet_name, "lang": lang_code, "run_idx": i}

                total_tokens_sum += total_tokens
                total_seconds += elapsed
                total_llm_count += llm_count
                total_llm_time += llm_total_time
                total_first_token_latency += run_first_token_latency
                total_avg_inter_token_latency += run_avg_inter_token_latency

                for sn, ss in run_script_stats.items():
                    agg_script_stats[sn]["count"] += ss["count"]
                    agg_script_stats[sn]["total_ms"] += ss.get("total_ms", 0)
                for detail in rd["script_call_details"]:
                    global_script_run_details[detail["name"]].append(detail)

                if rd["failure_details"]:
                    global_failure_details.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "session_id": rd["session_id"],
                        "failures": rd["failure_details"],
                    })

                if rd["thinking_findings"] or rd["output_findings"]:
                    chinese_intrusion_log.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "thinking_findings": rd["thinking_findings"],
                        "output_findings": rd["output_findings"],
                        "findings": rd["thinking_findings"] + rd["output_findings"],
                    })
                if rd["kg_findings"]:
                    kg_chinese_log.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "findings": rd["kg_findings"],
                    })
                if rd.get("kg_lang_param_check"):
                    kg_lang_param_log.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "check": rd["kg_lang_param_check"],
                    })
                if rd.get("kg_output_lang_check"):
                    kg_output_lang_log.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "check": rd["kg_output_lang_check"],
                    })

                if rd.get("acp_suppress_result"):
                    acp_suppress_log.append({
                        "sheet": sheet_name, "run": i, "lang": lang_code,
                        "result": rd["acp_suppress_result"],
                    })

                # 收集逐次执行详情
                _inter = _extract_intermediate_summary(rd.get("result") or {})
                run_details_log.append({
                    "sheet": sheet_name, "run": i, "lang": lang_code,
                    "elapsed": rd["elapsed"], "success": rd["success"],
                    "run_id": rd["run_id"],
                    "final_output": (rd.get("result") or {}).get("final_output", ""),
                    "thinking": _inter["thinking"],
                    "tool_steps": _inter["tool_steps"],
                    "progress": _inter["progress"],
                    "user_visible": _inter.get("user_visible", ""),
                })

                if question_idx < 4:
                    if sheet_name not in flywheel_wos_track:
                        flywheel_wos_track[sheet_name] = {"runs": 0, "wos_calls": 0}
                    flywheel_wos_track[sheet_name]["runs"] += 1
                    if rd["wos_count"] > 0:
                        flywheel_wos_track[sheet_name]["wos_calls"] += rd["wos_count"]

        avg_seconds = total_seconds / RUNS_PER_QUESTION
        avg_duration_str = format_duration(avg_seconds)
        avg_tokens = total_tokens_sum / RUNS_PER_QUESTION
        avg_llm_count = total_llm_count / RUNS_PER_QUESTION
        avg_llm_time = total_llm_time / RUNS_PER_QUESTION
        avg_llm_avg_time = total_llm_time / total_llm_count if total_llm_count > 0 else 0
        avg_row = start_row + RUNS_PER_QUESTION
        _write_cell(ws, avg_row, 1, f"{RUNS_PER_QUESTION}次平均耗时", style_cell)
        _write_cell(ws, avg_row, 3, avg_duration_str, style_cell)
        _write_cell(ws, avg_row, 4, round(avg_seconds, 2), style_cell)
        _write_cell(ws, avg_row, 5, round(avg_tokens, 0), style_cell)
        _write_cell(ws, avg_row, 6, round(avg_llm_count, 1), style_cell)
        _write_cell(ws, avg_row, 7, round(avg_llm_time, 2), style_cell)
        _write_cell(ws, avg_row, 8, round(avg_llm_avg_time, 2), style_cell)
        agg_script_parts = []
        for sn, ss in agg_script_stats.items():
            total_ms_val = ss.get("total_ms", 0)
            avg_ms = total_ms_val / ss["count"] if ss["count"] > 0 else 0
            avg_count = ss["count"] / RUNS_PER_QUESTION
            if total_ms_val > 0:
                avg_total_s = total_ms_val / RUNS_PER_QUESTION / 1000
                agg_script_parts.append(f"{sn}: avg{avg_count:.1f}次/{avg_total_s:.1f}s/avg{avg_ms/1000:.1f}s")
            else:
                agg_script_parts.append(f"{sn}: avg{avg_count:.1f}次")
        _write_cell(ws, avg_row, 9, "; ".join(agg_script_parts) if agg_script_parts else "", style_cell)
        avg_first_token_latency = total_first_token_latency / RUNS_PER_QUESTION
        avg_avg_inter_token_latency = total_avg_inter_token_latency / RUNS_PER_QUESTION
        _write_cell(ws, avg_row, 10, round(avg_first_token_latency, 3), style_cell)
        _write_cell(ws, avg_row, 11, round(avg_avg_inter_token_latency, 3), style_cell)
        _write_cell(ws, avg_row, 12, f"{lang_display}({lang_code})", style_cell)

        wb.save(_current_xlsx_path)
        print(f"\n{sheet_name} 完成: 平均耗时 {avg_duration_str}, 成功 {success_count}/{RUNS_PER_QUESTION}")

        global_total_seconds += total_seconds
        global_total_tokens += total_tokens_sum
        global_success_count += success_count
        global_total_runs += RUNS_PER_QUESTION
        global_total_llm_count += total_llm_count
        global_total_llm_time += total_llm_time

        if question_idx < 4:
            first4_total_seconds += total_seconds
            first4_total_runs += RUNS_PER_QUESTION
            first4_success_count += success_count
            first4_total_first_token_latency += total_first_token_latency
            first4_total_avg_inter_token_latency += total_avg_inter_token_latency
        else:
            rest_total_seconds += total_seconds
            rest_total_runs += RUNS_PER_QUESTION
            rest_success_count += success_count
            rest_total_first_token_latency += total_first_token_latency
            rest_total_avg_inter_token_latency += total_avg_inter_token_latency

    wb.close()

    global_avg_seconds = global_total_seconds / global_total_runs if global_total_runs > 0 else 0
    global_avg_duration_str = format_duration(global_avg_seconds)
    global_avg_tokens = global_total_tokens / global_total_runs if global_total_runs > 0 else 0

    print(f"\n{'='*70}")
    print("所有测试完成！结果已保存到 Excel 文件。")
    print(f"{'='*70}")
    print(f"\n{'='*70}")
    print(f"📊 全局统计汇总")
    print(f"  总问题数: {len(QUESTIONS)}")
    print(f"  总执行次数: {global_total_runs}")
    print(f"  全局平均耗时: {global_avg_duration_str} ({global_avg_seconds:.2f}s)")
    print(f"  全局平均 Tokens: {round(global_avg_tokens, 0)}")
    print(f"  全局成功率: {global_success_count}/{global_total_runs} ({global_success_count / global_total_runs * 100:.1f}%)" if global_total_runs > 0 else "")
    print(f"{'='*70}")

    print(f"\n{'='*70}")
    print(f"📊 额外统计")
    print(f"{'='*70}")

    print(f"\n--- 1、前4个问题统计 ---")
    if first4_total_runs > 0:
        first4_avg = first4_total_seconds / first4_total_runs
        print(f"  前4个问题执行总耗时: {format_duration(first4_total_seconds)} ({first4_total_seconds:.2f}s)")
        print(f"  执行 {first4_total_runs} 次, 平均执行时间: {format_duration(first4_avg)} ({first4_avg:.2f}s)")
    else:
        print(f"  无前4个问题数据")

    print(f"\n--- 2、后面所有问题统计 ---")
    if rest_total_runs > 0:
        rest_avg = rest_total_seconds / rest_total_runs
        print(f"  后面所有问题执行总耗时: {format_duration(rest_total_seconds)} ({rest_total_seconds:.2f}s)")
        print(f"  执行 {rest_total_runs} 次, 平均执行时间: {format_duration(rest_avg)} ({rest_avg:.2f}s)")
    else:
        print(f"  无后面问题数据")

    print(f"\n--- 3、首token时延 & 增量token时延 ---")
    total_successful_runs = first4_success_count + rest_success_count
    total_ftl = first4_total_first_token_latency + rest_total_first_token_latency
    total_aitl = first4_total_avg_inter_token_latency + rest_total_avg_inter_token_latency
    if total_successful_runs > 0:
        print(f"  平均首Token时延: {total_ftl / total_successful_runs:.3f}s")
        print(f"  平均增量Token时延: {total_aitl / total_successful_runs:.3f}s")
    else:
        print(f"  无成功执行数据")

    def _print_script_stats(script_name: str):
        details = global_script_run_details.get(script_name, [])
        total_calls = len(details)
        if total_calls == 0:
            print(f"  {script_name}: 未执行")
            return
        all_ms = [d.get("elapsed_ms", 0) for d in details]
        fail_details = [d for d in details if d.get("status") == "error"]
        fail_count = len(fail_details)
        success_details = [d for d in details if d.get("status") != "error"]
        success_count = len(success_details)
        if any(ms > 0 for ms in all_ms):
            avg_ms = sum(all_ms) / total_calls
            max_ms = max(all_ms)
            print(f"  {script_name}:")
            print(f"    总执行: {total_calls} 次, 平均: {avg_ms/1000:.2f}s, 最慢: {max_ms/1000:.2f}s, 失败: {fail_count} 次")
            if success_count > 0:
                success_ms = [d.get("elapsed_ms", 0) for d in success_details]
                success_avg = sum(success_ms) / success_count
                success_max = max(success_ms)
                print(f"    排除失败: {success_count} 次, 平均: {success_avg/1000:.2f}s, 最慢: {success_max/1000:.2f}s")
            else:
                print(f"    排除失败: 0 次")
        else:
            print(f"  {script_name}:")
            print(f"    总执行: {total_calls} 次, 失败: {fail_count} 次")
            if success_count > 0:
                result_lengths = [d.get("result_length", 0) for d in success_details]
                print(f"    排除失败: {success_count} 次, 平均结果长度: {sum(result_lengths)/success_count:.0f}")

    print(f"\n--- 4、knowledge_graph_tool.py 执行统计 ---")
    _print_script_stats("knowledge_graph_tool.py")

    print(f"\n--- 5、mindsdb_tool.py 执行统计 ---")
    _print_script_stats("business_data_search.py")

    print(f"\n--- 5.5、work_order_sync.py 执行统计 ---")
    _print_script_stats("work_order_sync.py")

    print(f"\n--- 5.6、飞轮问题 work_order_sync.py 覆盖检测 ---")
    flywheel_sheets = [f"问题{i}" for i in range(1, 5)]
    has_flywheel_data = any(s in flywheel_wos_track for s in flywheel_sheets)
    if has_flywheel_data:
        all_wos_ok = True
        for sheet in flywheel_sheets:
            track = flywheel_wos_track.get(sheet)
            if not track:
                print(f"  🔴 {sheet}: 未执行 (缺少飞轮数据)")
                all_wos_ok = False
                continue
            runs = track["runs"]
            wos_calls = track["wos_calls"]
            if wos_calls == 0:
                print(f"  🔴 {sheet}: 执行 {runs} 次, work_order_sync.py 未被调用 (异常!)")
                all_wos_ok = False
            else:
                coverage = runs / runs * 100 if runs > 0 else 0
                print(f"  ✅ {sheet}: 执行 {runs} 次, work_order_sync.py 调用 {wos_calls} 次 (覆盖率 {coverage:.0f}%)")
        if all_wos_ok:
            print(f"  ✅ 所有飞轮问题均正常调用 work_order_sync.py")
        else:
            print(f"  ⚠️ 存在飞轮问题未调用 work_order_sync.py，请检查!")
    else:
        print(f"  (无飞轮问题执行数据，跳过检测)")

    _lang_follow_final = _check_lang_follow_enabled()
    if _lang_follow_final is False:
        print(f"\n--- 6、中文混入检测 (最低严重度: {CHINESE_DETECT_MIN_SEVERITY}) ---")
        print(f"  ⏭️ 语言跟随未开启，跳过中文混入检测")
        print(f"  💡 提示: 开启前端「语言跟随」后，Agent 输出将跟随用户语言，此时再进行中文混入检测")
    else:
        print(f"\n--- 6、中文混入检测 (最低严重度: {CHINESE_DETECT_MIN_SEVERITY}) ---")
        method_counts: dict[str, int] = defaultdict(int)
        for entry in chinese_intrusion_log:
            for finding in entry.get("thinking_findings", []):
                desc = finding.get("description", "")
                method = "LLM" if "LLM" in desc else ("启发式" if "假名" in desc or "谚文" in desc else "规则")
                method_counts[method] += 1
            for finding in entry.get("output_findings", []):
                desc = finding.get("description", "")
                method = "LLM" if "LLM" in desc else ("启发式" if "假名" in desc or "谚文" in desc else "规则")
                method_counts[method] += 1
        method_summary = " | ".join("%s %d次" % (m, c) for m, c in sorted(method_counts.items()))
        print(f"    本次实际使用: {method_summary}")
        if chinese_intrusion_log:
            all_thinking: list[dict] = []
            all_output: list[dict] = []
            for entry in chinese_intrusion_log:
                all_thinking.extend(entry.get("thinking_findings", []))
                all_output.extend(entry.get("output_findings", []))

            category_labels = {
                "filename_reference": "文件名引用 (info)",
                "quoted_instruction": "引用中文指令 (warn)",
                "minor_mix": "少量中文混入 (warn)",
                "mixed_language": "中英混杂 (error)",
                "pure_chinese": "纯中文 (error)",
            }

            def _print_section(title, findings_list):
                print(f"\n  === {title} ({len(findings_list)} 处) ===")
                if not findings_list:
                    print(f"    ✅ 无中文混入")
                    return
                sev_counts = {"error": 0, "warn": 0, "info": 0}
                cat_counts: dict[str, int] = defaultdict(int)
                for finding in findings_list:
                    sev = finding.get("severity", "info")
                    cat = finding.get("category", "unknown")
                    if sev in sev_counts:
                        sev_counts[sev] += 1
                    cat_counts[cat] += 1
                print(f"    🔴 严重: {sev_counts['error']} 处 | 🟡 警告: {sev_counts['warn']} 处 | ℹ️ 信息: {sev_counts['info']} 处")
                for cat, count in sorted(cat_counts.items(), key=lambda x: x[1], reverse=True):
                    label = category_labels.get(cat, cat)
                    print(f"    {label}: {count} 处")
                affected_runs = set()
                for entry in chinese_intrusion_log:
                    src = entry.get("thinking_findings", []) if "思考" in title else entry.get("output_findings", [])
                    if src:
                        affected_runs.add((entry["sheet"], entry["run"], entry["lang"]))
                if affected_runs:
                    print(f"    涉及 {len(affected_runs)} 次执行")

            _print_section("思考过程 (thinking/reasoning)", all_thinking)
            _print_section("最终报告 (token.delta/final_output)", all_output)

            if all_output:
                output_errors = [finding for finding in all_output if finding.get("severity") == "error"]
                if output_errors:
                    print(f"\n  🔴 最终报告严重问题详情:")
                    for entry in chinese_intrusion_log:
                        entry_output_errors = [finding for finding in entry.get("output_findings", []) if finding.get("severity") == "error"]
                        if entry_output_errors:
                            print(f"  [{entry['sheet']}] 第{entry['run']}次 ({entry['lang']}):")
                            for finding in entry_output_errors:
                                cat_label = category_labels.get(finding.get("category", ""), finding.get("category", ""))
                                print(f"    🔴 [报告] | [{cat_label}] {finding.get('description', '')}")
                                chinese_snippets = re.findall(r'[\u4e00-\u9fff][\u4e00-\u9fff：，、（）\s\w]*[\u4e00-\u9fff]', finding['text'])
                                if chinese_snippets:
                                    print(f"       中文片段: {'; '.join(chinese_snippets[:3])}")

            if all_thinking:
                thinking_errors = [finding for finding in all_thinking if finding.get("severity") == "error"]
                if thinking_errors:
                    print(f"\n  🔴 思考过程严重问题详情 (仅显示前5条):")
                    shown = 0
                    for entry in chinese_intrusion_log:
                        entry_thinking_errors = [finding for finding in entry.get("thinking_findings", []) if finding.get("severity") == "error"]
                        if entry_thinking_errors:
                            print(f"  [{entry['sheet']}] 第{entry['run']}次 ({entry['lang']}): {len(entry_thinking_errors)} 处")
                            shown += 1
                            if shown >= 5:
                                break
        else:
            print(f"  ✅ 未检测到中文混入")

        non_zh_total = sum(RUNS_PER_QUESTION for q in QUESTIONS if q.get("lang", "zh") not in ("zh", "zh-cn"))
        if non_zh_total > 0:
            non_zh_clean = non_zh_total - len(chinese_intrusion_log)
            print(f"  📊 非中文场景共 {non_zh_total} 次执行, 无中文混入 {non_zh_clean}/{non_zh_total} ({non_zh_clean / non_zh_total * 100:.1f}%)")
        else:
            print(f"  📊 无非中文场景执行, 跳过中文混入统计")

    print(f"\n--- 7、knowledge_graph_tool 返回中文检测 ---")
    print(f"    本次实际使用: Unicode CJK 字符范围扫描 (U+4E00-U+9FFF), 统计 JSON 键值中的中文字符占比")
    if kg_chinese_log:
        total_kg_runs = len(kg_chinese_log)
        all_kg_keys: list[str] = []
        all_kg_values: list[str] = []
        for entry in kg_chinese_log:
            for kf in entry.get("findings", []):
                all_kg_keys.extend(kf.get("chinese_keys", []))
                all_kg_values.extend(kf.get("chinese_values", []))
        unique_keys = sorted(set(all_kg_keys))
        unique_values_sample = sorted(set(all_kg_values))[:10]
        print(f"  共 {total_kg_runs} 次执行检测到 KG 工具返回含中文")
        print(f"  中文 JSON 键名 ({len(unique_keys)} 个):")
        for k in unique_keys:
            print(f"    - {k}")
        if unique_values_sample:
            print(f"  中文值示例 ({len(set(all_kg_values))} 个, 展示前10):")
            for v in unique_values_sample:
                print(f"    - {v}")
        print(f"  详细记录:")
        for entry in kg_chinese_log:
            print(f"  [{entry['sheet']}] 第{entry['run']}次 ({entry['lang']}):")
            for kf in entry.get("findings", []):
                print(f"    中文字符: {kf['chinese_char_count']} | 占比: {kf['chinese_ratio']:.1%} | 键: {kf['chinese_keys'][:5]}")
    else:
        print(f"  ✅ knowledge_graph_tool 返回数据中未检测到中文 (或未调用该工具)")

    print(f"\n--- 8、knowledge_graph_tool language参数检测 ---")
    print(f"    本次实际使用: 解析输入参数 JSON 中的 language 字段, 与期望语言对比")
    if kg_lang_param_log:
        ok_count = sum(1 for e in kg_lang_param_log if e["check"]["status"] == "ok")
        issue_count = len(kg_lang_param_log) - ok_count
        print(f"  共 {len(kg_lang_param_log)} 次调用检测到输入参数")
        print(f"  ✅ 参数正确: {ok_count} 次 | 🔴 参数异常: {issue_count} 次")
        for entry in kg_lang_param_log:
            check = entry["check"]
            status = check["status"]
            icon = {"ok": "✅", "missing": "🔴", "mismatch": "🔴", "is_display_name": "🔴", "no_exec_cmd": "🟡", "no_input": "🟡"}.get(status, "❓")
            print(f"  {icon} [{entry['sheet']}] 第{entry['run']}次 ({entry['lang']}): {check['description']}")
            if check.get("exec_cmd"):
                print(f"     执行命令: {check['exec_cmd']}")
    else:
        print(f"  (未捕获到 KG 工具输入参数，跳过检测)")

    print(f"\n--- 9、knowledge_graph_tool 输出语言匹配检测 ---")
    print(f"    本次实际使用: 检测 KG 工具输出文本的语言, 与目标语言对比 (拉丁语系统一视为 en)")
    if kg_output_lang_log:
        ok_count = sum(1 for e in kg_output_lang_log if e["check"]["status"] in ("ok", "skip_zh"))
        mismatch_count = sum(1 for e in kg_output_lang_log if e["check"]["status"] == "mismatch")
        no_output_count = sum(1 for e in kg_output_lang_log if e["check"]["status"] == "no_output")
        print(f"  共 {len(kg_output_lang_log)} 次调用检测到输出")
        print(f"  ✅ 语言匹配: {ok_count} 次 | 🔴 语言不匹配: {mismatch_count} 次 | 🟡 无输出: {no_output_count} 次")
        for entry in kg_output_lang_log:
            check = entry["check"]
            if check["status"] in ("no_output", "skip_zh", "ok"):
                continue
            icon = "🔴"
            print(f"  {icon} [{entry['sheet']}] 第{entry['run']}次 ({entry['lang']}): {check['description']}")
            for d in check.get("details", []):
                print(f"     检测语言={d['detected_lang']} | 目标={d['target_lang']} | 匹配={'是' if d['is_match'] else '否'}")
    else:
        print(f"  (未捕获到 KG 工具输出，跳过检测)")

    # ACP 输出过滤检测结果
    print(f"\n--- 10、ACP 输出过滤检测 ---")
    if acp_suppress_log:
        ok_count = sum(1 for e in acp_suppress_log if e["result"]["status"] == "ok")
        leak_count = sum(1 for e in acp_suppress_log if e["result"]["status"] == "leak")
        print(f"  过滤模式: {ACP_SUPPRESS_SCRIPT_PATTERNS}")
        print(f"  ✅ 过滤生效: {ok_count} 次 | ⚠️ 过滤泄漏: {leak_count} 次")
        for entry in acp_suppress_log:
            r = entry["result"]
            icon = "✅" if r["status"] == "ok" else "⚠️"
            suppressed_str = ", ".join(r["suppressed"]) if r["suppressed"] else "无"
            unsuppressed_str = ", ".join(r["unsuppressed"]) if r["unsuppressed"] else "无"
            print(f"  {icon} [{entry['sheet']}] 第{entry['run']}次 ({entry['lang']}): 已过滤={suppressed_str}, 未过滤={unsuppressed_str}")
    else:
        if API_MODE == "acp":
            print(f"  (未配置 ACP_SUPPRESS_SCRIPT_PATTERNS 或未触发过滤检测)")
        else:
            print(f"  (非 ACP 模式，跳过)")

    think_analysis = _analyze_think_process(successful_run_ids, log_baseline)

    script_stats = {}
    for sn, details in global_script_run_details.items():
        all_ms = [d.get("elapsed_ms", 0) for d in details]
        fail_count = sum(1 for d in details if d.get("status") == "error")
        success_details = [d for d in details if d.get("status") != "error"]
        success_ms = [d.get("elapsed_ms", 0) for d in success_details] if success_details else all_ms
        script_stats[sn] = {
            "count": len(details),
            "avg": sum(success_ms) / len(success_ms) if success_ms else 0,
            "max": max(all_ms) if all_ms else 0,
            "fail": fail_count,
        }

    global_stats_for_md = {
        "total_questions": len(QUESTIONS),
        "total_runs": global_total_runs,
        "success_count": global_success_count,
        "success_rate": global_success_count / global_total_runs * 100 if global_total_runs > 0 else 0,
        "avg_duration": global_avg_duration_str,
        "avg_tokens": global_avg_tokens,
        "avg_first_token_latency": (first4_total_first_token_latency + rest_total_first_token_latency) / (first4_success_count + rest_success_count) if (first4_success_count + rest_success_count) > 0 else 0,
        "avg_inter_token_latency": (first4_total_avg_inter_token_latency + rest_total_avg_inter_token_latency) / (first4_success_count + rest_success_count) if (first4_success_count + rest_success_count) > 0 else 0,
        "avg_llm_call_count": global_total_llm_count / global_total_runs if global_total_runs > 0 else 0,
        "avg_llm_time": global_total_llm_time / global_total_runs if global_total_runs > 0 else 0,
        "avg_llm_avg_time": global_total_llm_time / global_total_llm_count if global_total_llm_count > 0 else 0,
    }

    _generate_md_report(
        system_info=_get_system_info(),
        stats={"scripts": script_stats},
        global_stats=global_stats_for_md,
        think_analysis=think_analysis,
        extra={
            "flywheel_wos_track": flywheel_wos_track,
            "chinese_intrusion_log": chinese_intrusion_log,
            "non_zh_total": sum(RUNS_PER_QUESTION for q in QUESTIONS if q.get("lang", "zh") not in ("zh", "zh-cn")),
            "kg_chinese_log": kg_chinese_log,
            "kg_lang_param_log": kg_lang_param_log,
            "kg_output_lang_log": kg_output_lang_log,
            "acp_suppress_log": acp_suppress_log,
            "run_details_log": run_details_log,
            "run_id_meta": run_id_meta,
            "failure_details": global_failure_details,
            "lang_follow_enabled": _check_lang_follow_enabled(),
        },
    )

    _script_details_path = os.path.join(
        _current_run_dir or os.path.join(_RESULT_BASE_DIR, "misc"),
        "script_call_details.json",
    )
    with open(_script_details_path, "w", encoding="utf-8") as _f:
        json.dump(dict(global_script_run_details), _f, ensure_ascii=False, indent=2)

    _failure_details_path = os.path.join(
        _current_run_dir or os.path.join(_RESULT_BASE_DIR, "misc"),
        "failure_details.json",
    )
    with open(_failure_details_path, "w", encoding="utf-8") as _f:
        json.dump(global_failure_details, _f, ensure_ascii=False, indent=2)

    if global_total_runs > 0 and global_avg_seconds > 0:
        _generate_time_breakdown_chart(
            avg_seconds=global_avg_seconds,
            total_runs=global_total_runs,
            script_stats=script_stats,
            lang_code=TEST_LANGUAGE,
            lang_display=_LANG_DISPLAY_NAMES.get(TEST_LANGUAGE, TEST_LANGUAGE),
        )

    print(f"{'='*70}")


if __name__ == "__main__":
    run_tests()
