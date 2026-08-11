"""
run_acp_full_matrix.py — ACP 全场景自动测试矩阵

自动执行 ACP 协议的 31 个测试项：
  - 前置检查:  test_acp_full.py --group api,error  (ACP 端点结构测试)
  - 核心矩阵:  test_benchmark_suite.py 的 28 个场景（10 种条件 × 3 种子模式 + 1 Agentic 基线）
  - 后置检查:  test_acp_full.py --group flow       (ACP 完整流程测试)

无需手动修改 .env 文件或前端操作。

使用方法：
    python run_acp_full_matrix.py                    # 执行全部 31 个测试项
    python run_acp_full_matrix.py --quick             # 快捷模式 (1 问题/场景)
    python run_acp_full_matrix.py --list              # 仅列出所有场景
    python run_acp_full_matrix.py --ids T1,T2a,T2b    # 仅运行指定 benchmark 场景（跳过结构测试）
    python run_acp_full_matrix.py --skip-agentic      # 跳过 T1 Agentic 基线
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(_SCRIPT_DIR, "test_benchmark_suite.py")
ACP_FULL_SCRIPT_PATH = os.path.join(_SCRIPT_DIR, "test_acp_full.py")
_RESULT_BASE_DIR = os.path.join(_SCRIPT_DIR, "result")


def _load_dotenv() -> dict[str, str | None]:
    env_map: dict[str, str | None] = {}
    dotenv = os.path.join(_SCRIPT_DIR, ".env")
    if os.path.exists(dotenv):
        with open(dotenv, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env_map[k.strip()] = v.strip().strip("'\"")
    return env_map


_dotenv = _load_dotenv()


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key) or _dotenv.get(key) or default


BASE_URL = _get_env("BASE_URL", "http://localhost:5055")
AUTH_USERNAME = _get_env("AUTH_USERNAME")
AUTH_PASSWORD = _get_env("AUTH_PASSWORD")
WORKSPACE_ID = _get_env("WORKSPACE_ID", "default")
EVAL_SAMPLE_SIZE_INPUT = int(_get_env("ACP_MATRIX_SAMPLE_SIZE", "1"))
RUNS_PER_QUESTION_INPUT = int(_get_env("ACP_MATRIX_RUNS", "1"))


# ── Auth & Runtime Config ──

_token: str | None = None
_session: Any | None = None


def _ensure_session():
    global _session, _token
    if _session is not None:
        return _session
    if not AUTH_USERNAME or not AUTH_PASSWORD:
        raise RuntimeError("AUTH_USERNAME and AUTH_PASSWORD must be set for ACP matrix login")
    import requests
    _session = requests.Session()
    resp = _session.post(
        f"{BASE_URL}/auth/login",
        json={"username": AUTH_USERNAME, "password": AUTH_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    _token = resp.json()["token"]
    _session.headers.update({
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_token}",
    })
    return _session


def _set_runtime_config(config: dict) -> bool:
    s = _ensure_session()
    resp = s.patch(
        f"{BASE_URL}/agentic/workspaces/{WORKSPACE_ID}/runtime-config",
        json=config,
        timeout=10,
    )
    return resp.status_code == 200


def _get_runtime_config() -> dict:
    s = _ensure_session()
    resp = s.get(
        f"{BASE_URL}/agentic/workspaces/{WORKSPACE_ID}/runtime-config",
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json()
    return {}


# ── Scenario Definition ──
# (id, name, api_mode, submode, lang_enabled, acp_lang, suppress_patterns, test_language)

SCENARIOS: list[tuple[str, str, str, str, bool, str, str, str]] = [
    # ── T1: Agentic 基线 ──
    ("T1", "Agentic基线", "agentic", "", False, "", "", "zh"),

    # ── T2: ACP 基线 × 3 子模式 ──
    ("T2a", "ACP基线-execute_stream", "acp", "execute_stream", False, "", "", "zh"),
    ("T2b", "ACP基线-stream", "acp", "stream", False, "", "", "zh"),
    ("T2c", "ACP基线-execute", "acp", "execute", False, "", "", "zh"),

    # ── T3: ACP + 中文 × 3 ──
    ("T3a", "ACP+中文-execute_stream", "acp", "execute_stream", True, "zh", "", "zh"),
    ("T3b", "ACP+中文-stream", "acp", "stream", True, "zh", "", "zh"),
    ("T3c", "ACP+中文-execute", "acp", "execute", True, "zh", "", "zh"),

    # ── T4: ACP + 英文 × 3 ──
    ("T4a", "ACP+英文-execute_stream", "acp", "execute_stream", True, "en", "", "en"),
    ("T4b", "ACP+英文-stream", "acp", "stream", True, "en", "", "en"),
    ("T4c", "ACP+英文-execute", "acp", "execute", True, "en", "", "en"),

    # ── T5: ACP + 日文 × 3 ──
    ("T5a", "ACP+日文-execute_stream", "acp", "execute_stream", True, "ja", "", "ja"),
    ("T5b", "ACP+日文-stream", "acp", "stream", True, "ja", "", "ja"),
    ("T5c", "ACP+日文-execute", "acp", "execute", True, "ja", "", "ja"),

    # ── T6: ACP + 韩文 × 3 ──
    ("T6a", "ACP+韩文-execute_stream", "acp", "execute_stream", True, "ko", "", "ko"),
    ("T6b", "ACP+韩文-stream", "acp", "stream", True, "ko", "", "ko"),
    ("T6c", "ACP+韩文-execute", "acp", "execute", True, "ko", "", "ko"),

    # ── T7: 抑制工具名 × 3 ──
    ("T7a", "抑制工具名-execute_stream", "acp", "execute_stream", False, "", "business_data_search", "zh"),
    ("T7b", "抑制工具名-stream", "acp", "stream", False, "", "business_data_search", "zh"),
    ("T7c", "抑制工具名-execute", "acp", "execute", False, "", "business_data_search", "zh"),

    # ── T8: 抑制操作名 × 3 ──
    ("T8a", "抑制操作名-execute_stream", "acp", "execute_stream", False, "", "故障历史", "zh"),
    ("T8b", "抑制操作名-stream", "acp", "stream", False, "", "故障历史", "zh"),
    ("T8c", "抑制操作名-execute", "acp", "execute", False, "", "故障历史", "zh"),

    # ── T9: 抑制多个 × 3 ──
    ("T9a", "抑制多个-execute_stream", "acp", "execute_stream", False, "", "business_data_search,故障历史", "zh"),
    ("T9b", "抑制多个-stream", "acp", "stream", False, "", "business_data_search,故障历史", "zh"),
    ("T9c", "抑制多个-execute", "acp", "execute", False, "", "business_data_search,故障历史", "zh"),

    # ── T10: 语言 + 抑制 × 3 ──
    ("T10a", "语言+抑制-execute_stream", "acp", "execute_stream", True, "en", "故障历史", "en"),
    ("T10b", "语言+抑制-stream", "acp", "stream", True, "en", "故障历史", "en"),
    ("T10c", "语言+抑制-execute", "acp", "execute", True, "en", "故障历史", "en"),
]


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m{seconds % 60:.0f}s"
    hours = minutes / 60
    return f"{hours:.0f}h{minutes % 60:.0f}m"


def _setup_scenario_config(scenario: tuple) -> bool:
    sid, name, api_mode, submode, lang_enabled, acp_lang, suppress, test_lang = scenario
    config: dict[str, Any] = {}
    config["lang"] = {"enabled": lang_enabled, "default": acp_lang or "zh"}
    if api_mode == "acp" and suppress:
        config["acp_suppress_script_patterns"] = suppress
    ok = _set_runtime_config(config)
    if ok:
        lang_label = "ON" if lang_enabled else "OFF"
        print(f"  ⚙  runtime_config: lang={lang_label}, suppress='{suppress}'")
    else:
        print(f"  ⚠  runtime_config 设置失败!")
    time.sleep(0.3)
    return ok


def _build_env(scenario: tuple, result_dir: str) -> dict:
    sid, name, api_mode, submode, lang_enabled, acp_lang, suppress, test_lang = scenario
    env = os.environ.copy()
    env["API_MODE"] = api_mode
    if api_mode == "acp":
        env["ACP_SUBMODE"] = submode
    if acp_lang:
        env["ACP_LANG"] = acp_lang
    if suppress:
        env["ACP_SUPPRESS_SCRIPT_PATTERNS"] = suppress
    env["TEST_LANGUAGE"] = test_lang
    env["BENCHMARK_LANG"] = test_lang
    env["EVAL_SAMPLE_SIZE"] = str(EVAL_SAMPLE_SIZE_INPUT)
    env["RUNS_PER_QUESTION"] = str(RUNS_PER_QUESTION_INPUT)
    env["BENCHMARK_RESULT_DIR"] = result_dir
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if test_lang not in ("zh", "zh-cn"):
        env["CHINESE_DETECT_MIN_SEVERITY"] = "info"
    return env


def _run_single_scenario(scenario: tuple, result_dir: str) -> dict:
    sid, name, api_mode, submode, lang_enabled, acp_lang, suppress, test_lang = scenario
    label = f"[{sid}] {name}"

    print(f"\n{'='*80}")
    print(f"  >>> {label}")
    if api_mode == "acp":
        le = "ON" if lang_enabled else "OFF"
        print(f"      模式: ACP/{submode} | 语言跟随: {le} | "
              f"ACP_LANG={acp_lang or '-'} | 抑制: {suppress or '-'}")
    else:
        print(f"      模式: Agentic")
    print(f"{'='*80}\n")

    # 设置服务端 runtime_config
    _setup_scenario_config(scenario)

    # 创建结果子目录
    scenario_dir = os.path.join(result_dir, sid)
    os.makedirs(scenario_dir, exist_ok=True)

    # 构建环境变量
    env = _build_env(scenario, scenario_dir)

    # 启动子进程
    start_ts = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-u", SCRIPT_PATH],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )

    output_lines: list[str] = []

    def _on_line(line: str):
        stripped = line.rstrip("\n")
        if not stripped:
            return
        m = re.match(r"(问题\d+) 完成:", stripped)
        if m:
            print(f"    ✅ {stripped}")
            return
        if "开始测试" in stripped:
            print(f"    ▶ {stripped}")
            return
        if "全局成功率" in stripped or "总问题数" in stripped or "全局平均耗时" in stripped:
            print(f"    📊 {stripped}")
            return

    for line in iter(proc.stdout.readline, ""):
        output_lines.append(line)
        _on_line(line)

    proc.stdout.close()
    proc.wait()
    wall_seconds = time.perf_counter() - start_ts

    combined = "".join(output_lines)

    # 保存日志
    log_path = os.path.join(scenario_dir, f"{sid}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"{'='*80}\n{label}\n{'='*80}\n\n{combined}")
    print(f"    📝 日志: {log_path}")

    # 解析关键指标
    result = _parse_result(combined, sid, name, wall_seconds)
    result["returncode"] = proc.returncode
    _print_result_summary(result)

    return result


def _parse_result(text: str, sid: str, name: str, wall_seconds: float) -> dict:
    result: dict[str, Any] = {
        "sid": sid,
        "name": name,
        "wall_seconds": round(wall_seconds, 2),
        "total_questions": None,
        "total_runs": None,
        "avg_seconds": None,
        "success_rate": None,
        "success_count": None,
        "chinese_intrusion": None,
        "acp_suppress_check": None,
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
    m = re.search(r"全局成功率:\s*(\d+)/(\d+)\s*\(([\d.]+)%\)", text)
    if m:
        result["success_count"] = int(m.group(1))
        result["success_runs"] = int(m.group(2))
        result["success_rate"] = float(m.group(3))
    ci_match = re.search(
        r"--- 6、中文混入检测.*?\n(.*?)(?=\n---|\n={10,}|$)",
        text, re.DOTALL,
    )
    if ci_match:
        result["chinese_intrusion"] = ci_match.group(1).strip()
    as_match = re.search(
        r"ACP 输出过滤验证.*?\n(.*?)(?=\n---|\n={10,}|$)",
        text, re.DOTALL,
    )
    if as_match:
        result["acp_suppress_check"] = as_match.group(1).strip()
    return result


def _print_result_summary(r: dict):
    sr = f"{r['success_rate']:.1f}%" if r.get("success_rate") is not None else "N/A"
    avg = f"{r['avg_seconds']:.2f}s" if r.get("avg_seconds") is not None else "N/A"
    qs = r.get("total_questions", "?")
    runs = r.get("total_runs", "?")
    print(f"\n    📊 [{r['sid']}] {r['name']}: {qs}题×{runs}次 | 平均{avg} | 成功率{sr} | 耗时{r['wall_seconds']:.0f}s")
    if r.get("chinese_intrusion"):
        for line in r["chinese_intrusion"].split("\n")[:3]:
            print(f"         {line.strip()}")
    if r.get("acp_suppress_check"):
        for line in r["acp_suppress_check"].split("\n")[:3]:
            print(f"         {line.strip()}")


def _run_acp_structural_check(group_flag: str, sid: str, name: str, result_dir: str) -> dict:
    """运行 test_acp_full.py 的指定测试组，返回结构化结果。"""
    label = f"[{sid}] {name}"

    print(f"\n{'='*80}")
    print(f"  >>> {label}")
    print(f"      test_acp_full.py --group {group_flag}")
    print(f"{'='*80}\n")

    start_ts = time.perf_counter()
    sub_env = os.environ.copy()
    sub_env["PYTHONIOENCODING"] = "utf-8"
    sub_env["PYTHONUNBUFFERED"] = "1"
    sub_env["ACP_SKIP_EXECUTE"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-u", ACP_FULL_SCRIPT_PATH, "--group", group_flag],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=sub_env,
    )

    output_lines: list[str] = []

    for line in iter(proc.stdout.readline, ""):
        stripped = line.rstrip("\n")
        output_lines.append(stripped)
        # 精简控制台输出：只显示关键行
        if any(kw in stripped for kw in ["✓", "✗", "⊘", "测试结果汇总", "总计:", "全部通过", "有"]):
            print(f"    {stripped}")

    proc.stdout.close()
    proc.wait()
    wall_seconds = time.perf_counter() - start_ts

    stderr_output = ""
    try:
        stderr_output = proc.stderr.read()
    except Exception:
        pass

    combined = "\n".join(output_lines)
    if stderr_output.strip():
        combined += f"\n--- STDERR ---\n{stderr_output}"

    # 保存日志
    log_path = os.path.join(result_dir, f"{sid}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"{'='*80}\n{label}\n{'='*80}\n\n{combined}")
    print(f"    📝 日志: {log_path}")

    # 解析关键指标
    result: dict[str, Any] = {
        "sid": sid,
        "name": name,
        "wall_seconds": round(wall_seconds, 2),
        "total_questions": None,
        "total_runs": None,
        "avg_seconds": None,
        "success_rate": None,
        "success_count": None,
        "chinese_intrusion": None,
        "acp_suppress_check": None,
        "returncode": proc.returncode,
    }

    # 从输出中提取: 总计: 16    通过: 16    失败: 0    跳过: 0
    m = re.search(r"总计:\s*(\d+)\s+通过:\s*(\d+)\s+失败:\s*(\d+)\s+跳过:\s*(\d+)", combined)
    if m:
        total = int(m.group(1))
        passed = int(m.group(2))
        failed = int(m.group(3))
        skipped = int(m.group(4))
        result["total_questions"] = total
        result["total_runs"] = total
        result["success_count"] = passed
        effective = total - skipped
        if effective > 0:
            result["success_rate"] = round(passed / effective * 100, 1)
        result["avg_seconds"] = round(wall_seconds / total, 2) if total > 0 else None

    status_icon = "✅" if (result.get("success_rate") or 0) >= 100 else "⚠️"
    print(f"\n    {status_icon} [{sid}] {name}: "
          f"{result.get('total_questions', '?')}用例 | "
          f"成功率{result.get('success_rate', 'N/A')}% | "
          f"耗时{result['wall_seconds']:.0f}s")

    return result


def _generate_summary_report(all_results: list[dict], run_dir: str, total_wall: float):
    report_path = os.path.join(run_dir, "full_matrix_report.md")

    lines: list[str] = []
    a = lines.append
    a("# ACP 全场景测试矩阵 汇总报告")
    a(f"")
    a(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    a(f"> 工作空间: {WORKSPACE_ID} | 后端: {BASE_URL}")
    a(f"> ACP_MATRIX_SAMPLE_SIZE={EVAL_SAMPLE_SIZE_INPUT} | ACP_MATRIX_RUNS={RUNS_PER_QUESTION_INPUT}")
    a(f"")
    a(f"| 指标 | 值 |")
    a(f"|------|-----|")
    a(f"| 场景总数 | {len(all_results)} |")
    a(f"| 总墙钟时间 | {_fmt_duration(total_wall)} |")
    a(f"")

    # ── 结构测试结果 (P1/P2/P3) ──
    structural = [r for r in all_results if r["sid"].startswith("P")]
    if structural:
        a(f"## 协议结构验证")
        a(f"")
        a(f"| ID | 测试项 | 用例数 | 成功率 | 耗时 |")
        a(f"|---:|------|:-----:|:-----:|:----:|")
        for r in structural:
            sr = f"{r['success_rate']:.1f}%" if r.get("success_rate") is not None else "N/A"
            icon = "✅" if (r.get("success_rate") or 0) >= 100 else "⚠️"
            a(f"| {icon} {r['sid']} | {r['name']} | {r.get('total_questions','?')} | {sr} | {r['wall_seconds']:.0f}s |")
        a(f"")

    a(f"## 各场景结果")
    a(f"")
    a(f"| ID | 场景 | 模式 | 语言跟随 | 语言 | 抑制 | 问题数 | 执行数 | 平均耗时 | 成功率 |")
    a(f"|---:|------|:----:|:--------:|:----:|:----:|:-----:|:------:|:--------:|:-----:|")
    for r in all_results:
        sid = r["sid"]
        if sid.startswith("P"):
            continue  # 结构测试已单独展示
        nr = next((s for s in SCENARIOS if s[0] == sid), None)
        mode = f"ACP/{nr[3]}" if nr and nr[2] == "acp" else "Agentic"
        lang_f = "ON" if nr and nr[4] else "OFF"
        lang = nr[5] or "-" if nr else "-"
        suppress = nr[6] or "-" if nr else "-"
        avg = f"{r['avg_seconds']:.2f}s" if r.get("avg_seconds") is not None else "N/A"
        sr = f"{r['success_rate']:.1f}%" if r.get("success_rate") is not None else "N/A"
        a(f"| {sid} | {r['name']} | {mode} | {lang_f} | {lang} | {suppress} | "
          f"{r.get('total_questions','?')} | {r.get('total_runs','?')} | {avg} | {sr} |")
    a(f"")

    # 失败场景（benchmark 场景）
    benchmark_results = [r for r in all_results if not r["sid"].startswith("P")]
    failed = [r for r in benchmark_results if r.get("success_rate") is not None and r["success_rate"] < 100]
    if failed:
        a(f"## 异常场景")
        a(f"")
        a(f"以下 benchmark 场景成功率低于 100%：")
        a(f"")
        for r in failed:
            a(f"- **[{r['sid']}] {r['name']}**: 成功率 {r['success_rate']:.1f}%")
        a(f"")

    # 结构测试失败
    structural_failed = [r for r in structural if r.get("success_rate") is not None and r["success_rate"] < 100]
    if structural_failed:
        if not failed:
            a(f"## 异常场景")
            a(f"")
        for r in structural_failed:
            a(f"- **[{r['sid']}] {r['name']}**: 成功率 {r['success_rate']:.1f}%")
        a(f"")

    # 中文混入检测摘要
    lang_scenarios = [r for r in all_results if r.get("chinese_intrusion")]
    if lang_scenarios:
        a(f"## 中文混入检测摘要")
        a(f"")
        for r in lang_scenarios:
            a(f"### [{r['sid']}] {r['name']}")
            a(f"```")
            a(r["chinese_intrusion"][:300])
            a(f"```")
            a(f"")

    # 抑制验证摘要
    suppress_scenarios = [r for r in all_results if r.get("acp_suppress_check")]
    if suppress_scenarios:
        a(f"## 输出抑制验证摘要")
        a(f"")
        for r in suppress_scenarios:
            a(f"### [{r['sid']}] {r['name']}")
            a(f"```")
            a(r["acp_suppress_check"][:300])
            a(f"```")
            a(f"")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n  📄 汇总报告: {report_path}")

    # 控制台输出
    print(f"\n{'='*80}")
    print(f"  全场景测试完成")
    print(f"{'='*80}")

    # 结构测试结果
    structural = [r for r in all_results if r["sid"].startswith("P")]
    for r in structural:
        sr = f"{r['success_rate']:.1f}%" if r.get("success_rate") is not None else "N/A"
        icon = "✅" if (r.get("success_rate") or 0) >= 100 else "⚠️"
        print(f"  {icon} [{r['sid']}] {r['name']}: {r.get('total_questions','?')}用例 | 成功率{sr}")

    # benchmark 结果
    benchmark_results = [r for r in all_results if not r["sid"].startswith("P")]
    b_success = sum(1 for r in benchmark_results if r.get("success_rate") is not None and r["success_rate"] >= 100)
    b_total = len(benchmark_results)
    print(f"  📊 benchmark 场景: {b_success}/{b_total} 通过")

    failed = [r for r in benchmark_results if r.get("success_rate") is not None and r["success_rate"] < 100]
    structural_failed = [r for r in structural if r.get("success_rate") is not None and r["success_rate"] < 100]
    all_failed = failed + structural_failed

    print(f"  总耗时: {_fmt_duration(total_wall)}")
    print(f"  报告: {report_path}")
    print(f"  日志: {run_dir}")
    if all_failed:
        print(f"  ⚠️  {len(all_failed)} 项异常:")
        for r in all_failed:
            print(f"     - [{r['sid']}] {r['name']}: 成功率 {r['success_rate']:.1f}%")
    else:
        print(f"  ✅ 全部场景成功率 100%")
    print(f"{'='*80}\n")


def _list_scenarios(scenarios: list):
    print(f"\nACP 全场景矩阵 (35 个测试项):\n")

    print(f"  ┌── 前置检查: ACP 协议结构测试 ──")
    print(f"  │ P1  ACP-API端点验证       test_acp_full.py --group api       11 用例")
    print(f"  │ P2  ACP-错误场景验证      test_acp_full.py --group error     9 用例")
    print(f"  │ P2b ACP-用户隔离验证      test_acp_full.py --group isolation 4 用例")
    print(f"  └" + "─" * 50)

    print(f"\n  ┌── 核心矩阵: benchmark 场景 ({len(scenarios)} 个) ──")
    print(f"{'ID':<6} {'名称':<28} {'模式':<20} {'语言跟随':<10} {'语言':<6} {'抑制':<30}")
    print(f"{'-'*100}")
    for s in scenarios:
        sid, name, api_mode, submode, lang_en, lang, suppress, test_lang = s
        mode = f"ACP/{submode}" if api_mode == "acp" else "Agentic"
        le = "ON" if lang_en else "OFF"
        lang_d = lang or "-"
        sup_d = suppress or "-"
        print(f"{sid:<6} {name:<28} {mode:<20} {le:<10} {lang_d:<6} {sup_d:<30}")
    print(f"  └" + "─" * 100)

    print(f"\n  ┌── 后置检查: ACP 完整流程测试 ──")
    print(f"  │ P3  ACP-完整流程验证      test_acp_full.py --group flow   6 用例")
    print(f"  └" + "─" * 50)

    print(f"\n总计: 35 个测试项 (4 结构测试 + {len(scenarios)} benchmark 场景)")
    print(f"预计时间: ~{len(scenarios) * 60 + 120}s ({_fmt_duration(len(scenarios) * 60 + 120)})\n")


def main():
    parser = argparse.ArgumentParser(description="ACP 全场景自动测试矩阵")
    parser.add_argument("--quick", action="store_true", help="快捷模式 (EVAL_SAMPLE_SIZE=1)")
    parser.add_argument("--list", action="store_true", help="仅列出场景")
    parser.add_argument("--ids", type=str, default="",
                        help="仅运行指定benchmark场景ID，逗号分隔，不执行结构测试 (如 T1,T2a,T2b)")
    parser.add_argument("--skip-agentic", action="store_true",
                        help="跳过 T1 Agentic 基线")
    args = parser.parse_args()

    if args.quick:
        global EVAL_SAMPLE_SIZE_INPUT
        EVAL_SAMPLE_SIZE_INPUT = 1

    # 过滤场景
    scenarios = list(SCENARIOS)
    if args.skip_agentic:
        scenarios = [s for s in scenarios if s[0] != "T1"]
    if args.ids:
        allowed = set(x.strip() for x in args.ids.split(",") if x.strip())
        scenarios = [s for s in scenarios if s[0] in allowed]
        if not scenarios:
            print(f"没有匹配的场景 (ids={args.ids})")
            sys.exit(1)

    if args.list:
        _list_scenarios(scenarios)
        return

    # 创建运行目录
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(_RESULT_BASE_DIR, f"full_matrix_{ts}")
    os.makedirs(run_dir, exist_ok=True)

    # 保存原始 runtime_config
    print(f"\n保存原始 runtime_config ...")
    original_config = _get_runtime_config()
    print(f"  原始配置: lang={original_config.get('lang', {})}, "
          f"suppress='{original_config.get('acp_suppress_script_patterns', '')}'")

    all_results: list[dict] = []
    total_start = time.perf_counter()

    run_structural = not args.ids  # --ids 时跳过结构测试

    if run_structural:
        # ── 前置检查: ACP 协议端点结构测试 (api + error) ──
        precheck_dir = os.path.join(run_dir, "_precheck")
        os.makedirs(precheck_dir, exist_ok=True)
        print(f"\n{'#'*80}")
        print(f"  # 前置检查: ACP 端点结构测试")
        print(f"{'#'*80}\n")

        p1 = _run_acp_structural_check("api", "P1", "ACP-API端点验证", precheck_dir)
        all_results.append(p1)

        p2 = _run_acp_structural_check("error", "P2", "ACP-错误场景验证", precheck_dir)
        all_results.append(p2)

        p2b = _run_acp_structural_check("isolation", "P2b", "ACP-用户隔离验证", precheck_dir)
        all_results.append(p2b)

    # ── 运行 benchmark 场景 ──
    structural_offset = 3 if run_structural else 0
    try:
        for scenario in scenarios:
            sid = scenario[0]
            result = _run_single_scenario(scenario, run_dir)
            all_results.append(result)

            elapsed = time.perf_counter() - total_start
            bench_done = len(all_results) - structural_offset
            bench_total = len(scenarios)
            remaining = bench_total - bench_done
            if remaining > 0 and bench_done > 0:
                avg_per = elapsed / bench_done
                eta = remaining * avg_per
                print(f"\n  📊 进度: {bench_done}/{bench_total} | "
                      f"已用: {_fmt_duration(elapsed)} | "
                      f"预计剩余: {_fmt_duration(eta)}\n")

    except KeyboardInterrupt:
        total_expected = len(scenarios) + structural_offset + (1 if run_structural else 0)
        print(f"\n\n用户中断，已执行 {len(all_results)}/{total_expected} 个场景")

    if run_structural:
        # ── 后置检查: ACP 完整流程测试 (flow) ──
        postcheck_dir = os.path.join(run_dir, "_postcheck")
        os.makedirs(postcheck_dir, exist_ok=True)
        print(f"\n{'#'*80}")
        print(f"  # 后置检查: ACP 完整流程测试")
        print(f"{'#'*80}\n")

        p3 = _run_acp_structural_check("flow", "P3", "ACP-完整流程验证", postcheck_dir)
        all_results.append(p3)

    # 恢复原始 runtime_config
    print(f"\n恢复原始 runtime_config ...")
    if original_config:
        _set_runtime_config(original_config)
        print(f"  已恢复")

    total_wall = time.perf_counter() - total_start
    _generate_summary_report(all_results, run_dir, total_wall)


if __name__ == "__main__":
    main()
