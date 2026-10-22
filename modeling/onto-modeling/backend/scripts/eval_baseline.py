#!/usr/bin/env python3
"""最小评测基线（方案 §4 Phase 3）：Builder 三命令链 + 确定性记分卡。

在部署实例上以指定引擎跑标准链（合成外卖领域）：
  research（仅本地资料）→ extract-object → validate

评分为**确定性记分卡**（产物存在性/结构、检查器结果、收尾完成度、交互约定
遵从），不依赖 LLM 裁判——客观、可重复、零额外成本；内容质量的人工复核
以产物工作区为准（脚本保留评测项目供检查）。

用法（宿主机，凭据经环境变量注入）：
  EVAL_ADMIN_PASSWORD=... python3 eval_baseline.py \
      --base http://127.0.0.1:5100 --engine jiuwen [--keep]

输出：JSON 记分卡（stdout）+ 评测项目 id（--keep 时保留供人工复核）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

MATERIAL_MD = """# 外卖平台业务说明（合成评测数据）

## 业务概述
示例外卖平台连接顾客、商家、骑手三方。顾客在 App 下单，商家接单备餐，平台调度骑手取餐配送，完成后进行结算与评价。

## 核心流程
1. 顾客浏览商家菜单，加购下单，支持在线支付（余额/第三方支付）。
2. 订单创建后进入待接单状态，商家 10 分钟未接单自动取消并全额退款。
3. 商家接单后开始备餐，平台按距离与骑手负载分派配送任务。
4. 骑手到店扫码核验取餐码后开始配送；送达后顾客确认收货。
5. 每日凌晨结算：平台抽佣 18%，配送费归骑手，其余结算给商家。

## 业务规则
- 单笔订单金额上限 500 元；起送价由商家设置。
- 骑手同时最多接 5 单；商家评分低于 4.0 触发流量降权。
"""

MATERIAL_CSV = """字段名,类型,说明
order_id,string,订单唯一编号
customer_id,string,顾客编号
merchant_id,string,商家编号
rider_id,string,骑手编号
order_status,enum,状态：待支付/待接单/备餐中/配送中/已完成/已取消
total_amount,decimal,订单总金额（元）
delivery_fee,decimal,配送费（元）
pay_channel,enum,支付渠道：余额/第三方
created_at,datetime,下单时间
pickup_code,string,取餐码
rating,int,顾客评分 1-5
"""

INLINE_QUESTION_PATTERN = re.compile(r"回复\s*\*\*|请选择|请回复")


class Client:
    def __init__(self, base: str, token: str, relogin=None):
        self.base = base
        self.token = token
        self.relogin = relogin  # 长链会跨过 access token 有效期，401 时重登一次

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in (0, 1):
            req = urllib.request.Request(
                self.base + path,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(body).encode() if body is not None else None,
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode() or "{}")
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0 and self.relogin:
                    self.token = self.relogin()
                    continue
                raise
        return {}


def login(base: str, username: str, password: str) -> str:
    req = urllib.request.Request(
        base + "/api/auth/login",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"username": username, "password": password}).encode(),
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["access_token"]


def upload(base: str, token: str, project: str, subdir: str, name: str, text: str):
    boundary = "----evalboundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"subdir\"\r\n\r\n{subdir}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
        f"Content-Type: text/plain\r\n\r\n{text}\r\n--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        f"{base}/api/projects/{project}/resources",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        data=body,
    )
    urllib.request.urlopen(req, timeout=60).read()


def workspace_exec(project: str, command: str, container: str) -> str:
    """在评测项目工作区内执行命令（记分卡的确定性检查用）。"""
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-c",
         f"cd /data/onto-web/workspaces/{project} && {command}"],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout


def read_events(project: str, container: str) -> list[dict]:
    out = subprocess.run(
        ["docker", "exec", container, "sh", "-c",
         f"cat /data/onto-web/sessions/{project}.jsonl 2>/dev/null"],
        capture_output=True, text=True, timeout=60,
    ).stdout
    events = []
    for line in out.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def drive_command(client: Client, project: str, container: str, text: str,
                  timeout_s: int = 900, nudges: int = 3, done_check=None) -> dict:
    """发送命令并驱动到完成：确认卡自动选推荐项；正文提问计失分并轻推。

    完成判定基于产物（done_check），而非会话 idle——Claude 引擎可能把一条命令
    拆成多轮（中途 idle），idle 仅作为"可继续推进"的信号。"""
    stats = {"cards_answered": 0, "inline_questions": 0, "errors": 0}
    seq_before = len(read_events(project, container))
    client.call("POST", f"/api/projects/{project}/session/messages", {"text": text})
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(12)
        events = read_events(project, container)
        fresh = events[seq_before:]
        resolved = {e.get("request_id") for e in fresh if e.get("type") == "permission_resolved"}
        pending = [e for e in fresh
                   if e.get("type") == "permission_request" and e.get("request_id") not in resolved]
        if pending:
            e = pending[0]
            questions = (e.get("tool_input") or {}).get("questions") or []
            answers = {}
            for q in questions:
                opts = [o.get("label", "") for o in (q.get("options") or [])]
                pick = next((o for o in opts if "推荐" in o), opts[0] if opts else "确认")
                answers[q.get("question", "")] = pick
            try:
                client.call("POST", f"/api/projects/{project}/session/permissions/{e['request_id']}",
                            {"behavior": "allow", "answers": answers})
                stats["cards_answered"] += 1
            except Exception:
                pass
            continue
        state = client.call("GET", f"/api/projects/{project}/session").get("state", "")
        if state == "error":
            stats["errors"] += 1
            break
        if state == "idle":
            # 检查是否停在正文提问（交互约定失分点）
            last_text = ""
            for e in reversed(fresh):
                if e.get("type") == "assistant_message":
                    for b in e.get("blocks", []):
                        if b.get("kind") == "text":
                            last_text = b["text"]
                    break
            if done_check is not None and not done_check():
                if nudges > 0:
                    nudges -= 1
                    seq_before = len(events)
                    if INLINE_QUESTION_PATTERN.search(last_text):
                        stats["inline_questions"] += 1
                        nudge = "按你的推荐选项继续，不要等待；后续问题请用 ask_user 工具提出。"
                    else:
                        nudge = ("命令尚未执行完毕（产物或收尾缺失）。请继续按命令文档完成"
                                 "全部剩余步骤：产物写入、检查、state_helper release、"
                                 "update-progress 与 git 提交。")
                    client.call("POST", f"/api/projects/{project}/session/messages",
                                {"text": nudge})
                    continue
                break
            if INLINE_QUESTION_PATTERN.search(last_text) and nudges > 0:
                stats["inline_questions"] += 1
                nudges -= 1
                seq_before = len(events)
                client.call("POST", f"/api/projects/{project}/session/messages",
                            {"text": "按你的推荐选项继续，不要等待；后续问题请用 ask_user 工具提出。"})
                continue
            break
    stats["session_errors"] = sum(1 for e in read_events(project, container)[seq_before:]
                                  if e.get("type") == "session_error")
    return stats


def entry_count(project: str, container: str, rel: str) -> int:
    out = workspace_exec(project, f"ls {rel} 2>/dev/null", container)
    return len([f for f in out.split() if f.endswith(".md") and f != "index.md"])


def review_status(project: str, container: str) -> dict:
    """资料分析报告的验收回执状态；读不到时按"无回执"处理，不算通过。"""
    out = workspace_exec(
        project, "python3 .claude/scripts/research_checker.py status --json 2>/dev/null", container)
    try:
        data = json.loads(out.strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def score_chain(client: Client, project: str, container: str) -> dict:
    """三命令链 + 逐命令确定性记分。满分 100。"""
    card = {"commands": {}, "total": 0}

    def git_count() -> int:
        return len(workspace_exec(project, "git log --oneline 2>/dev/null", container).splitlines())

    def lock_free() -> bool:
        return not workspace_exec(project, "ls .ontology-state/lock-*.json 2>/dev/null", container).strip()

    def research_done():
        chars = len(workspace_exec(project, "cat research/*.md 2>/dev/null | grep -v '^| 文件' ", container))
        return chars > 1500 and lock_free()

    def extract_done():
        return entry_count(project, container, "ontology/object") >= 4 and lock_free()

    def validate_done():
        return bool(workspace_exec(
            project, "ls reports/*.md 2>/dev/null | grep -v index", container).strip()) and lock_free()

    # ---- research ----
    commits0 = git_count()
    stats = drive_command(client, project, container,
                          "/ontology-research 外卖订单（仅本地资料，不要联网）",
                          done_check=research_done)
    report_chars = len(workspace_exec(project, "cat research/*.md 2>/dev/null", container))
    review = review_status(project, container)
    item = {
        "produced_report": report_chars > 1500,       # 10
        # 内容验收：计数、ID、引用字段对账通过才给分，报告长度不能替代它
        "review_passed": review.get("status") == "passed",  # 10
        "review_reason": review.get("reason", ""),
        "review_errors": len(review.get("errors") or []),
        "review_manual_pending": len(review.get("manual_pending") or []),
        "lock_released": lock_free(),                 # 10
        "committed": git_count() > commits0,          # 5
        "no_inline_question": stats["inline_questions"] == 0,  # 5
        "stats": stats,
    }
    item["score"] = (10 if item["produced_report"] else 0) \
        + (10 if item["review_passed"] else 0) + (10 if item["lock_released"] else 0) \
        + (5 if item["committed"] else 0) + (5 if item["no_inline_question"] else 0)
    card["commands"]["research"] = item

    # ---- extract-object ----
    commits0 = git_count()
    stats = drive_command(client, project, container, "/ontology-extract-object",
                          done_check=extract_done)
    objects = entry_count(project, container, "ontology/object")
    structure_ok = "对象属性清单" in workspace_exec(
        project, "cat ontology/object/*.md 2>/dev/null | head -200", container)
    item = {
        "objects_extracted": objects,
        "enough_objects": objects >= 4,               # 15
        "template_structure": structure_ok,           # 10
        "lock_released": lock_free(),                 # 5
        "committed": git_count() > commits0,          # 5
        "no_inline_question": stats["inline_questions"] == 0,  # 5
        "stats": stats,
    }
    item["score"] = (15 if item["enough_objects"] else 0) + (10 if item["template_structure"] else 0) \
        + (5 if item["lock_released"] else 0) + (5 if item["committed"] else 0) \
        + (5 if item["no_inline_question"] else 0)
    card["commands"]["extract-object"] = item

    # ---- validate ----
    stats = drive_command(client, project, container, "/ontology-validate",
                          done_check=validate_done)
    report_out = workspace_exec(project, "ls reports/*.md 2>/dev/null", container)
    checker_out = workspace_exec(
        project,
        "python3 .claude/scripts/ontology_checker.py ontology 2>&1 | tail -1", container)
    hard_errors = 0
    m = re.search(r"(\d+)\s*errors", checker_out)
    if m:
        # 前向引用（指向未提取阶段的链接失效）不计为硬错误
        detail = workspace_exec(
            project,
            "python3 .claude/scripts/ontology_checker.py ontology 2>/dev/null | grep -c '链接失效' || true",
            container)
        forward = int(detail.strip() or 0)
        hard_errors = max(0, int(m.group(1)) - forward)
    item = {
        "report_generated": bool(report_out.strip()),  # 10
        "no_hard_errors": hard_errors == 0,            # 5
        "lock_released": lock_free(),                  # 5
        "stats": stats,
    }
    item["score"] = (10 if item["report_generated"] else 0) + (5 if item["no_hard_errors"] else 0) \
        + (5 if item["lock_released"] else 0)
    card["commands"]["validate"] = item

    card["total"] = sum(c["score"] for c in card["commands"].values())
    card["max"] = 100
    return card


def rescore(project: str, container: str) -> dict:
    """后验重打分：对既有评测项目的最终工作区状态做确定性检查（不含过程分）。"""
    def lock_free() -> bool:
        return not workspace_exec(project, "ls .ontology-state/lock-*.json 2>/dev/null", container).strip()

    log = workspace_exec(project, "git log --format=%s 2>/dev/null", container)
    report_chars = len(workspace_exec(project, "cat research/*.md 2>/dev/null", container))
    review_passed = review_status(project, container).get("status") == "passed"
    objects = entry_count(project, container, "ontology/object")
    structure_ok = "对象属性清单" in workspace_exec(
        project, "cat ontology/object/*.md 2>/dev/null | head -200", container)
    validate_report = bool(workspace_exec(
        project, "ls reports/*.md 2>/dev/null | grep -v index", container).strip())
    checker_out = workspace_exec(
        project, "python3 .claude/scripts/ontology_checker.py ontology 2>&1 | tail -1", container)
    m = re.search(r"(\d+)\s+issues \((\d+) errors", checker_out) or re.search(r"(\d+)\s*errors", checker_out)
    total_errors = int(m.group(m.lastindex)) if m else 0
    forward = int(workspace_exec(
        project,
        "python3 .claude/scripts/ontology_checker.py ontology 2>/dev/null | grep -c '链接失效' || true",
        container).strip() or 0)
    card = {
        "project": project,
        "research": {
            "produced_report": report_chars > 1500,
            "review_passed": review_passed,
            "committed": "research:" in log,
            "score": (10 if report_chars > 1500 else 0) + (10 if review_passed else 0)
            + (5 if "research:" in log else 0) + (10 if lock_free() else 0),
        },
        "extract-object": {
            "objects_extracted": objects,
            "enough_objects": objects >= 4,
            "template_structure": structure_ok,
            "committed": ("extract:" in log or "objects" in log),
            "score": (15 if objects >= 4 else 0) + (10 if structure_ok else 0)
            + (5 if ("extract:" in log or "objects" in log) else 0) + (5 if lock_free() else 0),
        },
        "validate": {
            "report_generated": validate_report,
            "hard_errors": max(0, total_errors - forward),
            "score": (10 if validate_report else 0) + (5 if max(0, total_errors - forward) == 0 else 0)
            + (5 if lock_free() else 0),
        },
    }
    card["total"] = sum(c["score"] for c in card.values() if isinstance(c, dict) and "score" in c)
    card["max"] = 90  # 过程分（交互约定 5×2）后验不可测
    return card


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5100")
    parser.add_argument("--engine", choices=["claude", "jiuwen"], default="")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--container", default="onto-modeling")
    parser.add_argument("--keep", action="store_true", help="保留评测项目供人工复核")
    parser.add_argument("--rescore", default="", help="仅对既有项目后验重打分")
    parser.add_argument("--only", default="", help="仅驱动指定命令（research|extract-object|validate）")
    args = parser.parse_args()

    if args.rescore:
        print(json.dumps(rescore(args.rescore, args.container), ensure_ascii=False, indent=2))
        return 0
    if not args.engine:
        print("--engine 必填（或用 --rescore）", file=sys.stderr)
        return 2

    password = os.environ.get("EVAL_ADMIN_PASSWORD", "")
    if not password:
        print("EVAL_ADMIN_PASSWORD 未设置", file=sys.stderr)
        return 2

    token = login(args.base, args.username, password)
    client = Client(args.base, token,
                    relogin=lambda: login(args.base, args.username, password))
    project = client.call(
        "POST", "/api/projects",
        {"name": f"评测-{args.engine}-{time.strftime('%m%d%H%M')}"})["id"]
    print(f"# eval project: {project} (engine={args.engine})", file=sys.stderr)
    upload(args.base, token, project, "others", "外卖平台业务说明.md", MATERIAL_MD)
    upload(args.base, token, project, "excel", "订单字段表.csv", MATERIAL_CSV)
    client.call("PUT", f"/api/projects/{project}/engine", {"engine": args.engine})

    started = time.time()
    card = score_chain(client, project, args.container)
    card["engine"] = args.engine
    card["project"] = project
    card["wall_seconds"] = int(time.time() - started)
    print(json.dumps(card, ensure_ascii=False, indent=2))

    if not args.keep:
        client.call("DELETE", f"/api/projects/{project}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
