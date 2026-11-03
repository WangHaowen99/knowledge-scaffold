Version management for non-technical users. All output in Chinese. Subcommand: $ARGUMENTS

## Parse subcommand

If $ARGUMENTS is empty or "历史": go to **History**
If $ARGUMENTS starts with "回退"、"对比" or "标记": go to **Rollback / Diff / Tag**

---

## History

版本号由平台在每一轮结束合入时编（版本页上的 v0.N）；这里的提交里还有这一轮没合入的修改，不要按提交数自己编号。
只列最近 10 次修改的说明和时间：

```bash
git log -10 --format='%s%x09%ar'
```

显示为：

```
最近的修改

- {描述}（{相对时间}）
...
```

Where:
- {描述} = commit message with prefix translated:
  - `research:` → "资料分析："
  - `flowchart:` → "流程图："
  - `extract:` → "提取："
  - `fix:` → "修复："
  - `mock:` → "测试数据："
  - `codegen:` → "代码生成："
  - Others → use as-is
- {相对时间} = translate to Chinese (e.g., "2 hours ago" → "2 小时前")

---

## Rollback / Diff / Tag

回退、对比、标记都在版本页做：不在对话里跑 `git revert` / `git reset` / `git checkout` / `git tag`。告诉用户：
"版本页里可以选一个版本对比、回退或加标记。"
