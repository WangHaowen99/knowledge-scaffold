"""已启用行业包的声明式校验规则（详设 5.5「校验」：ontology_checker 额外加载 rules.yaml）。

装配器把各行业包的 `rules.yaml`、`concepts.yaml` 另存成 `.claude/industry/<包名>/checks.json`、
`concepts.json`（工作区脚本只用标准库）。规则只有四类，全部只给警告或建议，不阻断提交：

- require_relations_to：对象的「关联对象」里要有指向这些概念的关系；
- require_fields：对象属性清单里要有名称含这些词的属性（中文名或英文名）；
- require_sections：条目要有这些标题；
- prefer_terms：{非标准称谓: 标准称谓}，正文里出现非标准称谓（别名段除外）时提示。

`match: {concept: 名}` 按条目名匹配概念名与别名（条目名等于、或包含概念名）；`applies_to` 取
object / operation / rule / metric / any。
"""

from __future__ import annotations

import json
from pathlib import Path

RULE_KINDS = ('require_relations_to', 'require_fields', 'require_sections', 'prefer_terms')
LEVELS = {'warning': '警告', 'suggestion': '建议'}


def load(workspace_root: Path) -> list:
    """[(包信息, 概念表 {名: 别名集合}, 规则)]；读不出的包跳过。"""
    base = Path(workspace_root) / '.claude' / 'industry'
    out = []
    if not base.is_dir():
        return out
    for info_path in sorted(base.glob('*/pack.json')):
        try:
            info = json.loads(info_path.read_text(encoding='utf-8'))
            if not info.get('checks'):
                continue
            checks = json.loads((info_path.parent / info['checks']).read_text(encoding='utf-8'))
            concepts = {}
            if info.get('concepts'):
                data = json.loads((info_path.parent / info['concepts']).read_text(encoding='utf-8'))
                for item in data.get('concepts') or []:
                    if isinstance(item, dict) and item.get('name'):
                        concepts[str(item['name'])] = {str(item['name']), *map(str, item.get('aliases') or [])}
        except (OSError, ValueError, KeyError, AttributeError):
            continue
        for rule in checks.get('rules') or []:
            if isinstance(rule, dict) and any(kind in rule for kind in RULE_KINDS):
                out.append((info, concepts, rule))
    return out


def _names(concept: str, concepts: dict) -> set:
    return concepts.get(concept) or {concept}


def matches_concept(name: str, concept: str, concepts: dict) -> bool:
    return any(name == alias or (len(alias) > 1 and alias in name) for alias in _names(concept, concepts))


def _without_alias_sections(raw: str) -> str:
    kept, skip = [], False
    for line in raw.split('\n'):
        if line.startswith('#'):
            skip = '别名' in line
        if not skip:
            kept.append(line)
    return '\n'.join(kept)


def violations(doc_type: str, name: str, sections: dict, field_names: set, relation_targets: set,
               raw: str, concepts: dict, rule: dict) -> list:
    """一条规则对一个条目的问题说明（空列表表示满足或不适用）。"""
    applies = str(rule.get('applies_to') or 'any')
    if applies != 'any' and applies != doc_type:
        return []
    concept = str((rule.get('match') or {}).get('concept') or '')
    if concept and not matches_concept(name, concept, concepts):
        return []
    problems = []
    if doc_type == 'object':
        for target in rule.get('require_relations_to') or []:
            if not any(matches_concept(item, str(target), concepts) for item in relation_targets):
                problems.append(f'缺少指向「{target}」的关联')
        for word in rule.get('require_fields') or []:
            if not any(str(word) in item for item in field_names):
                problems.append(f'属性清单里没有「{word}」')
    for title in rule.get('require_sections') or []:
        if not any(str(title) in heading for heading in sections):
            problems.append(f'缺少「{title}」一节')
    text = _without_alias_sections(raw)
    for loose, standard in (rule.get('prefer_terms') or {}).items():
        if str(loose) and str(loose) in text:
            problems.append(f'「{loose}」建议改称「{standard}」')
    return problems


def level(rule: dict) -> str:
    return LEVELS.get(str(rule.get('level') or 'warning'), '警告')
