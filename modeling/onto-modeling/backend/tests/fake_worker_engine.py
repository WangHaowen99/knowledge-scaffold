"""跑在 Jiuwen worker 子进程里的假引擎（tests/test_jiuwen_worker.py 用）：按消息原文决定产出什么。"""

from __future__ import annotations

import asyncio
import os


class FakeWorkerEngine:
    name = "jiuwen"

    def __init__(self, ctx):
        self.ctx = ctx
        self.queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> bool:
        if self.ctx.project_id == "refuse":
            raise RuntimeError("合成的连接失败")
        return bool(self.ctx.load_resume_token())

    async def send(self, text: str, context: str | None = None) -> None:
        asyncio.get_running_loop().create_task(self._turn(text))

    async def _turn(self, text: str) -> None:
        if text == "崩溃":
            os._exit(3)
        if text == "提问":
            decision = await self.ctx.request_permission("AskUserQuestion", {"questions": []}, None, None)
            await self.queue.put({"type": "assistant_message", "blocks": [{"kind": "text", "text": decision["behavior"]}]})
        elif text.startswith("注入："):
            # 宿主注入在 worker 里算（同 JiuwenEngine）：回出这条消息要附上的包提示规则
            from app.services.session_host import injection

            hint = injection.prompt_hint(self.ctx.cwd, text[len("注入："):])
            await self.queue.put({"type": "assistant_message", "blocks": [{"kind": "text", "text": hint}]})
        else:
            history = len(self.ctx.load_history() or [])
            await self.queue.put({"type": "assistant_message",
                                  "blocks": [{"kind": "text", "text": f"{self.ctx.project_id}:{text}:{history}:{os.getpid()}"}]})
        self.ctx.save_resume_token(f"jiuwen:{self.ctx.project_id}")
        self.ctx.log_error("fake", "合成留档")
        await self.queue.put({"type": "result", "subtype": "success", "is_error": False})

    async def events(self):
        while True:
            event = await self.queue.get()
            yield event
            if event.get("type") == "_engine_end":
                return

    async def interrupt(self) -> None:
        await self.queue.put({"type": "result", "subtype": "interrupted", "is_error": False})

    async def disconnect(self) -> None:
        pass

    async def kill(self) -> None:
        pass

    def context_limits(self):
        return 1000, 800
