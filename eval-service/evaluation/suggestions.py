"""Optimization suggestions: the owner's Judge model attributes failed cases to knowledge package defects.

Failed cases are analysed in batches that fit one model request of about REQUEST_CHARS characters, small
enough for judge models with a 32k-token context. Cases relating to the same package document share a
batch, each batch carries the documents relevant to its own cases, and a merge request joins batches that
found the same root cause. Documents are ranked by the cases' questions, references, judge reasons, tool
calls and answers; documents a tool call names come first, and long documents are cut to their beginning
and best-matching sections. When the gateway rejects a request as too long for the model, or the model does
not answer in time, its cases are planned again at half the size, so smaller or slower models need no
configuration. Reasoning models may think for minutes, so one request may take TIMEOUT seconds and the whole
generation GENERATION_SECONDS; progress is reported so the run page can show what is being waited for. The
model only drafts; /ontology-evolution re-verifies each item in the modeling workspace before changing
anything. Package text, questions, references, answers and the redacted tool-call previews are sent to the
same gateway that already judges this user's runs.
"""
from datetime import datetime
import json
import re
import time

from .artifacts import CHINA, clip, failed_first, outcome, summarize, tool_failure_lines, user_suggestions
from .core import Failure
from .packages import unpack

# Input characters of one request, and the floor when overflowing requests are planned again.
REQUEST_CHARS = 32000
MIN_REQUEST_CHARS = 4000
# Shares of one request: cases with their tool calls, package documents, data mapping, table headers, file list.
SHARES = {'cases': 0.40, 'documents': 0.35, 'mapping': 0.12, 'tables': 0.06, 'files': 0.07}
# Batches planned up front; cases beyond them are listed as not analysed instead of being dropped silently.
MAX_BATCHES = 8
# All requests of one generation: batches, re-planned halves, retries and the merge.
MAX_REQUESTS = 14
MAX_CANDIDATES = 200
MAX_ITEMS = 60
DOCUMENT_CHARS = 6000
INDEX_CHARS = 3000
# Index documents together take at most this share of the document budget.
INDEX_SHARE = 0.15
# Beginning of a long document always sent, ahead of its best-matching sections.
SECTION_HEAD = 800
# Recorded tool calls sent per case, failed ones first.
CASE_CALLS = 6
# One request waits at most TIMEOUT seconds and the whole generation GENERATION_SECONDS; a request is not
# started with less than MIN_REQUEST_SECONDS left.
TIMEOUT = 600
GENERATION_SECONDS = 1800
MIN_REQUEST_SECONDS = 60
SLOW_MODEL = ('判分模型长时间未返回优化建议（单次等待 10 分钟、整次生成 30 分钟），通常是推理型模型思考时间过长。'
              '已自动拆小重试仍未完成，可稍后点击“重新生成”，或在“评测判分模型配置”中换用响应更快的模型。')
CATEGORIES = {'A': 'A 知识缺失', 'B': 'B 语义模糊', 'C': 'C 数据映射', 'D': 'D 用例问题', 'E': 'E 运行异常', 'unknown': '证据不足'}
# Package directories and where /ontology-evolution edits them in the modeling workspace.
WORKSPACE_DIRS = {'objects/': 'ontology/object/', 'operations/': 'ontology/operation/', 'rules/': 'ontology/rule/',
                  'metrics/': 'ontology/metric/', 'testcase/': 'ontology/testcase/'}
SEVERITY = {'fail': 0, 'error': 1, 'partial': 2}
LINE_BREAK = re.compile(r'<br\s*/?>', re.IGNORECASE)
SYSTEM = '''你是知识包评测优化分析员。输入是一轮知识包评测中未通过的用例（问题、参考答案、智能体回答、判分理由、工具失败、工具调用记录）、本轮总体统计 overview 和该知识包的相关文档节选；用例较多时分批输入，本批只含其中一部分。
逐条归因并给出修订建议，用 submit_suggestions 提交。归因类别：
A 知识缺失：问题涉及的对象、属性、操作或规则在知识包中不存在或不完整；
B 语义模糊：概念存在但描述或别名不足，检索或理解失败；
C 数据映射：对象未绑定数据表，或来源表名、列名与评测数据表结构不一致；
D 用例问题：参考答案本身错误、过严或表述歧义；
E 运行异常：环境、凭据或服务问题，与知识包无关；
unknown：证据不足，无法判断。
规则：
1. 异常用例先看工具失败：脚本参数校验失败、脚本非零退出、查数工具报表或列不存在属于 A/B/C，不是 E；只有超时、401/403、429、5xx、连接失败、模型未配置等环境信号才归为 E。
2. 落点只能是知识包文档：objects/、operations/、rules/、metrics/ 下的 .md 文件，D 类可写 testcase/；写知识包内路径，不确定时留空。
3. 修订建议必须是普适业务知识：不得照抄评测问题或参考答案原文，不得出现“评测”“用例”“标准答案”“判分”等考核措辞；场景类知识写成覆盖一类场景的规则。
4. 每条建议写清依据（引用判分理由、工具失败、工具调用或文档缺口），具体简洁；不要编造文档中不存在的内容。可把同一根因的多个用例合并为一条。
5. tool_calls 是智能体按顺序的工具调用（n 为调用序号；input、output、error 为截断预览），空列表表示没有调用工具；tool_calls_omitted 为未附上的调用数。区分 B 与 C 时先看调用：检索到了哪些对象、查询了哪张表和哪些列、脚本参数取自哪里；依据中注明用例号与调用序号，如“用例 3 调用 #2”。没有 tool_calls 字段表示未采集，不代表没有调用。
6. 文档节选和文件清单只覆盖与本批用例相关的部分，清单中没有的文件不一定不存在。'''
SYSTEM_FEEDBACK = SYSTEM.replace('输入是一轮知识包评测中未通过的用例', '输入是终端用户在消费服务中实际问答后提交的反馈：错误问答（问题、期望答案或用户纠正、智能体回答、用户给出的原因、工具失败、工具调用记录）').replace(
    'D 用例问题：参考答案本身错误、过严或表述歧义', 'D 用户误判：用户的期望答案有误，或问题本身超出知识包范围').replace(
    '6. 文档节选', '6. overview.user_suggestions 是用户直接提出的修改建议（按 number 编号），可单独成条，在 suggestions 中写建议编号；与错误问答同一根因时合并。\n7. 文档节选')
MERGE_SYSTEM = '''你是知识包评测优化分析员。输入是同一轮评测分批归因得到的条目和各批总体判断。
合并同一根因的条目（类别相同，且落点相同或依据指向同一缺口），合并其用例编号；保留依据与建议要点，不得新增输入中没有的用例编号、落点或结论，也不得丢弃任何用例编号；再写一段覆盖全部批次的总体判断。用 submit_suggestions 提交。'''
SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['summary', 'items'], 'properties': {
    'summary': {'type': 'string'},
    'items': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
              'required': ['cases', 'category', 'evidence', 'suggestion'], 'properties': {
                  'cases': {'type': 'array', 'items': {'type': 'integer'}},
                  'suggestions': {'type': 'array', 'items': {'type': 'integer'}},
                  'category': {'type': 'string', 'enum': list(CATEGORIES)},
                  'evidence': {'type': 'string'}, 'suggestion': {'type': 'string'},
                  'targets': {'type': 'array', 'items': {'type': 'string'}}}}},
    'notes': {'type': 'array', 'items': {'type': 'string'}}}}


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def failed_cases(run):
    """Numbered (1-based, as in the report) failed, partial and error cases, most severe first."""
    found = []
    for index, case in enumerate(run['cases'], 1):
        verdict, reason = outcome(case)
        if verdict in SEVERITY:
            found.append((SEVERITY[verdict], index, case, verdict, reason))
    found.sort(key=lambda item: item[:2])
    return [item[1:] for item in found]


def case_input(number, case, verdict, reason, stored, limit):
    """Model input of one failed case within ``limit`` characters; recorded tool calls are trimmed first."""
    item = {'number': number, 'verdict': verdict, 'question': clip(case.get('question'), 800),
            'reference': clip(case.get('reference'), 1200), 'answer': clip(case.get('answer'), 1200),
            'reason': clip(reason, 500), 'tool_failures': tool_failure_lines(case.get('trace'))[:6]}
    if size(item) > limit:
        item.update(reference=clip(case.get('reference'), 300), answer=clip(case.get('answer'), 300),
                    tool_failures=item['tool_failures'][:2])
    if isinstance(stored, dict):
        recorded = [call for call in stored.get('calls') or [] if isinstance(call, dict)]
        failed = [call for call in recorded if call.get('status') == 'failed']
        attached = next((calls for calls in (failed_first(recorded, CASE_CALLS), failed[:CASE_CALLS])
                         if size({**item, 'tool_calls': calls}) <= limit), [])
        if attached or not recorded:
            item['tool_calls'] = attached
        omitted = len(recorded) + int(stored.get('omitted') or 0) - len(attached)
        if omitted:
            item['tool_calls_omitted'] = omitted
    return item


def terms(texts):
    """Chinese character bigrams and longer ASCII words used to rank package documents."""
    found = set()
    for text in texts:
        for run in re.findall(r'[一-鿿]+', text):
            found.update(run[i:i + 2] for i in range(len(run) - 1))
        found.update(word.lower() for word in re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', text))
    return found


def tool_text(case, stored):
    """Text of a case's recorded tool calls and tool failures: tool names, scripts, tables, columns and errors."""
    calls = [call for call in (stored or {}).get('calls') or [] if isinstance(call, dict)]
    return ' '.join([str(call.get(key) or '') for call in calls for key in ('tool', 'input', 'output', 'error', 'code')]
                    + tool_failure_lines(case.get('trace')))


def case_terms(case, reason, tools):
    """Weighted terms of one case: question, reference, judge reason and tool calls count twice, the answer once."""
    primary = terms([' '.join((str(case.get('question') or ''), str(case.get('reference') or ''), reason, tools))])
    return primary, terms([str(case.get('answer') or '')]) - primary


def score(wanted, found):
    primary, secondary = wanted
    return 2 * len(primary & found) + len(secondary & found)


def stem(name):
    return name.rsplit('/', 1)[-1][:-len('.md')]


class Package:
    """Markdown documents of the evaluated package, ranked against the terms of the cases being analysed."""

    def __init__(self, raw):
        files, _ = unpack(raw)
        self.docs = {name: data.decode('utf-8', 'replace') for name, data in files.items()
                     if name.endswith('.md') and name.startswith(tuple(WORKSPACE_DIRS))}
        self.terms = {name: terms([text[:20000]]) for name, text in self.docs.items()}
        self.names = sorted(name for name in files if name.endswith('.md'))
        mapping = files.get('mappings/object-data-mappings.json')
        self.mapping = mapping.decode('utf-8', 'replace') if mapping else ''
        try:
            objects = json.loads(self.mapping).get('objects') if self.mapping else None
        except ValueError:
            objects = None
        self.objects = objects if isinstance(objects, dict) else {}
        # Tables an object reads, so a query of that table names the object's document.
        self.tables = {}
        for key, item in self.objects.items():
            item = item if isinstance(item, dict) else {}
            sources = [s.get('resource') for s in item.get('sources') or [] if isinstance(s, dict)]
            self.tables[f'objects/{key}.md'] = {str(t) for t in [item.get('resource'), *sources] if t}

    def named(self, text):
        """Non-index documents a case's tool calls refer to by file name or by a table their object reads."""
        words = {word.lower() for word in re.findall(r'[A-Za-z0-9_]+', text)}

        def mentioned(value):
            return value.lower() in words if value.isascii() else value in text

        found = {name for name in self.docs if not name.endswith('/index.md') and mentioned(stem(name))}
        found.update(name for name, tables in self.tables.items() if name in self.docs and any(map(mentioned, tables)))
        return found

    def anchor(self, wanted, named):
        """The document a case relates to most; cases with one anchor are analysed together."""
        if named:
            return min(named)
        scored = [(-score(wanted, found), name) for name, found in self.terms.items()
                  if not name.endswith('/index.md') and score(wanted, found)]
        return min(scored)[1] if scored else ''

    def excerpt(self, name, wanted, limit):
        """A document within ``limit`` characters: its beginning and best-matching sections, in document order."""
        text = self.docs[name]
        if len(text) <= limit:
            return text
        head, *sections = [part for part in re.split(r'(?m)^(?=#{1,6}\s)', text) if part.strip()] or [text]
        if not sections:
            return clip(text, limit)
        head = clip(head, min(SECTION_HEAD, limit))
        ranked = sorted((-score(wanted, terms([part])), i) for i, part in enumerate(sections))
        picked, used = set(), len(head)
        for value, i in ranked:
            if value == 0 or used >= limit:
                break
            if used + len(sections[i]) <= limit or not picked:
                picked.add(i)
                used += min(len(sections[i]), limit - used)
        parts, previous = [head], -1
        for i in sorted(picked):
            parts.append(('' if i == previous + 1 else '…\n') + clip(sections[i], max(limit - len(head), 0)))
            previous = i
        return clip(''.join(parts), limit)

    def documents(self, wanted, named, budget):
        """Index documents within their share, then documents tool calls name, then the rest by relevance."""
        chosen, used = [], 0
        indexes = sorted((-score(wanted, found), name) for name, found in self.terms.items() if name.endswith('/index.md'))
        index_budget = int(budget * INDEX_SHARE)
        for _, name in indexes:
            room = min(INDEX_CHARS, index_budget - used)
            if room < 200:
                break
            content = self.excerpt(name, wanted, room)
            chosen.append({'path': name, 'content': content})
            used += len(content)
        # Unrelated documents come last and only while room remains, so a small package is sent whole.
        others = sorted((name not in named, -score(wanted, found), name) for name, found in self.terms.items()
                        if not name.endswith('/index.md'))
        for _, _, name in others:
            room = min(DOCUMENT_CHARS, budget - used)
            if room < SECTION_HEAD:
                break
            content = self.excerpt(name, wanted, room)
            chosen.append({'path': name, 'content': content})
            used += len(content)
        return chosen

    def mapping_for(self, paths, budget):
        """The data mapping; when it exceeds ``budget``, only the objects of the documents sent with it."""
        if len(self.mapping) <= budget or not self.objects:
            return clip(self.mapping, budget)
        kept, omitted = {}, 0
        for key, item in self.objects.items():
            if f'objects/{key}.md' in paths and size({**kept, key: item}) <= budget:
                kept[key] = item
            else:
                omitted += 1
        return json.dumps({'objects': kept, 'omitted_objects': omitted}, ensure_ascii=False)

    def files(self, first, budget):
        """File names within ``budget`` characters, the documents sent with the request first."""
        listed, used = [], 0
        for name in list(first) + [name for name in self.names if name not in first]:
            if used + len(name) + 4 > budget:
                break
            listed.append(name)
            used += len(name) + 4
        return listed


def plan(entries, request_chars, evidence):
    """Batches whose cases fit the case share of one request; cases with the same anchor stay together."""
    budget = int(request_chars * SHARES['cases'])
    groups = {}
    for entry in entries:  # Severity order, so groups follow their most severe case.
        groups.setdefault(entry['anchor'] or f"#{entry['number']}", []).append(entry)
    batches, current, used = [], [], 0
    for group in groups.values():
        for entry in group:
            item = case_input(entry['number'], entry['case'], entry['verdict'], entry['reason'],
                              (evidence or {}).get(entry['number'] - 1), budget)
            if current and used + size(item) > budget:
                batches.append(current)
                current, used = [], 0
            current.append((entry, item))
            used += size(item)
    if current:
        batches.append(current)
    return batches


def request_payload(batch, overview, package, tables, request_chars):
    wanted = (set().union(*(entry['terms'][0] for entry, _ in batch)), set().union(*(entry['terms'][1] for entry, _ in batch)))
    named = set().union(*(entry['named'] for entry, _ in batch))
    chosen = package.documents(wanted, named, int(request_chars * SHARES['documents']))
    paths = [doc['path'] for doc in chosen]
    return {'overview': overview, 'cases': [item for _, item in batch],
            'package_files': package.files(paths, int(request_chars * SHARES['files'])),
            'documents': chosen, 'data_mapping': package.mapping_for(set(paths), int(request_chars * SHARES['mapping'])),
            'data_tables': clip(json.dumps(tables, ensure_ascii=False), int(request_chars * SHARES['tables']))}


def valid_items(value, numbers, proposals=None):
    """Items of a model answer with a known category, their case numbers limited to the cases it was given and, when
    ``proposals`` is given, their user suggestion numbers to those suggestions."""
    items = []
    for item in value.get('items') or []:
        if isinstance(item, dict) and item.get('category') in CATEGORIES:
            cases = sorted({n for n in item.get('cases') or [] if isinstance(n, int) and n in numbers})
            proposed = sorted({n for n in item.get('suggestions') or []
                               if isinstance(n, int) and (proposals is None or n in proposals)})
            items.append({**item, 'cases': cases, 'suggestions': proposed})
    return items


# Wording of an assessment, and concrete values that must not be copied from a single case into the package.
ASSESSMENT = ('评测', '用例', '标准答案', '判分')
CONCRETE = re.compile(r'\d{4,}|\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2})?|[A-Za-z]{1,6}-?\d{4,}')
COPIED = 20


def common(left, right):
    """Length of the longest text two strings share."""
    best, previous = 0, [0] * (len(right) + 1)
    for a in left:
        current = [0]
        for j, b in enumerate(right, 1):
            current.append(previous[j - 1] + 1 if a == b else 0)
        best = max(best, current[-1])
        previous = current
    return best


def overfit(item, cases):
    """Why a suggestion may only fit the cases it came from, or '' when it reads as general knowledge.

    Checked by program, for both sources: it must cite a case or user suggestion, must not carry a case's own numbers,
    dates or document numbers, must not copy a reference answer or a judge reason, and must not speak of the assessment."""
    text = str(item.get('suggestion') or '')
    if not item['cases'] and not item.get('suggestions'):
        return '没有引用任何用例或用户建议'
    sources = [cases[n] for n in item['cases'] if n in cases]
    values = {match for case in sources for match in CONCRETE.findall(f"{case.get('question', '')} {case.get('reference', '')}")}
    copied = [value for value in values if value in text]
    if copied:
        return f"写入了个例中的具体值 {'、'.join(sorted(copied)[:3])}"
    for case in sources:
        for origin in (case.get('reference') or '', case.get('reason') or ''):
            if origin and common(text[:600], origin[:1500]) > COPIED:
                return '与标准答案或判分理由大段相同'
    words = [word for word in ASSESSMENT if word in text]
    if words:
        return f"出现考核措辞「{'、'.join(words)}」"
    return ''


def covered(items):
    return {n for item in items for n in item['cases']}


def numbered(numbers):
    shown = '、'.join(map(str, sorted(numbers)[:20]))
    return shown + (f' 等 {len(numbers)} 条' if len(numbers) > 20 else '')


def workspace_path(target, known):
    target = target.strip().strip('`').lstrip('./')
    for package_dir, workspace_dir in WORKSPACE_DIRS.items():
        if target.startswith(workspace_dir):
            target = package_dir + target[len(workspace_dir):]
        if target.startswith(package_dir) and target.endswith('.md') and '..' not in target:
            note = '' if target in known or package_dir == 'testcase/' else '（新建）'
            return f'`{workspace_dir}{target[len(package_dir):]}`{note}'
    return ''


def flat(text, limit):
    """Model text on one line: the viewer shows raw HTML as text, so line breaks a model wrote as <br> become spaces."""
    return ' '.join(clip(LINE_BREAK.sub(' ', str(text or '')), limit).split())


def row(number, item):
    """One table row of an entry; carried rows of earlier runs are written the same way under a new number."""
    cited = [*map(str, item['cases']), *(f'建议 {n}' for n in item.get('suggestions') or [])]
    return (f"| {number} | {'、'.join(cited) or '—'} | {CATEGORIES.get(item['category'], item['category'])} | "
            f"{item['evidence'].replace('|', chr(92) + '|')} | {item['suggestion'].replace('|', chr(92) + '|')} | "
            f"{'、'.join(item.get('targets') or []) or '—'} |")


def table_rows(run, value, analysed, known):
    """Table rows of a model answer, final as rendered: generalization checked, targets as workspace paths.

    Kept with the run so later runs can carry rows modeling has not handled without asking the model again."""
    numbers = set(analysed)
    proposals = {n for n, _ in enumerate(user_suggestions(run), 1)}
    cases = {number: {'question': case.get('question'), 'reference': case.get('reference'), 'reason': outcome(case)[1]}
             for number, case in enumerate(run['cases'], 1)}
    rows = []
    for item in valid_items(value, numbers, proposals)[:MAX_ITEMS]:
        review = overfit(item, cases)
        rows.append({'cases': item['cases'], 'suggestions': item['suggestions'], 'category': item['category'],
                     'evidence': flat(item.get('evidence'), 400),
                     'suggestion': (f'⚠ 需人工复核：{review}；' if review else '') + flat(item.get('suggestion'), 600),
                     'targets': [path for path in (workspace_path(str(t), known) for t in (item.get('targets') or [])[:5])
                                 if path]})
    return rows


def render(run, stem, now, value, analysed, known, model, traced=0, batches=1, skipped=(), failed=(), slow=False):
    return document(run, stem, now, value, table_rows(run, value, analysed, known), analysed, model, traced, batches,
                    skipped, failed, slow)


def document(run, stem, now, value, rows, analysed, model, traced=0, batches=1, skipped=(), failed=(), slow=False):
    numbers = set(analysed)
    proposals = {n for n, _ in enumerate(user_suggestions(run), 1)}
    feedback = run.get('origin') == 'consumer_feedback'
    base = run.get('base_ref') or run.get('package_ref') or {}
    covered_text = (f'> 覆盖：{len(numbers)} 条错误问答、{len(proposals)} 条用户建议' if feedback
                    else f'> 覆盖用例：{len(numbers)} 条失分或异常用例')
    lines = [f"# 评测优化建议 — {datetime.fromtimestamp(now, CHINA).strftime('%Y-%m-%d %H:%M')}", '',
             f"> 依据评测报告：reports/{stem}.md（轮次 {run['id']}，知识包版本 {base.get('version', '')}）",
             f"> 生成方式：评测服务调用判分模型 {model} 按{'用户反馈' if feedback else '失分用例'}、工具调用记录与知识包内容自动归因；本建议仅作 /ontology-evolution 的归因初稿，须在建模工作区逐条复核后再实施。",
             covered_text + (f'，分 {batches} 批分析后合并' if batches > 1 else '')
             + (f'；其中 {traced} 条附带工具调用记录' if traced else '')]
    if skipped:
        lines.append(f'> 未纳入：用例 {numbered(skipped)} 超出单次生成上限')
    if failed:
        lines.append(f'> 未纳入：用例 {numbered(failed)} 所在批次'
                     + ('判分模型响应超时（思考时间过长），可点击“重新生成”重试，或换用响应更快的判分模型' if slow
                        else '生成失败，可点击“重新生成”重试'))
    lines += ['', '## 总体判断', '', flat(value.get('summary'), 1500) or '（无）', '',
              '## 失分归因与修订建议', '', '| # | 用例 | 归因 | 依据 | 建议修订 | 落点 |', '|---|---|---|---|---|---|']
    lines += [row(count, item) for count, item in enumerate(rows, 1)]
    if not rows:
        raise Failure('suggestions_protocol', '判分模型未返回有效的优化建议', 422, 'suggestions')
    lines += ['', '## 说明', '']
    lines += [f"- {flat(note, 300)}" for note in (value.get('notes') or [])[:10] if isinstance(note, str) and note.strip()]
    lines += ['- 用例编号对应评测报告「逐用例明细」中的序号' + ('，“建议 n”对应「用户建议」中的序号。' if feedback else '。'),
              '- 标“需人工复核”的条目未通过泛化检查（未引用依据、写入个例数值、照抄答案或出现考核措辞），实施前请先确认。',
              '- 落点为建模工作区路径，由知识包路径换算（objects/ → ontology/object/ 等）；标“新建”的文件当前包内不存在，属于结构性缺口，建议先走对应提取命令。',
              '- 修订须遵守泛化红线：只写普适业务知识，不得照抄用例问题或标准答案。', '']
    return '\n'.join(lines)


def passed(run, stem, now, summary):
    return '\n'.join([f"# 评测优化建议 — {datetime.fromtimestamp(now, CHINA).strftime('%Y-%m-%d %H:%M')}", '',
                      f"> 依据评测报告：reports/{stem}.md（轮次 {run['id']}）", '',
                      f"本轮评测没有失败、部分正确或异常的用例（通过 {summary['pass']} 条，待人工判定 {summary['manual']} 条），无需评测优化。", ''])


class Suggestions:
    def __init__(self, judge, packages, http, request_chars=REQUEST_CHARS, clock=time.monotonic):
        self.judge, self.packages, self.http, self.request_chars, self.clock = judge, packages, http, request_chars, clock

    def ask(self, current, profile, system, payload, timeout=TIMEOUT):
        response = self.http.json('POST', current['url'], '/chat/completions', token=current['key'], timeout=timeout, json={
            'model': profile['model'], 'messages': [{'role': 'system', 'content': system},
                                                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
            'tools': [{'type': 'function', 'function': {'name': 'submit_suggestions', 'parameters': SCHEMA}}],
            'tool_choice': {'type': 'function', 'function': {'name': 'submit_suggestions'}}})
        try:
            call = response['choices'][0]['message']['tool_calls'][0]['function']
            value = json.loads(call['arguments'])
            if call['name'] != 'submit_suggestions' or not isinstance(value, dict):
                raise ValueError()
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise Failure('suggestions_protocol', '判分模型未返回有效的优化建议', 422, 'suggestions') from exc
        return value

    def merge(self, current, profile, results, limit, spare, timeout):
        """One answer for several batches: the model joins shared root causes, else the batches are concatenated."""
        items = [{'cases': item['cases'], 'suggestions': item['suggestions'], 'category': item['category'],
                  'evidence': clip(item.get('evidence'), 400),
                  'suggestion': clip(item.get('suggestion'), 600), 'targets': [str(t) for t in (item.get('targets') or [])[:5]]}
                 for numbers, value in results for item in valid_items(value, numbers)]
        summaries = [' '.join(clip(value.get('summary'), 600).split()) for _, value in results]
        notes = []
        for _, value in results:
            notes += [note for note in value.get('notes') or [] if isinstance(note, str)]
        joined = {'summary': '；'.join(s for s in summaries if s), 'items': items, 'notes': notes}
        payload = {'batch_summaries': summaries, 'items': items}
        if spare < 1 or timeout < MIN_REQUEST_SECONDS or size(payload) > limit:
            return joined
        try:
            merged = self.ask(current, profile, MERGE_SYSTEM, payload, timeout)
        except Failure:
            return joined
        # A merge may join cases but must not lose any of them.
        if covered(valid_items(merged, covered(items))) < covered(items):
            return joined
        return {**merged, 'notes': notes + [n for n in merged.get('notes') or [] if isinstance(n, str)]}

    def generate(self, owner, run, token, stem, now, data_tables, evidence=None, progress=None):
        """Suggestions markdown, the model used and the table rows. Raises Failure with a user-facing reason.

        ``owner`` selects the judge configuration: the run's owner, or the package owner generating a feedback run's.

        ``evidence`` maps zero-based case indexes to the tool calls recorded when each case settled; ``progress``
        is called with the batch being waited for, or with ``merging`` before batches are joined."""
        deadline = self.clock() + GENERATION_SECONDS
        report = progress or (lambda value: None)
        cases = failed_cases(run)
        current = self.judge.settings.effective(owner)
        profile = self.judge.profile(current)
        if not token:
            raise Failure('login_required', '生成优化建议需要读取知识包，请重新登录后点击“重新生成”', 401, 'suggestions')
        try:
            raw, _ = self.packages.fetch(run['package_ref'], token)
        except Failure as exc:
            if exc.status == 401:
                # Hub refused the user's login, typically expired while the suggestions waited in the queue.
                raise Failure('login_required', '登录已过期，请重新登录后点击“重新生成”', 401, 'suggestions') from exc
            raise
        package = Package(raw)
        overview = summarize(run)
        entries = []
        for number, case, verdict, reason in cases[:MAX_CANDIDATES]:
            tools = tool_text(case, (evidence or {}).get(number - 1))
            wanted, named = case_terms(case, reason, tools), package.named(tools)
            entries.append({'number': number, 'case': case, 'verdict': verdict, 'reason': reason,
                            'terms': wanted, 'named': named, 'anchor': package.anchor(wanted, named)})
        limit = self.request_chars
        planned = plan(entries, limit, evidence)
        proposals = [{'number': n, 'content': clip(item.get('content'), 1000), 'scope': item.get('scope') or ''}
                     for n, item in enumerate(user_suggestions(run), 1)]
        if proposals:
            overview = {**overview, 'user_suggestions': proposals}
            if not planned:
                planned = [[]]  # Only suggestions: one request carries them.
        system = SYSTEM_FEEDBACK if run.get('origin') == 'consumer_feedback' else SYSTEM
        queue = planned[:MAX_BATCHES]
        skipped = [entry['number'] for batch in planned[MAX_BATCHES:] for entry, _ in batch]
        skipped += [number for number, *_ in cases[MAX_CANDIDATES:]]
        results, failed, errors, retried, sent, requests, slow = [], [], [], set(), {}, 0, False
        while queue:
            batch = queue.pop(0)
            numbers = tuple(entry['number'] for entry, _ in batch)
            payload = request_payload(batch, overview, package, data_tables, limit)
            if size(payload) > limit and len(batch) > 1:
                # Planned before an overflow lowered the limit; JSON escaping alone may not split it.
                replanned = plan([entry for entry, _ in batch], limit, evidence)
                if len(replanned) > 1:
                    queue[:0] = replanned
                    continue
            remaining = deadline - self.clock()
            if requests >= MAX_REQUESTS or remaining < MIN_REQUEST_SECONDS:
                failed += numbers
                slow = slow or remaining < MIN_REQUEST_SECONDS
                continue
            requests += 1
            report({'batch': len(results) + 1, 'batches': len(results) + 1 + len(queue)})
            try:
                value = self.ask(current, profile, system, payload, min(TIMEOUT, remaining))
            except Failure as exc:
                timed_out = exc.code == 'dependency_timeout'
                needs_smaller = exc.context_overflow or timed_out
                # Too long for the model, or it did not finish thinking in time: send these cases in smaller requests.
                if needs_smaller and limit > MIN_REQUEST_CHARS:
                    limit = max(MIN_REQUEST_CHARS, min(limit, size(payload)) // 2)
                    queue[:0] = plan([entry for entry, _ in batch], limit, evidence)
                    slow = slow or timed_out
                    continue
                if not needs_smaller and exc.status in (429, 502, 503) and numbers not in retried:
                    retried.add(numbers)
                    queue.insert(0, batch)
                    continue
                failed += numbers
                errors.append(exc)
                slow = slow or timed_out
                continue
            results.append((set(numbers), value))
            sent.update((entry['number'], item) for entry, item in batch)
        if not results:
            if any(error.context_overflow for error in errors):
                raise Failure('suggestions_context', '判分模型的上下文不足以分析单条失分用例，请换用上下文更大的判分模型后重新生成',
                              422, 'suggestions')
            if slow:
                raise Failure('suggestions_timeout', SLOW_MODEL, 504, 'suggestions')
            raise errors[0] if errors else Failure('suggestions_budget', '优化建议的模型请求次数已用尽，请稍后重新生成', 503, 'suggestions')
        if len(results) > 1:
            report({'batch': len(results), 'batches': len(results), 'merging': True})
            value = self.merge(current, profile, results, limit, MAX_REQUESTS - requests,
                               min(TIMEOUT, deadline - self.clock()))
        else:
            value = results[0][1]
        traced = sum(1 for item in sent.values() if item.get('tool_calls'))
        rows = table_rows(run, value, sorted(sent), set(package.names))
        return document(run, stem, now, value, rows, sorted(sent), profile['model'], traced, len(results), skipped,
                        sorted(failed), slow), profile, rows
