from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(_SCRIPT_DIR, "test_benchmark_suite.py")
_RESULT_BASE_DIR = os.path.join(_SCRIPT_DIR, "result")

_LANG_DISPLAY = {
    "zh": "中文",
    "en": "英文",
    "ja": "日文",
    "ko": "韩文",
    "ru": "俄文",
    "de": "德文",
    "fr": "法文",
    "es": "西班牙文",
    "pt": "葡萄牙文",
    "ar": "阿拉伯文",
    "it": "意大利文",
    "nl": "荷兰文",
    "pl": "波兰文",
    "tr": "土耳其文",
    "vi": "越南文",
    "th": "泰文",
    "hi": "印地文",
    "id": "印尼文",
    "uk": "乌克兰文",
    "cs": "捷克文",
}


_ENV_CACHE: dict[str, str | None] | None = None


def _load_env() -> dict[str, str | None]:
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE
    dotenv_path = os.path.join(_SCRIPT_DIR, ".env")
    env_map: dict[str, str | None] = {}
    if os.path.exists(dotenv_path):
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env_map[k.strip()] = v.strip().strip("'\"")
    _ENV_CACHE = env_map
    return env_map


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key) or _load_env().get(key) or default


def _resolve_languages() -> list[tuple[str, str]]:
    raw = _get_env("MULTILANG_LANGUAGES", "zh,en,ja,ko")
    codes = [c.strip() for c in raw.split(",") if c.strip()]
    if codes == ["all"]:
        cases_dir = os.path.join(_SCRIPT_DIR, "translated_cases")
        codes = sorted(
            d
            for d in os.listdir(cases_dir)
            if os.path.isdir(os.path.join(cases_dir, d))
            and os.path.isfile(os.path.join(cases_dir, d, f"cases_{d}.json"))
        )
        if "zh" not in codes:
            codes.insert(0, "zh")
    result = []
    for code in codes:
        name = _LANG_DISPLAY.get(code, code)
        result.append((code, name))
    return result


LANGUAGES = _resolve_languages()

_run_dir: str | None = None
_report_path: str | None = None

_all_results: list[dict] = []


def _init_run_dir() -> str:
    global _run_dir, _report_path
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _run_dir = os.path.join(_RESULT_BASE_DIR, f"multilang_{ts}")
    os.makedirs(os.path.join(_run_dir, "logs"), exist_ok=True)
    _report_path = os.path.join(_run_dir, "report.md")
    return _run_dir


def _run_benchmark(lang_code: str, lang_name: str) -> dict:
    print(f"\n{'='*80}")
    print(f"  >>> 开始执行: {lang_name} ({lang_code})")
    print(f"{'='*80}\n")

    lang_result_dir = os.path.join(_run_dir or _RESULT_BASE_DIR, lang_code)
    os.makedirs(lang_result_dir, exist_ok=True)

    env = os.environ.copy()
    env["BENCHMARK_LANG"] = lang_code
    env["BENCHMARK_RESULT_DIR"] = lang_result_dir
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    start_ts = time.time()

    proc = subprocess.Popen(
        [sys.executable, "-u", SCRIPT_PATH],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )

    print(f"  🚀 子进程已启动 (PID: {proc.pid})，等待输出...\n")
    sys.stdout.flush()

    output_lines: list[str] = []
    questions_seen: set[str] = set()
    questions_completed: set[str] = set()
    question_start_times: dict[str, float] = {}

    _ERROR_KEYWORDS = ("❌", "执行异常", "连接失败", "超时", "Traceback", "Error:", "ERROR")

    def _on_line(line: str):
        stripped = line.rstrip("\n")
        if not stripped:
            return

        m = re.match(r"开始测试:\s*(问题\d+)", stripped)
        if m:
            q = m.group(1)
            questions_seen.add(q)
            question_start_times[q] = time.time()
            done = len(questions_completed)
            total_seen = len(questions_seen)
            print(f"  ▶ [{q}] 开始测试... ({done}/{total_seen} 完成)")
            sys.stdout.flush()
            return

        m = re.match(r"(问题\d+) 完成:\s*平均耗时\s*(\S+),\s*成功\s*(\d+)/(\d+)", stripped)
        if m:
            q = m.group(1)
            questions_completed.add(q)
            q_elapsed = time.time() - question_start_times.get(q, start_ts)
            done = len(questions_completed)
            total_seen = len(questions_seen)
            avg_per_q = (time.time() - start_ts) / done if done > 0 else 0
            remaining = max(0, total_seen - done)
            eta = avg_per_q * remaining if remaining > 0 else 0
            eta_str = _fmt_duration(eta) if remaining > 0 else "—"
            print(
                f"  ✅ [{q}] 完成 | 平均耗时: {m.group(2)} | 成功: {m.group(3)}/{m.group(4)} | "
                f"耗时: {_fmt_duration(q_elapsed)} | "
                f"进度: {done}/{total_seen} | 预计剩余: {eta_str}"
            )
            sys.stdout.flush()
            return

        if "并行执行模式" in stripped:
            stripped_clean = stripped.lstrip(" ⚡")
            print(f"  ⚡ {stripped_clean}")
            sys.stdout.flush()
            return

        if any(kw in stripped for kw in _ERROR_KEYWORDS):
            print(f"  ⚠ {stripped}")
            sys.stdout.flush()
            return

    stderr_chunks: list[str] = []

    def _drain_stderr():
        try:
            for line in iter(proc.stderr.readline, ""):
                stderr_chunks.append(line)
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    for line in iter(proc.stdout.readline, ""):
        output_lines.append(line)
        _on_line(line)

    proc.stdout.close()
    proc.wait()
    stderr_thread.join(timeout=5)

    stderr = "".join(stderr_chunks)

    wall_seconds = time.time() - start_ts

    combined = "".join(output_lines)
    if stderr.strip():
        combined += f"\n--- STDERR ---\n{stderr}"

    result = _parse_output(combined, lang_code, lang_name, wall_seconds)
    result["returncode"] = proc.returncode

    _save_lang_log(lang_code, lang_name, combined, result)

    return result


def _parse_output(text: str, lang_code: str, lang_name: str, wall_seconds: float) -> dict:
    result = {
        "lang_code": lang_code,
        "lang_name": lang_name,
        "wall_seconds": round(wall_seconds, 2),
        "total_questions": None,
        "total_runs": None,
        "avg_seconds": None,
        "avg_tokens": None,
        "success_rate": None,
        "success_count": None,
        "first4_avg_seconds": None,
        "rest_avg_seconds": None,
        "avg_first_token_latency": None,
        "avg_inter_token_latency": None,
        "kg_tool_stats": None,
        "mindsdb_tool_stats": None,
        "wos_tool_stats": None,
        "flywheel_wos_check": None,
        "chinese_intrusion": None,
        "kg_chinese_detection": None,
        "kg_lang_param_detection": None,
        "kg_output_lang_detection": None,
        "per_question_summary": [],
    }

    m = re.search(r"总问题数:\s*(\d+)", text)
    if m:
        result["total_questions"] = int(m.group(1))

    m = re.search(r"总执行次数:\s*(\d+)", text)
    if m:
        result["total_runs"] = int(m.group(1))

    m = re.search(r"全局平均耗时:\s*\S+\s*\(([\d.]+)s\)", text)
    if m:
        result["avg_seconds"] = float(m.group(1))

    m = re.search(r"全局平均 Tokens:\s*(\d+)", text)
    if m:
        result["avg_tokens"] = int(m.group(1))

    m = re.search(r"全局成功率:\s*(\d+)/(\d+)\s*\(([\d.]+)%\)", text)
    if m:
        result["success_count"] = int(m.group(1))
        result["success_runs"] = int(m.group(2))
        result["success_rate"] = float(m.group(3))

    m = re.search(r"前4个问题.*?平均执行时间:\s*\S+\s*\(([\d.]+)s\)", text, re.DOTALL)
    if m:
        result["first4_avg_seconds"] = float(m.group(1))

    m = re.search(r"后面所有问题.*?平均执行时间:\s*\S+\s*\(([\d.]+)s\)", text, re.DOTALL)
    if m:
        result["rest_avg_seconds"] = float(m.group(1))

    m = re.search(r"平均首Token时延:\s*([\d.]+)s", text)
    if m:
        result["avg_first_token_latency"] = float(m.group(1))

    m = re.search(r"平均增量Token时延:\s*([\d.]+)s", text)
    if m:
        result["avg_inter_token_latency"] = float(m.group(1))

    kg_match = re.search(
        r"knowledge_graph_tool\.py 执行统计.*?\n(.*?)(?=\n---|\n={10,}|$)",
        text,
        re.DOTALL,
    )
    if kg_match:
        result["kg_tool_stats"] = kg_match.group(1).strip()

    mindsdb_match = re.search(
        r"mindsdb_tool\.py 执行统计.*?\n(.*?)(?=\n---|\n={10,}|$)",
        text,
        re.DOTALL,
    )
    if mindsdb_match:
        result["mindsdb_tool_stats"] = mindsdb_match.group(1).strip()

    wos_match = re.search(
        r"work_order_sync\.py 执行统计.*?\n(.*?)(?=\n---|\n={10,}|$)",
        text,
        re.DOTALL,
    )
    if wos_match:
        result["wos_tool_stats"] = wos_match.group(1).strip()

    flywheel_match = re.search(
        r"飞轮问题 work_order_sync\.py 覆盖检测.*?\n(.*?)(?=\n---|\n={10,}|$)",
        text,
        re.DOTALL,
    )
    if flywheel_match:
        result["flywheel_wos_check"] = flywheel_match.group(1).strip()

    chinese_section = re.search(
        r"中文混入检测.*?\n(.*?)(?=\n---|\n={10,}|$)",
        text,
        re.DOTALL,
    )
    if chinese_section:
        result["chinese_intrusion"] = chinese_section.group(1).strip()

    kg_chinese = re.search(
        r"knowledge_graph_tool 返回中文检测.*?\n(.*?)(?=\n---|\n={10,}|$)",
        text,
        re.DOTALL,
    )
    if kg_chinese:
        result["kg_chinese_detection"] = kg_chinese.group(1).strip()

    kg_lang_param = re.search(
        r"knowledge_graph_tool language参数检测.*?\n(.*?)(?=\n---|\n={10,}|$)",
        text,
        re.DOTALL,
    )
    if kg_lang_param:
        result["kg_lang_param_detection"] = kg_lang_param.group(1).strip()

    kg_output_lang = re.search(
        r"knowledge_graph_tool 输出语言匹配检测.*?\n(.*?)(?=\n---|\n={10,}|$)",
        text,
        re.DOTALL,
    )
    if kg_output_lang:
        result["kg_output_lang_detection"] = kg_output_lang.group(1).strip()

    q_blocks = re.findall(
        r"开始测试:\s*(问题\d+)\s*\[(.+?)\]",
        text,
    )
    q_completions = re.findall(
        r"(问题\d+) 完成:\s*平均耗时\s*(\S+),\s*成功\s*(\d+)/(\d+)",
        text,
    )
    completion_map = {q: (dur, int(s), int(t)) for q, dur, s, t in q_completions}

    for sheet, lang_info in q_blocks:
        if sheet in completion_map:
            dur, succ, total = completion_map[sheet]
            result["per_question_summary"].append(
                {
                    "sheet": sheet,
                    "lang_info": lang_info,
                    "avg_duration": dur,
                    "success": succ,
                    "total": total,
                }
            )

    return result


def _save_lang_log(lang_code: str, lang_name: str, output: str, result: dict):
    log_dir = os.path.join(_run_dir or _RESULT_BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"benchmark_{lang_code}_{ts}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"{'='*80}\n")
        f.write(f"语言: {lang_name} ({lang_code})\n")
        f.write(f"开始时间: {ts}\n")
        f.write(f"返回码: {result.get('returncode', '?')}\n")
        f.write(f"{'='*80}\n\n")
        f.write(output)
    print(f"  日志已保存: {log_path}")


def _print_lang_summary(r: dict):
    print(f"\n{'='*80}")
    print(f"  {r['lang_name']} ({r['lang_code']}) 统计摘要")
    print(f"{'='*80}")
    print(f"  总问题数:       {r.get('total_questions', 'N/A')}")
    print(f"  总执行次数:     {r.get('total_runs', 'N/A')}")
    print(f"  墙钟总耗时:     {r.get('wall_seconds', 'N/A')}s")
    print(f"  全局平均耗时:   {r.get('avg_seconds', 'N/A')}s")
    print(f"  全局平均Tokens: {r.get('avg_tokens', 'N/A')}")
    if r.get("success_rate") is not None:
        print(f"  成功率:         {r['success_count']}/{r.get('success_runs', '?')} ({r['success_rate']}%)")
    if r.get("first4_avg_seconds") is not None:
        print(f"  前4问题平均:    {r['first4_avg_seconds']}s")
    if r.get("rest_avg_seconds") is not None:
        print(f"  后续问题平均:   {r['rest_avg_seconds']}s")
    if r.get("avg_first_token_latency") is not None:
        print(f"  平均首Token:    {r['avg_first_token_latency']}s")
    if r.get("avg_inter_token_latency") is not None:
        print(f"  平均增量Token:  {r['avg_inter_token_latency']}s")

    if r.get("per_question_summary"):
        print(f"\n  {'问题':<8} {'语言':<16} {'平均耗时':<10} {'成功率':<10}")
        print(f"  {'-'*44}")
        for q in r["per_question_summary"]:
            rate = f"{q['success']}/{q['total']}"
            print(f"  {q['sheet']:<8} {q['lang_info']:<16} {q['avg_duration']:<10} {rate:<10}")

    if r.get("kg_tool_stats"):
        print(f"\n  KG工具统计:")
        for line in r["kg_tool_stats"].split("\n"):
            print(f"    {line}")

    if r.get("chinese_intrusion"):
        print(f"\n  中文混入检测:")
        for line in r["chinese_intrusion"].split("\n")[:15]:
            print(f"    {line}")

    if r.get("kg_lang_param_detection"):
        print(f"\n  KG工具language参数检测:")
        for line in r["kg_lang_param_detection"].split("\n")[:10]:
            print(f"    {line}")

    if r.get("kg_output_lang_detection"):
        print(f"\n  KG工具输出语言匹配检测:")
        for line in r["kg_output_lang_detection"].split("\n")[:10]:
            print(f"    {line}")

    print(f"{'='*80}")


def _print_final_report(results: list[dict]):
    total_wall = sum(r["wall_seconds"] for r in results)
    total_runs = sum(r.get("total_runs") or 0 for r in results)
    total_questions_across = len(results)

    def _read_env_config_from_md() -> str:
        if not _run_dir:
            return ""
        for r in results:
            lang_dir = os.path.join(_run_dir, r["lang_code"])
            if not os.path.isdir(lang_dir):
                continue
            md_files = sorted(glob.glob(os.path.join(lang_dir, "benchmark_*.md")))
            if not md_files:
                continue
            try:
                with open(md_files[-1], "r", encoding="utf-8") as f:
                    content = f.read()
                m = re.search(
                    r"(## 环境配置\s*\n.*?---\s*\n)",
                    content,
                    re.DOTALL,
                )
                if m:
                    return m.group(1).rstrip()
                m = re.search(
                    r"(## 环境配置\s*\n.*?)(?=##\s全局统计)",
                    content,
                    re.DOTALL,
                )
                if m:
                    return m.group(1).rstrip()
            except Exception:
                pass
            break
        return ""

    report_lines: list[str] = []
    a = report_lines.append

    a("# 多语言基准测试 汇总报告")
    a(f"")
    a(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    a(f"")
    a(f"| 指标 | 值 |")
    a(f"|------|-----|")
    a(f"| 测试语言 | {', '.join(f'{n}({c})' for c, n in LANGUAGES)} |")
    a(f"| 总轮次 | {total_questions_across} 轮 |")
    a(f"| 总执行数 | {total_runs} 次 |")
    a(f"| 总墙钟时间 | {_fmt_duration(total_wall)} |")
    a(f"")

    env_config = _read_env_config_from_md()
    if env_config:
        a(env_config)
        a(f"")

    total_success = sum(r.get("success_count") or 0 for r in results)
    total_success_runs = sum(r.get("success_runs") or 0 for r in results)
    avg_seconds_vals = [r["avg_seconds"] for r in results if r.get("avg_seconds") is not None]
    avg_tokens_vals = [r["avg_tokens"] for r in results if r.get("avg_tokens") is not None]
    ftl_vals = [r["avg_first_token_latency"] for r in results if r.get("avg_first_token_latency") is not None]
    aitl_vals = [r["avg_inter_token_latency"] for r in results if r.get("avg_inter_token_latency") is not None]

    a(f"## 全局统计")
    a(f"")
    a(f"| 指标 | 值 |")
    a(f"|------|-----|")
    a(f"| 总问题数(跨语言) | {sum(r.get('total_questions') or 0 for r in results)} |")
    a(f"| 总执行次数 | {total_runs} |")
    a(
        f"| 总成功率 | {total_success}/{total_success_runs} ({total_success/total_success_runs*100:.1f}%) |"
        if total_success_runs
        else f"| 总成功率 | N/A |"
    )
    a(
        f"| 跨语言平均耗时 | {sum(avg_seconds_vals)/len(avg_seconds_vals):.2f}s |"
        if avg_seconds_vals
        else f"| 跨语言平均耗时 | N/A |"
    )
    a(
        f"| 跨语言平均 Tokens | {sum(avg_tokens_vals)/len(avg_tokens_vals):.0f} |"
        if avg_tokens_vals
        else f"| 跨语言平均 Tokens | N/A |"
    )
    a(
        f"| 跨语言平均首Token时延 | {sum(ftl_vals)/len(ftl_vals):.3f}s |"
        if ftl_vals
        else f"| 跨语言平均首Token时延 | N/A |"
    )
    a(
        f"| 跨语言平均增量Token时延 | {sum(aitl_vals)/len(aitl_vals):.3f}s |"
        if aitl_vals
        else f"| 跨语言平均增量Token时延 | N/A |"
    )
    a(f"")

    a(f"---")
    a(f"")
    a(f"## 一、各语言核心指标对比")
    a(f"")
    a(f"| 语言 | 平均耗时(s) | 平均 Tokens | 成功率 | 首 Token(s) | 增量 Token(s) |")
    a(f"|------|------------|-------------|--------|-------------|---------------|")
    for r in results:
        lang_label = f"{r['lang_name']}({r['lang_code']})"
        avg_s = f"{r['avg_seconds']:.2f}" if r.get("avg_seconds") is not None else "N/A"
        avg_t = f"{r['avg_tokens']}" if r.get("avg_tokens") is not None else "N/A"
        sr = f"{r['success_rate']:.1f}%" if r.get("success_rate") is not None else "N/A"
        ftl = f"{r['avg_first_token_latency']:.3f}" if r.get("avg_first_token_latency") is not None else "N/A"
        aitl = f"{r['avg_inter_token_latency']:.3f}" if r.get("avg_inter_token_latency") is not None else "N/A"
        a(f"| {lang_label} | {avg_s} | {avg_t} | {sr} | {ftl} | {aitl} |")
    a(f"")

    a(f"## 二、各语言分问题详细统计")
    a(f"")
    for r in results:
        a(f"### {r['lang_name']} ({r['lang_code']})")
        a(f"")
        if r.get("per_question_summary"):
            a(f"| 问题 | 语言信息 | 平均耗时 | 成功 |")
            a(f"|------|----------|----------|------|")
            for q in r["per_question_summary"]:
                rate = f"{q['success']}/{q['total']}"
                a(f"| {q['sheet']} | {q['lang_info']} | {q['avg_duration']} | {rate} |")
        else:
            a(f"(无分问题数据)")
        a(f"")

    a(f"## 三、前4问题 vs 后续问题对比")
    a(f"")
    a(f"| 语言 | 前4问题平均(s) | 后续问题平均(s) |")
    a(f"|------|---------------|----------------|")
    for r in results:
        lang_label = f"{r['lang_name']}({r['lang_code']})"
        f4 = f"{r['first4_avg_seconds']:.2f}" if r.get("first4_avg_seconds") is not None else "N/A"
        rest = f"{r['rest_avg_seconds']:.2f}" if r.get("rest_avg_seconds") is not None else "N/A"
        a(f"| {lang_label} | {f4} | {rest} |")
    a(f"")

    a(f"## 四、中文混入检测（非中文语言）")
    a(f"")
    for r in results:
        if r["lang_code"] in ("zh", "zh-cn"):
            continue
        a(f"### {r['lang_name']} ({r['lang_code']})")
        a(f"")
        if r.get("chinese_intrusion"):
            for line in r["chinese_intrusion"].split("\n")[:20]:
                a(line)
        else:
            a(f"(无检测数据)")
        a(f"")

    a(f"## 五、工具执行统计对比")
    a(f"")

    def _parse_tool_stats_block(text: str | None):
        if not text:
            return None
        m = re.search(r"排除失败:\s*(\d+)\s*次,\s*平均:\s*([\d.]+)s,\s*最慢:\s*([\d.]+)s", text)
        if not m:
            return None
        return {"count": int(m.group(1)), "avg": float(m.group(2)), "max": float(m.group(3))}

    tool_keys = [
        ("knowledge_graph_tool.py", "kg_tool_stats"),
        ("mindsdb_tool.py", "mindsdb_tool_stats"),
        ("work_order_sync.py", "wos_tool_stats"),
    ]
    a(f"### 跨语言汇总")
    a(f"")
    a(f"| 工具 | 总执行 | 平均 | 最慢 | 语言数 |")
    a(f"|------|--------|------|------|--------|")
    for tool_name, stats_key in tool_keys:
        parsed_list = []
        for r in results:
            p = _parse_tool_stats_block(r.get(stats_key))
            if p:
                parsed_list.append(p)
        if parsed_list:
            total_count = sum(p["count"] for p in parsed_list)
            weighted_avg = sum(p["count"] * p["avg"] for p in parsed_list) / total_count if total_count else 0
            overall_max = max(p["max"] for p in parsed_list)
            a(f"| {tool_name} | {total_count} | {weighted_avg:.2f}s | {overall_max:.2f}s | {len(parsed_list)} |")
    a(f"")

    for r in results:
        a(f"### {r['lang_name']} ({r['lang_code']})")
        a(f"")
        if r.get("kg_tool_stats"):
            for line in r["kg_tool_stats"].split("\n"):
                a(line)
        else:
            a(f"KG工具: 无数据")
        if r.get("mindsdb_tool_stats"):
            for line in r["mindsdb_tool_stats"].split("\n"):
                a(line)
        else:
            a(f"MindsDB工具: 无数据")
        if r.get("wos_tool_stats"):
            for line in r["wos_tool_stats"].split("\n"):
                a(line)
        else:
            a(f"work_order_sync: 无数据")
        if r.get("flywheel_wos_check"):
            a(f"飞轮覆盖检测:")
            for line in r["flywheel_wos_check"].split("\n"):
                a(line)
        a(f"")

    a(f"## 六、knowledge_graph_tool language参数检测")
    a(f"")
    has_kg_lang_param = False
    for r in results:
        if r.get("kg_lang_param_detection"):
            has_kg_lang_param = True
            a(f"### {r['lang_name']} ({r['lang_code']})")
            a(f"")
            for line in r["kg_lang_param_detection"].split("\n")[:20]:
                a(line)
            a(f"")
    if not has_kg_lang_param:
        a(f"(未捕获到 KG 工具输入参数数据)")
        a(f"")

    a(f"## 七、knowledge_graph_tool 输出语言匹配检测")
    a(f"")
    has_kg_output_lang = False
    for r in results:
        if r.get("kg_output_lang_detection"):
            has_kg_output_lang = True
            a(f"### {r['lang_name']} ({r['lang_code']})")
            a(f"")
            for line in r["kg_output_lang_detection"].split("\n"):
                a(line)
            a(f"")
    if not has_kg_output_lang:
        a(f"(未捕获到 KG 工具输出语言匹配数据)")
        a(f"")

    a(f"## 八、各语言墙钟耗时")
    a(f"")
    a(f"| 语言 | 墙钟耗时 |")
    a(f"|------|----------|")
    for r in results:
        lang_label = f"{r['lang_name']}({r['lang_code']})"
        a(f"| {lang_label} | {_fmt_duration(r['wall_seconds'])} ({r['wall_seconds']:.1f}s) |")
    a(f"| **总计** | **{_fmt_duration(total_wall)} ({total_wall:.1f}s)** |")
    a(f"")

    report_text = "\n".join(report_lines)

    print(f"\n\n{report_text}")

    save_path = _report_path or os.path.join(
        _RESULT_BASE_DIR, f"multilang_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    )
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"\n汇总报告已保存: {save_path}")

    _generate_multilang_charts(results, os.path.dirname(save_path))


def _generate_multilang_charts(results: list[dict], output_dir: str):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print(f"  ⚠ matplotlib 未安装，跳过图表生成")
        return

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    def _parse_tool_stats(text: str | None):
        if not text:
            return None
        m = re.search(r"排除失败:\s*(\d+)\s*次,\s*平均:\s*([\d.]+)s,\s*最慢:\s*([\d.]+)s", text)
        if not m:
            return None
        return {"count": int(m.group(1)), "avg": float(m.group(2)), "max": float(m.group(3))}

    tool_keys = [
        ("kg_tool_stats", "KG 工具", "#DD8452"),
        ("mindsdb_tool_stats", "MindsDB", "#55A868"),
        ("wos_tool_stats", "WOS", "#C44E52"),
    ]

    langs = []
    total_avgs = []
    llm_list = []
    tool_lists = {k: [] for k, _, _ in tool_keys}

    for r in results:
        avg_s = r.get("avg_seconds")
        total_runs = r.get("total_runs") or 0
        if avg_s is None or total_runs == 0:
            continue

        lang_label = f"{r['lang_name']}({r['lang_code']})"
        langs.append(lang_label)
        total_avgs.append(avg_s)

        tool_per_run = 0
        for stats_key, _, _ in tool_keys:
            p = _parse_tool_stats(r.get(stats_key))
            val = p["count"] * p["avg"] / total_runs if p else 0
            tool_lists[stats_key].append(val)
            tool_per_run += val

        llm_list.append(avg_s - tool_per_run)

    if not langs:
        return

    x = np.arange(len(langs))
    w = 0.55

    fig, (ax_bar, ax_sum) = plt.subplots(1, 2, figsize=(18, 7), gridspec_kw={"width_ratios": [3, 2]})

    segments = [
        (llm_list, "LLM 时间", "#4C72B0"),
    ]
    for stats_key, tool_label, color in tool_keys:
        segments.append((tool_lists[stats_key], tool_label, color))

    bottoms = np.zeros(len(langs))
    for vals, label, color in segments:
        vals_arr = np.array(vals)
        ax_bar.bar(x, vals_arr, w, bottom=bottoms, label=label, color=color, edgecolor="white", linewidth=0.5)
        for i in range(len(langs)):
            v = vals_arr[i]
            pct = v / total_avgs[i] * 100
            mid = bottoms[i] + v / 2
            if v > 1.5:
                ax_bar.text(
                    x[i],
                    mid,
                    f"{v:.1f}s\n({pct:.0f}%)",
                    ha="center",
                    va="center",
                    fontsize=7.5,
                    color="white",
                    fontweight="bold",
                )
            elif v > 0.3:
                ax_bar.text(
                    x[i], mid, f"{v:.1f}s", ha="center", va="center", fontsize=7, color="white", fontweight="bold"
                )
        bottoms = bottoms + vals_arr

    for i in range(len(langs)):
        ax_bar.text(
            x[i],
            total_avgs[i] + 0.8,
            f"总计 {total_avgs[i]:.1f}s",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
        )

    ax_bar.set_ylabel("时间 (秒)", fontsize=11)
    ax_bar.set_title("各语言执行时间分解", fontsize=13, fontweight="bold")
    ax_bar.set_xticks(x)
    ax_bar.set_xticklabels(langs, fontsize=11)
    ax_bar.legend(loc="upper left", fontsize=10)
    ax_bar.set_ylim(0, max(total_avgs) * 1.18)
    ax_bar.spines["top"].set_visible(False)
    ax_bar.spines["right"].set_visible(False)

    avg_total = sum(total_avgs) / len(total_avgs)
    avg_llm = sum(llm_list) / len(llm_list)
    avg_tools = {}
    for stats_key, _, _ in tool_keys:
        avg_tools[stats_key] = sum(tool_lists[stats_key]) / len(tool_lists[stats_key])

    labels = ["LLM 时间"] + [tl for _, tl, _ in tool_keys]
    vals = [avg_llm] + [avg_tools[k] for k, _, _ in tool_keys]
    colors = ["#4C72B0"] + [c for _, _, c in tool_keys]

    bars = ax_sum.barh(labels, vals, color=colors)
    for bar, v in zip(bars, vals):
        pct = v / avg_total * 100 if avg_total else 0
        ax_sum.text(
            bar.get_width() + 0.2,
            bar.get_y() + bar.get_height() / 2,
            f"{v:.2f}s ({pct:.0f}%)",
            ha="left",
            va="center",
            fontsize=10,
        )

    ax_sum.set_xlabel("时间 (秒)", fontsize=11)
    ax_sum.set_title(f"所有语言平均 (总耗时 {avg_total:.2f}s)", fontsize=13, fontweight="bold")
    ax_sum.set_xlim(0, avg_total * 1.3)
    ax_sum.spines["top"].set_visible(False)
    ax_sum.spines["right"].set_visible(False)

    plt.tight_layout()
    p1 = os.path.join(output_dir, "time_breakdown.png")
    fig.savefig(p1, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  📊 多语言时间分解图已保存: {p1}")

    tool_names = ["knowledge_graph_tool.py", "mindsdb_tool.py", "work_order_sync.py"]
    all_elapsed = {}
    for sn in tool_names:
        all_elapsed[sn] = []

    if _run_dir:
        for r in results:
            lang_dir = os.path.join(_run_dir, r["lang_code"])
            json_path = os.path.join(lang_dir, "script_call_details.json")
            if not os.path.exists(json_path):
                continue
            import json

            with open(json_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for sn in tool_names:
                items = raw.get(sn, [])
                for d in items:
                    if d.get("status") != "error" and d.get("elapsed_ms"):
                        all_elapsed[sn].append(d["elapsed_ms"] / 1000)

    has_data = any(all_elapsed[sn] for sn in tool_names)
    if not has_data:
        return

    fig2, axes = plt.subplots(1, 3, figsize=(16, 5))
    for idx, (sn, (_, label, color)) in enumerate(zip(tool_names, tool_keys)):
        ax = axes[idx]
        elapsed_s = all_elapsed[sn]

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

    fig2.suptitle("工具调用耗时分布 (所有语言汇总)", fontsize=13, fontweight="bold", y=1.02)
    plt.tight_layout()
    p2 = os.path.join(output_dir, "tool_latency_dist.png")
    fig2.savefig(p2, dpi=150, bbox_inches="tight")
    plt.close(fig2)
    print(f"  📊 工具耗时分布图已保存: {p2}")


def _fmt_duration(seconds: float) -> str:
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    if h > 0:
        return f"{h}h{m}m{s}s"
    return f"{m}m{s}s"


def _find_latest_multilang_run() -> str | None:
    if not os.path.exists(_RESULT_BASE_DIR):
        return None
    candidates = sorted(glob.glob(os.path.join(_RESULT_BASE_DIR, "multilang_*")))
    if not candidates:
        return None
    for c in reversed(candidates):
        log_dir = os.path.join(c, "logs")
        if os.path.isdir(log_dir):
            return c
    return None


def _replay_from_logs() -> list[dict]:
    run_dir = _find_latest_multilang_run()
    if not run_dir:
        print(f"  ⚠ 未找到任何多语言测试日志 (result/multilang_*)")
        return []

    log_dir = os.path.join(run_dir, "logs")
    print(f"  从目录恢复: {run_dir}")

    results = []
    for lang_code, lang_name in LANGUAGES:
        pattern = f"benchmark_{lang_code}_*.log"
        matches = sorted(glob.glob(os.path.join(log_dir, pattern)))
        if not matches:
            print(f"  ⚠ 未找到 {lang_name} ({lang_code}) 的日志文件")
            continue
        latest_log = matches[-1]
        print(f"  读取日志: {latest_log}")
        with open(latest_log, "r", encoding="utf-8") as f:
            text = f.read()
        result = _parse_output(text, lang_code, lang_name, 0)
        result["returncode"] = 0
        wall_match = re.search(r"墙钟总耗时:\s*([\d.]+)s", text)
        if wall_match:
            result["wall_seconds"] = float(wall_match.group(1))
        total_runs = result.get("total_runs") or 0
        avg_seconds = result.get("avg_seconds")
        if total_runs > 0 and avg_seconds is not None and result["wall_seconds"] == 0:
            result["wall_seconds"] = round(total_runs * avg_seconds, 1)
        results.append(result)

    global _run_dir, _report_path
    _run_dir = run_dir
    _report_path = os.path.join(run_dir, "report.md")

    return results


def main():
    if "--replay" in sys.argv:
        print(f"\n{'#'*80}")
        print(f"  多语言基准测试 — 从日志重新生成报告")
        print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#'*80}")
        results = _replay_from_logs()
        if not results:
            return
        for r in results:
            _print_lang_summary(r)
        _print_final_report(results)
        return

    run_dir = _init_run_dir()

    print(f"\n{'#'*80}")
    print(f"  多语言基准测试 — 自动循环执行")
    print(f"  语言序列: {' -> '.join(f'{n}({c})' for c, n in LANGUAGES)}")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  结果目录: {run_dir}")
    print(f"{'#'*80}")

    overall_start = time.time()
    lang_times: list[float] = []

    for idx, (lang_code, lang_name) in enumerate(LANGUAGES, 1):
        lang_start = time.time()
        print(f"\n{'*'*80}")
        print(f"  [{idx}/{len(LANGUAGES)}] 正在执行: {lang_name} ({lang_code})")
        print(f"{'*'*80}")

        r = _run_benchmark(lang_code, lang_name)
        _all_results.append(r)

        lang_elapsed = time.time() - lang_start
        lang_times.append(lang_elapsed)

        _print_lang_summary(r)

        if r.get("returncode", 0) != 0:
            print(f"  ⚠️ 警告: {lang_name} ({lang_code}) 执行返回码非0: {r['returncode']}")

        remaining = len(LANGUAGES) - idx
        if remaining > 0 and lang_times:
            avg_lang = sum(lang_times) / len(lang_times)
            eta = avg_lang * remaining
            print(
                f"\n  📊 整体进度: {idx}/{len(LANGUAGES)} 语言完成 | "
                f"已用: {_fmt_duration(time.time() - overall_start)} | "
                f"预计剩余: {_fmt_duration(eta)}"
            )

    overall_elapsed = time.time() - overall_start
    print(f"\n{'#'*80}")
    print(f"  全部 {len(LANGUAGES)} 轮执行完毕，总耗时: {_fmt_duration(overall_elapsed)}")
    print(f"  结果目录: {run_dir}")
    print(f"{'#'*80}")

    _print_final_report(_all_results)


if __name__ == "__main__":
    main()
