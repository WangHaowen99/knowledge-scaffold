---
description: 从会议纪要提取术语，写成术语表
argument-hint: <纪要文件路径>
---

# 整理纪要术语

纪要路径：$ARGUMENTS（为空时先问用户要整理哪份纪要）。

1. 运行 `python3 .claude/scripts/sample_notes_terms.py "<纪要路径>"`，拿到候选术语和出现次数。
2. 读 `.claude/agents/sample-notes-agent.md`，作为子代理（Task）的 prompt 派出，把纪要路径和候选术语一起交给它。
3. 子代理写完后，告诉用户收录了几条术语、哪几条释义待确认，并给出 `ext/sample-notes/glossary.md` 的路径。
