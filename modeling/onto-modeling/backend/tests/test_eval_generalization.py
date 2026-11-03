"""反馈演进的泛化性校验测试：应试痕迹检测（原文雷同 / 考核措辞）。

脚本走真实 subprocess 执行，与 /ontology-evolution Step 4 的调用方式一致。
"""

import json
import subprocess
from pathlib import Path

from app import config

# 泛化检查随反馈演进包拆出了 core（方案 D7 后半）
SCRIPTS = Path(config.ASSETS_DIR) / "packs" / "evo" / "scripts"

GROUND_TRUTH = (
    "冷链配送场景下，温控超标的判定依据是车厢温度连续超出阈值十五分钟以上，"
    "此时需要立即触发预警并在两小时内完成货物复检与责任归属确认。"
)
QUESTION = "冷链配送中温控超标应该在多长时间内完成货物复检？"


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "ontology" / "rule").mkdir(parents=True)
    (ws / "reports").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / "reports" / "eval-20260101-000000.md").write_text(
        "# 评测报告\n\n## 逐用例明细\n\n### 用例 1\n"
        f"- **问题**：{QUESTION}\n"
        f"- **标准答案**：{GROUND_TRUTH}\n"
        "- **判分**：❌ fail\n",
        encoding="utf-8",
    )
    return ws


def run_script(ws: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(SCRIPTS / "check_eval_generalization.py"), *args],
        cwd=ws, capture_output=True, text=True, timeout=60,
    )


def write_rule(ws: Path, name: str, body: str) -> None:
    (ws / "ontology" / "rule" / name).write_text(body, encoding="utf-8")


def test_detects_verbatim_copy_of_ground_truth(tmp_path):
    """把标准答案原文抄进规则文档 —— 硬错误。"""
    ws = make_workspace(tmp_path)
    write_rule(ws, "温控超标处置.md", f"# 规则描述\n\n{GROUND_TRUTH}\n")

    proc = run_script(ws, "--json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    findings = json.loads(proc.stdout)
    assert [f["category"] for f in findings] == ["应试痕迹"]
    assert findings[0]["file"] == "ontology/rule/温控超标处置.md"
    assert findings[0]["severity"] == "错误"


def test_detects_exam_wording(tmp_path):
    """引用评测语境（标准答案/判分等）—— 硬错误，且与雷同判定独立。"""
    ws = make_workspace(tmp_path)
    write_rule(
        ws,
        "温控预警.md",
        "# 规则描述\n\n本题的标准答案要求先预警后复检。\n",
    )

    proc = run_script(ws, "--json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    findings = json.loads(proc.stdout)
    assert [f["category"] for f in findings] == ["应试措辞"]
    assert "标准答案" in findings[0]["message"]


def test_business_scoring_terms_are_not_exam_wording(tmp_path):
    """评分类业务术语（得分/评分标准）不算应试措辞——只在考核语境同行时才判。"""
    ws = make_workspace(tmp_path)
    write_rule(
        ws,
        "承运商评分.md",
        "# 规则描述\n\n承运商月度得分由时效、温控与破损三项加权得出。\n\n"
        "# 方案要点\n\n评分标准每季度复核一次，得分低于基线的承运商进入整改名单。\n",
    )
    assert run_script(ws, "--json").returncode == 0

    write_rule(
        ws,
        "承运商评分.md",
        "# 规则描述\n\n承运商月度得分由时效、温控与破损三项加权得出。\n\n"
        "# 方案要点\n\n该题的得分点是先答时效再答温控。\n",
    )
    proc = run_script(ws, "--json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)[0]["category"] == "应试措辞"


def test_generalized_rule_passes(tmp_path):
    """普适业务表达（同一知识点、自己的措辞）—— 通过。"""
    ws = make_workspace(tmp_path)
    write_rule(
        ws,
        "温控异常处置.md",
        "# 规则描述\n\n温控类异常按“监测—预警—复检—定责”四步闭环处理。\n\n"
        "# 触发特征\n\n车厢温度偏离设定区间且持续时间跨过告警窗口。\n\n"
        "# 方案要点\n\n告警窗口与复检时限由品类的耐温能力决定，"
        "生鲜类最短、常温药品类次之；复检结论需同时落到货物状态与责任方两个字段上。\n",
    )

    proc = run_script(ws, "--json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == []


def test_threshold_controls_sensitivity(tmp_path):
    """连续雷同长度低于阈值时不报——阈值可调但默认收紧。"""
    ws = make_workspace(tmp_path)
    write_rule(ws, "片段.md", "# 规则描述\n\n温控超标的判定依据是车厢温度连续超出阈值。\n")

    assert run_script(ws, "--json", "--threshold", "18").returncode == 1
    assert run_script(ws, "--json", "--threshold", "40").returncode == 0


def test_only_checks_packaged_dirs(tmp_path):
    """非打包目录的改动不在校验范围内（那里的内容本就到不了 consumer）。"""
    ws = make_workspace(tmp_path)
    (ws / "ontology" / "knowledge").mkdir(parents=True)
    (ws / "ontology" / "knowledge" / "沉淀.md").write_text(GROUND_TRUTH, encoding="utf-8")

    proc = run_script(ws)
    assert proc.returncode == 0
    assert "无需校验" in proc.stdout


def write_feedback(ws: Path, name: str, expected: str) -> Path:
    """反馈入口写入的批次文件（onto-feedback/v1，ADR-0020）。"""
    path = ws / "feedback" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "format": "onto-feedback/v1", "feedback_id": path.stem, "source": {"service": "合成评测服务", "run": "r1"},
        "items": [{"case_id": "c1", "question": QUESTION, "expected": expected, "actual": "", "verdict": "fail"}],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_reads_cases_from_latest_feedback_batch(tmp_path):
    """有反馈批次时取最新一批的问题与标准答案，旧版报告不再参与。"""
    ws = make_workspace(tmp_path)
    (ws / "reports" / "eval-20260101-000000.md").unlink()
    write_feedback(ws, "20260101-000000-old.json", "旧批次的合成标准答案，与修订文档毫无关系，只用来确认取的是最新一批。")
    write_feedback(ws, "20260102-000000-new.json", GROUND_TRUTH)
    write_rule(ws, "抄答案.md", f"# 规则描述\n\n{GROUND_TRUTH}\n")

    proc = run_script(ws, "--json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)[0]["category"] == "应试痕迹"

    older = run_script(ws, "--json", "--feedback", "feedback/20260101-000000-old.json")
    assert older.returncode == 0, older.stdout + older.stderr


def test_skips_without_case_corpus(tmp_path):
    """既无反馈批次也无旧版报告 —— 跳过而非误报。"""
    ws = make_workspace(tmp_path)
    (ws / "reports" / "eval-20260101-000000.md").unlink()
    write_rule(ws, "任意规则.md", f"# 规则描述\n\n{GROUND_TRUTH}\n")

    proc = run_script(ws)
    assert proc.returncode == 0
    assert "跳过校验" in proc.stdout


def test_files_mode_checks_staged_and_explicit_paths(tmp_path):
    """--files 显式指定；git 已 add 的改动也在默认扫描范围内。"""
    ws = make_workspace(tmp_path)
    write_rule(ws, "抄答案.md", f"# 规则描述\n\n{GROUND_TRUTH}\n")
    subprocess.run(["git", "add", "ontology"], cwd=ws, check=True)

    assert run_script(ws, "--json").returncode == 1  # staged 也被扫描到
    proc = run_script(ws, "--json", "--files", "ontology/rule/抄答案.md")
    assert proc.returncode == 1
    assert json.loads(proc.stdout)[0]["file"] == "ontology/rule/抄答案.md"
