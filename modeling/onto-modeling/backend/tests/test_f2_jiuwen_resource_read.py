"""内测引擎的文件工具只读访问助手副本里的资料链接（F2 第 9 条）。

真实模型抽查：资料分析一开始助手用 read_file 读 `resource/` 就报「Path … outside sandbox」，改用 bash 才读到。
助手副本里的 `resource` 是指向主线资料区的链接，openjiuwen 的文件沙箱按 resolve 之后的路径判定，链接目标在
沙箱三根之外。现在：

- 读文件、列目录、找文件、Grep 能读到这一条链接指向的主线资料区；
- 写照旧按沙箱三根判定，越界即拒；主线工作区里资料区以外的路径、资料区里指向别处的链接都不放；
- 副本顶层的 `resource` 被换成指向别处的链接时，不放。

用 openjiuwen 的真实文件操作与工具（不起模型）；测试数据全部为合成内容。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytest.importorskip("openjiuwen")

from openjiuwen.core.common.exception.codes import StatusCode  # noqa: E402
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperationCard  # noqa: E402
from openjiuwen.core.sys_operation.cwd import set_cwd  # noqa: E402
from openjiuwen.core.sys_operation.sys_operation import SysOperation  # noqa: E402

from app.kernel.changesets import worktree  # noqa: E402
from app.services.engines import jiuwen_sandbox  # noqa: E402

OK = StatusCode.SUCCESS.code


@pytest.fixture()
def layout(tmp_path):
    data = tmp_path / "data"
    main = data / "workspaces" / "p1"
    (main / "resource" / "others").mkdir(parents=True)
    (main / "resource" / "others" / "合成制度.md").write_text("# 合成制度\n补货阈值 1.2\n", encoding="utf-8")
    (main / "ontology").mkdir()
    (main / "ontology" / "主线对象.md").write_text("# 主线对象\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "别处.md").write_text("别处\n", encoding="utf-8")
    os.symlink(outside, main / "resource" / "others" / "escape")
    copy = worktree.copy_path(main)
    copy.mkdir(parents=True)
    (copy / "ontology").mkdir()
    os.symlink(os.path.relpath(main / "resource", copy), copy / "resource")
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    card = SysOperationCard(id="f2-sandbox", mode=OperationMode.LOCAL, work_config=LocalWorkConfig(
        shell_allowlist=None, restrict_to_sandbox=True, sandbox_root=[str(copy), str(shadow)]))
    jiuwen_sandbox.install()
    set_cwd(str(copy))
    return SysOperation(card), main, copy, outside


def test_file_tools_read_the_resource_link_target_but_nothing_else(layout):
    op, main, copy, outside = layout
    fs = op.fs()

    async def scenario():
        read = await fs.read_file("resource/others/合成制度.md")
        assert read.code == OK, read.message
        assert "补货阈值" in read.data.content
        # 工具先 resolve 再交给文件操作：主线资料区的绝对路径同样能读
        absolute = await fs.read_file(str((main / "resource" / "others" / "合成制度.md").resolve()))
        assert absolute.code == OK, absolute.message
        listed = await fs.list_files("resource/others")
        assert listed.code == OK and "合成制度.md" in [item.name for item in listed.data.list_items]
        found = await fs.search_files("resource", "*.md")
        assert found.code == OK and any(item.name == "合成制度.md" for item in found.data.matching_files)
        # 写照旧不行，文件也没写出来
        wrote = await fs.write_file("resource/others/新增.md", "x")
        assert wrote.code != OK
        assert not (main / "resource" / "others" / "新增.md").exists()
        # 主线工作区里资料区以外的、资料区里指向别处的链接：都不放
        assert (await fs.read_file(str(main / "ontology" / "主线对象.md"))).code != OK
        assert (await fs.read_file("resource/others/escape/别处.md")).code != OK
        assert (await fs.read_file(str(outside / "别处.md"))).code != OK

    asyncio.run(scenario())


def test_grep_reads_the_resource_link_target(layout):
    from openjiuwen.harness.tools.filesystem import GrepTool

    op, _main, _copy, _outside = layout
    tool = GrepTool(op)

    async def scenario():
        hit = await tool.invoke({"pattern": "补货阈值", "path": "resource", "output_mode": "files_with_matches"})
        assert hit.success, hit.error
        assert "合成制度.md" in str(hit.data)

    asyncio.run(scenario())


def test_a_repointed_resource_link_is_not_trusted(layout):
    op, _main, copy, outside = layout
    (copy / "resource").unlink()
    os.symlink(outside, copy / "resource")

    async def scenario():
        assert (await op.fs().read_file("resource/别处.md")).code != OK

    asyncio.run(scenario())
