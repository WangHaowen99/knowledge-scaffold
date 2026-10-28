"""Ask DA-java a question over a kb-service knowledge base and print the
tool calls and the answer (SSE from /api/agents/run-stream).

    python3 deploy/compose/da/smoke.py "润滑油系统在什么条件下会联锁停机？"
    python3 deploy/compose/da/smoke.py "冷却塔风机电流上限？" --tools kb_search

Reads keys from ~/.config/kb-suite (secrets.env, keys.env); uses the first
knowledge base unless --kb is given.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.request

DA_URL = "http://127.0.0.1:5197"
KBS_URL = "http://127.0.0.1:5198"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
logger = logging.getLogger("smoke")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    root = os.path.expanduser(os.environ.get("KBS_SECRETS_DIR", "~/.config/kb-suite"))
    for name in ("secrets.env", "keys.env"):
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        for line in lines:
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env[key] = value
    return env


def first_kb(env: dict[str, str]) -> str:
    req = urllib.request.Request(f"{KBS_URL}/v1/kbs", headers={"Authorization": "Bearer " + env["KBS_PLUGIN_KEY"]})
    return json.load(OPENER.open(req))["items"][0]["id"]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")  # NOSONAR(S4792)
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--kb")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--tools", help="comma separated toolsOverride")
    args = parser.parse_args()
    env = load_env()
    kb = args.kb or first_kb(env)
    session = f"{args.tenant}:smoke-{int(time.time())}"
    body = {"sessionId": session, "input": args.question, "scope": {"kbIds": [kb]}, "maxTurns": 12}
    if args.tools:
        body["toolsOverride"] = args.tools.split(",")
    req = urllib.request.Request(
        f"{DA_URL}/api/agents/run-stream",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Engine-Secret": env["DA_ENGINE_SECRET"],
            "X-Da-Tenant-Id": args.tenant,
            "X-Da-Session-Id": session,
        },
        method="POST",
    )
    event, answer = None, []
    for raw in OPENER.open(req, timeout=900):
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        data = json.loads(line[5:].strip() or "{}")
        if event == "content_delta":
            answer.append(data.get("delta", ""))
        elif event == "tool_call":
            logger.info("[tool] %s %s", data.get("toolName"), json.dumps(data.get("input"), ensure_ascii=False)[:160])
        elif event == "tool_result":
            logger.info("   -> %s", str(data.get("output"))[:160])
        elif event in ("done", "error"):
            logger.info("[%s] %s", event, json.dumps(data, ensure_ascii=False)[:200])
            break
    logger.info("\n%s", "".join(answer)[-2000:])


if __name__ == "__main__":
    main()
