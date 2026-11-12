#!/usr/bin/env python3
"""反馈演进的泛化性校验：检测知识文档里的"应试"痕迹。

按失分反馈修订知识包容易走偏成"把标准答案抄进知识库"——分数看着涨，知识却没有普适价值，
换一批用例立刻失效。本脚本在修订落盘后机械核对：

1. **原文雷同**：知识文档与反馈用例的问题/标准答案存在 >= N 个连续字符完全相同；
2. **应试措辞**：知识文档里出现"标准答案""评测用例""判分"等面向考核的表述；
   "得分""评分标准"这类在业务里也正当的词，只有与考核语境同行时才判定。

用例语料默认取最新一批反馈（反馈入口写入的 feedback/*.json，格式 onto-feedback/v1）；
没有反馈时回退到旧版评测报告 reports/eval-*.md（评测移出前留在工作区里的）。
本脚本随反馈演进包迁出 core（待包机制落地，见 _plan/gaps.md）。

用法（工作区根目录执行）：

    python3 .claude/scripts/check_eval_generalization.py                 # 校验 git 未提交的本体改动
    python3 .claude/scripts/check_eval_generalization.py --files a.md b.md
    python3 .claude/scripts/check_eval_generalization.py --base HEAD~1   # 指定比对基线
    python3 .claude/scripts/check_eval_generalization.py --feedback feedback/<批次>.json
    python3 .claude/scripts/check_eval_generalization.py --json

发现问题时 exit 1。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_THRESHOLD = 30

# 只可能出现在考核语境的表述——出现即为应试痕迹。
EXAM_MARKERS = ('标准答案', '评测用例', '评测问题', '判分', '用例编号')

# 业务域里也可能是正当术语（评分类对象、问卷类场景）——只有与考核语境同行时才算应试。
CONTEXTUAL_EXAM_MARKERS = ('得分', '考点', '评分标准')
EXAM_CONTEXT_WORDS = ('评测', '用例', '判分', '标准答案', '考核', '本题', '该题', '这道题')

CASE_FIELD_PATTERN = re.compile(r'^\s*-\s*\*\*(问题|标准答案)\*\*[：:]\s*(.+)$')
FEEDBACK_DIR = 'feedback'
FEEDBACK_TEXT_FIELDS = ('question', 'expected')

# 知识包打包范围（与 ontology_checker.PACKAGED_SUBDIRS 一致）。
PACKAGED_SUBDIRS = ('object', 'operation', 'logic', 'rule', 'metric')


# 命令行输出通道：独立 logger，不经根 logger，消息原样（无前缀）
_console = logging.Logger('check_eval_generalization', logging.INFO)
_console_handler = logging.StreamHandler()
_console.addHandler(_console_handler)


def _say(text: object = '', *, error: bool = False) -> None:
    """输出一行：结果写标准输出（助手读它），error=True 写标准错误；每次取当前的流，测试替换了也跟着走。"""
    _console_handler.setStream(sys.stderr if error else sys.stdout)
    _console.info(text)


def normalize(text: str) -> str:
    """去掉空白与 markdown 装饰，只留可比对的实义字符。"""
    text = re.sub(r'```.*?```', '', text, flags=re.S)       # 代码块不参与比对
    text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.S)  # frontmatter
    text = re.sub(r'[\s*#`>|\[\]()_~]+', '', text)
    return text


def _feedback_texts(path: Path) -> list[str]:
    """反馈批次（onto-feedback/v1）里每条反馈的问题与标准答案。"""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    texts = []
    for item in (data.get('items') if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        for key in FEEDBACK_TEXT_FIELDS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
    return texts


def _report_texts(path: Path) -> list[str]:
    """旧版评测报告（markdown）里的「问题」「标准答案」行。"""
    if not path.is_file():
        return []
    texts = []
    for line in path.read_text(encoding='utf-8').splitlines():
        m = CASE_FIELD_PATTERN.match(line)
        if m:
            texts.append(m.group(2).strip())
    return texts


def read_cases(workspace: Path, report: Path | None = None, feedback: Path | None = None) -> list[str]:
    """收集用例语料：指定的反馈批次或报告优先；都不指定时取最新一批反馈，没有反馈再取最新的旧版报告。"""
    if feedback is not None:
        return _feedback_texts(feedback)
    if report is not None:
        return _report_texts(report)
    folder = workspace / FEEDBACK_DIR
    batches = sorted(folder.glob('*.json')) if folder.is_dir() else []
    if batches:
        return _feedback_texts(batches[-1])  # 文件名以接收时间开头，按名排序即按时间
    reports = sorted(
        (workspace / 'reports').glob('eval-*.md'), key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return _report_texts(reports[0]) if reports else []


def changed_docs(workspace: Path, base: str | None) -> list[Path]:
    """git 视角的本体文档改动；base 为 None 时取工作区全部未入库改动。"""
    def git(*args: str) -> list[str]:
        try:
            out = subprocess.run(
                # quotepath=false：中文路径不被转义成 "\345\272\224..." 形式
                ['git', '-c', 'core.quotepath=false', *args],
                cwd=workspace, text=True, capture_output=True, check=True,
            ).stdout
        except Exception:
            return []
        return [n.strip() for n in out.splitlines() if n.strip()]

    if base:
        names = git('diff', '--name-only', base)
    else:
        names = (
            git('diff', '--name-only')
            + git('diff', '--name-only', '--cached')
            + git('ls-files', '--others', '--exclude-standard')
        )

    docs = []
    for name in dict.fromkeys(names):
        if not name.endswith('.md'):
            continue
        parts = Path(name).parts
        if len(parts) >= 3 and parts[0] == 'ontology' and parts[1] in PACKAGED_SUBDIRS:
            path = workspace / name
            if path.is_file():
                docs.append(path)
    return docs


def longest_shared_run(doc_text: str, case_text: str, threshold: int) -> str | None:
    """返回文档与用例共有的最长连续片段（>= threshold 才算）。"""
    if len(doc_text) < threshold or len(case_text) < threshold:
        return None
    grams = {case_text[i:i + threshold] for i in range(len(case_text) - threshold + 1)}
    best = ''
    i = 0
    while i <= len(doc_text) - threshold:
        if doc_text[i:i + threshold] in grams:
            end = i + threshold
            while end < len(doc_text) and doc_text[end - threshold + 1:end + 1] in grams:
                end += 1
            run = doc_text[i:end]
            if len(run) > len(best):
                best = run
            i = end - threshold + 1
        else:
            i += 1
    return best or None


def exam_wording_hits(raw: str) -> list[str]:
    """命中的应试措辞。上下文型标记只在与考核语境同行时才算，避免误伤业务术语。"""
    hits = [m for m in EXAM_MARKERS if m in raw]
    for line in raw.splitlines():
        context = next((w for w in EXAM_CONTEXT_WORDS if w in line), None)
        if not context:
            continue
        for marker in CONTEXTUAL_EXAM_MARKERS:
            hit = f'{marker}（与「{context}」同行）'
            if marker in line and hit not in hits:
                hits.append(hit)
    return hits


def check(workspace: Path, docs: list[Path], cases: list[str], threshold: int) -> list[dict]:
    findings: list[dict] = []
    norm_cases = [(c, normalize(c)) for c in cases]
    for doc in docs:
        rel = str(doc.relative_to(workspace))
        raw = doc.read_text(encoding='utf-8')
        norm_doc = normalize(raw)

        for original, norm_case in norm_cases:
            run = longest_shared_run(norm_doc, norm_case, threshold)
            if run:
                findings.append({
                    'severity': '错误',
                    'file': rel,
                    'category': '应试痕迹',
                    'message': (
                        f'与反馈用例存在 {len(run)} 字连续雷同："{run[:60]}"…… '
                        f'（用例片段："{original[:40]}"……）——请改写为普适业务知识表达，'
                        f'不要照抄用例问题或标准答案。'
                    ),
                })
                break  # 同一文档只报最先命中的用例，避免噪声

        hits = exam_wording_hits(raw)
        if hits:
            findings.append({
                'severity': '错误',
                'file': rel,
                'category': '应试措辞',
                'message': (
                    f'知识文档中出现面向考核的表述：{"、".join(hits)}——'
                    f'知识文档只描述业务本身，不引用考核语境。'
                ),
            })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description='检查反馈演进的知识修订是否泛化（无应试痕迹）。')
    parser.add_argument('--workspace', default='.', help='工作区根目录（默认当前目录）')
    parser.add_argument('--files', nargs='*', help='只检查这些文档（默认取 git 改动）')
    parser.add_argument('--base', help='git 比对基线（默认工作区未提交改动）')
    parser.add_argument('--feedback', help='反馈批次路径（默认取最新 feedback/*.json）')
    parser.add_argument('--report', help='旧版评测报告路径（没有反馈批次时默认取最新 reports/eval-*.md）')
    parser.add_argument('--threshold', type=int, default=DEFAULT_THRESHOLD,
                        help=f'判定雷同的连续字符数（默认 {DEFAULT_THRESHOLD}）')
    parser.add_argument('--json', action='store_true', help='以 JSON 输出')
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()

    if args.files:
        docs = [(workspace / f).resolve() for f in args.files]
        docs = [d for d in docs if d.is_file()]
    else:
        docs = changed_docs(workspace, args.base)

    report = Path(args.report).resolve() if args.report else None
    feedback = Path(args.feedback).resolve() if args.feedback else None
    cases = read_cases(workspace, report, feedback)

    if not cases:
        msg = '未找到用例语料（没有 feedback/*.json 反馈批次，也没有旧版 reports/eval-*.md），跳过校验。'
        _say(json.dumps({'skipped': msg}, ensure_ascii=False) if args.json else f'⚠️  {msg}')
        return 0
    if not docs:
        msg = '本次没有打包范围内的本体文档改动，无需校验。'
        _say(json.dumps({'skipped': msg}, ensure_ascii=False) if args.json else f'✅ {msg}')
        return 0

    findings = check(workspace, docs, cases, args.threshold)

    if args.json:
        _say(json.dumps(findings, ensure_ascii=False))
    else:
        scope = f'{len(docs)} 个文档 / {len(cases)} 条用例语料'
        if not findings:
            _say(f'✅ 泛化性校验通过（{scope}，无原文雷同与应试措辞）')
        else:
            _say(f'❌ 泛化性校验发现 {len(findings)} 个问题（{scope}）：')
            for f in findings:
                _say(f'  - [{f["category"]}] {f["file"]}：{f["message"]}')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
