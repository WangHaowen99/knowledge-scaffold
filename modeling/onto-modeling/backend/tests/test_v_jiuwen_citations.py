"""V 阶段真实模型发现：出处角标约定只写在 INTERACTION_CONVENTIONS.md 里。

助手不一定读，真实模型一个项目 96 条对话消息没有一处 `[^n]`，
界面上就没有角标、悬停看不到原文。
"""

from __future__ import annotations

import re

from tests.test_jiuwen_assets import make_loader


def test_system_prompt_states_the_citation_footnote_format():
    prompt = make_loader().system_prompt()
    assert "[^1]" in prompt
    assert re.search(r"\[\^1\]: resource/", prompt)
