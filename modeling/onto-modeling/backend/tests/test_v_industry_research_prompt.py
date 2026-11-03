"""资料分析子代理要用上已启用的行业包（V 阶段：启用「零售连锁」后真实模型的资料分析没读 standards.json、concepts.json；
行业包的用法只写在工作区规则里，派出去的资料分析子代理按自己的 prompt 走，没看到）。"""

from __future__ import annotations

from pathlib import Path

from app import config


def test_research_agent_prompt_reads_enabled_industry_packs():
    text = (Path(config.ASSETS_DIR) / "workspace-toolkit" / "agents" / "research-agent.md").read_text(encoding="utf-8")
    assert ".claude/industry/" in text
    assert "standards.json" in text and "concepts.json" in text
    # 缺的规范类型进报告的「缺口与待决」，用到的行业包写进「信息来源」
    assert "缺口与待决" in text and "信息来源" in text
