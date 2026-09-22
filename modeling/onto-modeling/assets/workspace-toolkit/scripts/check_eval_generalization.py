#!/usr/bin/env python3
"""评测演进的泛化性校验：检测知识文档里的"应试"痕迹。

评测演进容易走偏成"把标准答案抄进知识库"——分数看着涨，知识却没有普适价值，
换一批用例立刻失效。本脚本在修订落盘后机械核对：

1. **原文雷同**：知识文档与评测用例的问题/标准答案存在 >= N 个连续字符完全相同；
2. **应试措辞**：知识文档里出现"标准答案""评测用例""判分"等面向考核的表述；
   "得分""评分标准"这类在业务里也正当的词，只有与考核语境同行时才判定。

用法（工作区根目录执行）：

    python3 .claude/scripts/check_eval_generalization.py                 # 校验 git 未提交的本体改动
    python3 .claude/scripts/check_eval_generalization.py --files a.md b.md
    python3 .claude/scripts/check_eval_generalization.py --base HEAD~1   # 指定比对基线
    python3 .claude/scripts/check_eval_generalization.py --json

发现问题时 exit 1。
"""
from __future__ import annotations

import argparse
import json
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

# 知识包打包范围（与 ontology_checker.PACKAGED_SUBDIRS 一致）。
PACKAGED_SUBDIRS = ('object', 'operation', 'logic', 'rule', 'metric')


def normalize(text: str) -> str:
    """去掉空白与 markdown 装饰，只留可比对的实义字符。"""
    text = re.sub(r'```.*?```', '', text, flags=re.S)       # 代码块不参与比对
    text = re.sub(r'^---\n.*?\n---\n', '', text, flags=re.S)  # frontmatter
    text = re.sub(r'[\s*#`>|\[\]()_~]+', '', text)
    return text


def read_cases(workspace: Path, report: Path | None) -> list[str]:
    """收集评测用例语料：优先指定报告，否则取最新 eval 报告 + eval.json。"""
    texts: list[str] = []

    report_path = report
    if report_path is None:
        candidates = sorted(
            (workspace / 'reports').glob('eval-*.md'),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        report_path = candidates[0] if candidates else None

    if report_path and report_path.is_file():
        for line in report_path.read_text(encoding='utf-8').splitlines():
            m = CASE_FIELD_PATTERN.match(line)
            if m:
                texts.append(m.group(2).strip())

    state = workspace / '.ontology-state' / 'eval.json'
    if state.is_file():
        try:
            data = json.loads(state.read_text(encoding='utf-8'))
        except Exception:
            data = {}
        for case in data.get('cases') or []:
            for key in ('question', 'groundTruth', 'ground_truth'):
                value = case.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())

    return texts


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
                        f'与评测用例存在 {len(run)} 字连续雷同："{run[:60]}"…… '
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
                    f'知识文档只描述业务本身，不引用评测语境。'
                ),
            })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description='检查评测演进的知识修订是否泛化（无应试痕迹）。')
    parser.add_argument('--workspace', default='.', help='工作区根目录（默认当前目录）')
    parser.add_argument('--files', nargs='*', help='只检查这些文档（默认取 git 改动）')
    parser.add_argument('--base', help='git 比对基线（默认工作区未提交改动）')
    parser.add_argument('--report', help='评测报告路径（默认取最新 reports/eval-*.md）')
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
    cases = read_cases(workspace, report)

    if not cases:
        msg = '未找到评测用例语料（无 reports/eval-*.md 也无 .ontology-state/eval.json），跳过校验。'
        print(json.dumps({'skipped': msg}, ensure_ascii=False) if args.json else f'⚠️  {msg}')
        return 0
    if not docs:
        msg = '本次没有打包范围内的本体文档改动，无需校验。'
        print(json.dumps({'skipped': msg}, ensure_ascii=False) if args.json else f'✅ {msg}')
        return 0

    findings = check(workspace, docs, cases, args.threshold)

    if args.json:
        print(json.dumps(findings, ensure_ascii=False))
    else:
        scope = f'{len(docs)} 个文档 / {len(cases)} 条用例语料'
        if not findings:
            print(f'✅ 泛化性校验通过（{scope}，无原文雷同与应试措辞）')
        else:
            print(f'❌ 泛化性校验发现 {len(findings)} 个问题（{scope}）：')
            for f in findings:
                print(f'  - [{f["category"]}] {f["file"]}：{f["message"]}')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
