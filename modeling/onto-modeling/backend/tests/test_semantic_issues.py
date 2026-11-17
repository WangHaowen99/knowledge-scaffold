"""语义检查边查边记（semantic_issues.py）：子任务每查完一项记问题、标做完，主命令查缺项、补派、合并。

起因：语义检查子任务一次思考里把全部检查项过一遍、再起草整份 JSON，撞上输出上限被截断，查出的问题全丢。
拆成三部分派发、逐项落盘以后，一次思考只覆盖一项；中途出错也只补派缺的项。数据全部合成。
"""

from __future__ import annotations

import importlib
import io
import json
import re
from pathlib import Path

import pytest
import yaml

from app import config
from app.kernel_guards import _guard_common as guards

TOOLKIT = Path(config.ASSETS_DIR) / "workspace-toolkit"
SCRIPTS = TOOLKIT / "scripts"
SPEC = TOOLKIT / "agents" / "semantic-check-agent.md"
VALIDATE = TOOLKIT / "commands" / "ontology-validate.md"
SECTIONS = {"operation": "## 操作类检查（PART=operation）", "object": "## 对象类检查（PART=object）",
            "cross": "## 跨文档检查（PART=cross）"}
RUN_PATH = re.compile(r"\.ontology-state/semantic/\d{8}-\d{6}-[0-9a-f]{4}")


@pytest.fixture
def issues(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("semantic_issues")


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def cli(issues, capsys, monkeypatch):
    def run(*argv: str, stdin: str = "") -> tuple[int, str]:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = issues.main(list(argv))
        return code, capsys.readouterr().out.strip()

    return run


def _issue(**changes) -> dict:
    item = {"severity": "警告", "category": "兜底缺失", "file": "operation/创建合成订单.md",
            "message": "调用[[查询合成库存]]后没有兜底", "confidence": "medium"}
    item.update(changes)
    return item


def _lines(*items: dict) -> str:
    return "\n".join(json.dumps(item, ensure_ascii=False) for item in items)


def _init(cli, mode: str = "full") -> str:
    code, out = cli("init", "--mode", mode)
    assert code == 0 and RUN_PATH.fullmatch(out), out
    return out


def _records(project: Path, run: str, part: str) -> list[dict]:
    path = project / run / f"{part}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_init_makes_a_run_directory_with_its_mode(cli, project):
    first, second = _init(cli), _init(cli, "change")
    assert first != second
    assert json.loads((project / first / "run.json").read_text(encoding="utf-8")) == {"mode": "full"}
    assert json.loads((project / second / "run.json").read_text(encoding="utf-8")) == {"mode": "change"}


def test_init_keeps_only_recent_runs(cli, project, issues):
    base = project / ".ontology-state" / "semantic"
    for index in range(issues.KEEP_RUNS + 3):
        (base / f"20260101-0000{index:02d}-abcd").mkdir(parents=True)
    (base / "manual-notes").mkdir()
    run = _init(cli)
    kept = sorted(path.name for path in base.iterdir() if RUN_PATH.fullmatch(f".ontology-state/semantic/{path.name}"))
    assert len(kept) == issues.KEEP_RUNS and Path(run).name in kept and "20260101-000000-abcd" not in kept
    assert (base / "manual-notes").is_dir()


def test_add_accepts_object_array_and_lines_and_normalizes_files(cli, project):
    run = _init(cli)
    code, out = cli("add", "--dir", run, "--part", "operation", "--check", "兜底完整性",
                    stdin=json.dumps(_issue(file="ontology/operation/创建合成订单.md"), ensure_ascii=False))
    assert code == 0 and out == "已记录 1 条（本部分共 1 条）"
    fenced = "```json\n" + json.dumps([_issue(message="两份文档口径不同", file=(
        "./ontology/object/合成订单.md，/data/ws/p1/ontology/operation/创建合成订单.md"))], ensure_ascii=False) + "\n```"
    assert cli("add", "--dir", run, "--part", "operation", "--check", "6. 对象关联分类", stdin=fenced)[0] == 0
    code, out = cli("add", "--dir", run, "--part", "operation", "--check", "Data/Operation内容边界与口径一致",
                    stdin=_lines(_issue(message="合成字段口径缺失"), _issue(message="合成单位不一致", severity="建议")))
    assert code == 0 and out == "已记录 2 条（本部分共 4 条）"
    records = _records(project, run, "operation")
    assert [record["file"] for record in records] == [
        "operation/创建合成订单.md", "object/合成订单.md, operation/创建合成订单.md",
        "operation/创建合成订单.md", "operation/创建合成订单.md"]
    assert [record["check"] for record in records] == [
        "兜底完整性", "对象关联分类", "Data/Operation 内容边界与口径一致", "Data/Operation 内容边界与口径一致"]
    assert {record["part"] for record in records} == {"operation"}
    # 补派时同一条再记一次：不重复记
    code, out = cli("add", "--dir", run, "--part", "operation", "--check", "兜底完整性", stdin=_lines(_issue()))
    assert code == 0 and out == "已记录 0 条，1 条与已记的重复没再记（本部分共 4 条）"
    assert len(_records(project, run, "operation")) == 4


def test_invalid_batch_is_rejected_whole_with_reasons(cli, project):
    run = _init(cli)
    bad = _issue(severity="严重", category="合成类别", message=" ")
    del bad["confidence"]
    code, out = cli("add", "--dir", run, "--part", "object", "--check", "描述歧义", stdin=_lines(_issue(), bad))
    assert code == 2
    assert out.splitlines()[0] == "没有记录：本批 2 条里有不合格的，改正后整批重发。"
    assert "第 2 条：severity 取值「严重」不对，应为 错误 / 警告 / 建议" in out
    assert "第 2 条：category 取值「合成类别」不对" in out
    assert "第 2 条：confidence 取值（缺）不对" in out and "第 2 条：message 不能为空" in out
    assert "第 1 条" not in out
    assert not (project / run / "object.jsonl").exists()

    code, out = cli("add", "--dir", run, "--part", "object", "--check", "描述歧义", stdin='{"severity": "警告",')
    assert code == 2 and "不是合法的 JSON" in out and "第 1 行" in out
    code, out = cli("add", "--dir", run, "--part", "object", "--check", "兜底完整性", stdin=_lines(_issue()))
    assert code == 2 and "不在对象类检查里" in out and "关联对象数据支撑" in out
    assert cli("add", "--dir", run, "--part", "objects", stdin=_lines(_issue()))[0] == 2
    code, out = cli("add", "--dir", ".ontology-state/semantic/none", "--part", "object", stdin=_lines(_issue()))
    assert code == 2 and "先运行 init" in out
    assert not (project / run / "object.jsonl").exists()


def test_change_mode_requires_origin_and_reports_existing_only_for_errors(cli, project):
    run = _init(cli, "change")
    code, out = cli("add", "--dir", run, "--part", "all", stdin=_lines(_issue()))
    assert code == 2 and "origin 必填" in out
    code, out = cli("add", "--dir", run, "--part", "all", stdin=_lines(_issue(origin="existing")))
    assert code == 2 and "只在 severity 为「错误」时上报" in out
    assert cli("add", "--dir", run, "--part", "all", stdin=_lines(_issue(origin="later")))[0] == 2
    assert cli("add", "--dir", run, "--part", "all", stdin=_lines(
        _issue(origin="existing", severity="错误", message="原有的合成错误"), _issue(origin="change")))[0] == 0
    assert [record["origin"] for record in _records(project, run, "all")] == ["existing", "change"]


def test_regression_mode_keeps_at_most_three_fix_related_issues(cli, project, issues):
    run = _init(cli, "regression")
    code, out = cli("add", "--dir", run, "--part", "all", stdin=_lines(_issue(confidence="low")))
    assert code == 2 and "high 或 medium" in out
    assert cli("add", "--dir", run, "--part", "all", stdin=_lines(_issue(origin="existing")))[0] == 2
    three = [_issue(message=f"修复后边界仍不一致 {index}") for index in range(issues.REGRESSION_LIMIT)]
    assert cli("add", "--dir", run, "--part", "all", stdin=_lines(*three))[0] == 0
    assert {record["origin"] for record in _records(project, run, "all")} == {"change"}
    code, out = cli("add", "--dir", run, "--part", "all", stdin=_lines(_issue(message="第四条")))
    assert code == 2 and "最多记 3 条" in out
    assert len(_records(project, run, "all")) == issues.REGRESSION_LIMIT


def test_done_and_status_list_missing_checks(cli, project, issues):
    run = _init(cli)
    code, out = cli("done", "--dir", run, "--part", "object")
    assert code == 2 and "要带 --check" in out and "描述歧义、关联合理性、业务合理性、关联对象数据支撑" in out
    code, out = cli("done", "--dir", run, "--part", "object", "--check", "1. 描述歧义")
    assert code == 0 and out == "已标做完「描述歧义」；对象类检查还剩 3 项：关联合理性、业务合理性、关联对象数据支撑"
    cli("done", "--dir", run, "--part", "object", "--check", "关联合理性")
    cli("done", "--dir", run, "--part", "object", "--check", "关联合理性")
    assert (project / run / "object.done").read_text(encoding="utf-8").splitlines() == ["描述歧义", "关联合理性"]
    cli("add", "--dir", run, "--part", "object", "--check", "描述歧义",
        stdin=_lines(_issue(file="object/合成订单.md", category="描述歧义", message="合成状态含义不清")))
    # 别的部分的检查项不能标在这一部分
    assert cli("done", "--dir", run, "--part", "cross", "--check", "Data/Operation 内容边界与口径一致")[0] == 2

    code, out = cli("status", "--dir", run, "--parts", "operation,object,cross")
    assert code == 0
    assert out.splitlines() == [
        f"操作类（operation）：做完 0/{len(issues.CHECKS['operation'])}，缺 {'、'.join(issues.CHECKS['operation'])}；记 0 条",
        "对象类（object）：做完 2/4，缺 业务合理性、关联对象数据支撑；记 1 条",
        f"跨文档（cross）：做完 0/5，缺 {'、'.join(issues.CHECKS['cross'])}；记 0 条",
        "有缺项：operation、object、cross",
    ]

    # 补派（ONLY_CHECKS）只做缺的两项：已记的问题保留
    for check in ("业务合理性", "关联对象数据支撑"):
        code, out = cli("done", "--dir", run, "--part", "object", "--check", check)
    assert out == "已标做完「关联对象数据支撑」；对象类检查 4 项都做完了"
    assert cli("status", "--dir", run, "--parts", "object")[1].splitlines() == [
        "对象类（object）：做完 4/4；记 1 条", "全部做完"]
    assert cli("status", "--dir", run, "--parts", "")[0] == 2


def test_whole_part_is_marked_once_without_check(cli, project):
    run = _init(cli, "change")
    assert cli("status", "--dir", run, "--parts", "all")[1].splitlines() == ["整份（all）：未标完成；记 0 条", "有缺项：all"]
    code, _ = cli("add", "--dir", run, "--part", "all", "--check", "兜底完整性", stdin=_lines(_issue(origin="change")))
    assert code == 0
    assert cli("done", "--dir", run, "--part", "all") == (0, "已标完成：整份检查做完了")
    assert cli("status", "--dir", run, "--parts", "all")[1].splitlines() == ["整份（all）：已标完成；记 1 条", "全部做完"]


def test_merge_sorts_dedupes_and_drops_internal_fields(cli, project):
    run = _init(cli)
    low = _issue(file="operation/b.md", message="合成建议", severity="建议", confidence="low")
    high = _issue(file="operation/b.md", message="合成分支缺失", category="逻辑缺陷", confidence="high")
    error = _issue(file="object/a.md", message="合成空值当零", severity="错误", category="空值语义模糊")
    cli("add", "--dir", run, "--part", "operation", "--check", "逻辑步骤完整性", stdin=_lines(low, high, error))
    pair = _issue(file="object/a.md, operation/b.md", message="合成口径不一致", category="语义口径不一致",
                  confidence="high")
    cli("add", "--dir", run, "--part", "cross", "--check", "字段引用", stdin=_lines(high, pair))

    code, out = cli("merge", "--dir", run)
    merged = json.loads(out)
    assert code == 0
    assert [(item["file"], item["message"]) for item in merged] == [
        ("object/a.md", "合成空值当零"), ("object/a.md, operation/b.md", "合成口径不一致"),
        ("operation/b.md", "合成分支缺失"), ("operation/b.md", "合成建议")]
    assert all("part" not in item and "check" not in item for item in merged)
    assert merged[0] == error
    assert json.loads(cli("merge", "--dir", run, "--parts", "cross")[1]) == [pair, high]
    assert cli("merge", "--dir", run, "--parts", "object") == (0, "[]")


def test_check_list_matches_the_spec_sections(issues):
    spec = SPEC.read_text(encoding="utf-8")
    for part, heading in SECTIONS.items():
        assert spec.count(heading) == 1, heading
        body = spec.split(heading, 1)[1].split("\n## ", 1)[0]
        headings = re.findall(r"^### (\d+)\. (.+?)\s*$", body, re.M)
        assert [number for number, _ in headings] == [str(index) for index in range(1, len(headings) + 1)]
        assert tuple(name for _, name in headings) == issues.CHECKS[part]
    order = [spec.index(heading) for heading in SECTIONS.values()]
    assert order == sorted(order) and spec.index("## 记录方式") < order[0] < order[-1] < spec.index("## 各模式说明")


def test_value_lists_match_the_spec(issues):
    spec = SPEC.read_text(encoding="utf-8")

    def values(label: str) -> tuple[str, ...]:
        line = next(line for line in spec.splitlines() if line.startswith(label))
        return tuple(re.findall(r'"([^"]+)"', line))

    assert values("Valid severity values:") == issues.SEVERITIES
    assert values("Valid category values:") == issues.CATEGORIES
    assert "`high` | `medium` | `low`" in spec and issues.CONFIDENCES == ("high", "medium", "low")
    assert "semantic_issues.py add --dir {ISSUES_DIR} --part {PART}" in spec
    assert "semantic_issues.py done --dir {ISSUES_DIR} --part {PART}" in spec


def test_manifest_and_validate_command_use_the_script():
    manifest = yaml.safe_load((TOOLKIT / "manifest.yaml").read_text(encoding="utf-8"))
    assert "semantic_issues.py" in manifest["contributes"]["scripts"]
    validate = VALIDATE.read_text(encoding="utf-8")
    dispatch = validate.split("### B1:", 1)[1].split("### B2:", 1)[0]
    for phrase in ("semantic_issues.py init --mode {MODE}", "semantic_issues.py status --dir {ISSUES_DIR}",
                   "semantic_issues.py merge --dir {ISSUES_DIR}", "同一条消息", "PART: {部分}", "ONLY_CHECKS:",
                   "UNFINISHED_CHECKS", "`cross` 总是派"):
        assert phrase in dispatch, phrase
    assert "return a JSON array" not in dispatch
    regression = validate.split("### B5:", 1)[1]
    assert "semantic_issues.py init --mode regression" in regression and "PART: all" in regression
    assert "未完成的检查项" in validate.split("### B2:", 1)[1].split("### B4", 1)[0]


def test_guards_let_the_recording_commands_through(project, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    run = ".ontology-state/semantic/20260101-000000-abcd"
    (project / run).mkdir(parents=True)
    script = "python3 .claude/scripts/semantic_issues.py"
    commands = [
        f"{script} init --mode full",
        f"{script} add --dir {run} --part operation --check 兜底完整性 <<'EOF'\n{_lines(_issue())}\nEOF",
        f'{script} done --dir {run} --part operation --check "Data/Operation 内容边界与口径一致"',
        f"{script} status --dir {run} --parts operation,object,cross",
        f"{script} merge --dir {run}",
    ]
    for guard in ("lock", "scope", "toolset", "resource"):
        for command in commands:
            call = {"cwd": str(project), "tool_name": "Bash", "tool_input": {"command": command}}
            assert guards.check(guard, call) is None, (guard, command)
