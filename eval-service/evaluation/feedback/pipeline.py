"""Turn received consumer feedback into feedback runs, in one background thread.

Each pass attributes received signals to a package version, keeps only the newest vote per consumer run,
confirms user corrections, rewrites follow-up questions into standalone ones, groups signals about the same
question into clusters, decides which clusters have enough information for modeling, takes ready clusters into
the package version's collecting feedback run, and seals runs that are full or idle. A sealed run gets its
report like an evaluation run; its suggestions wait for a package owner, who reads the package with their login.

Model steps use the shared default judge configuration, as feedback belongs to no user. While it is not
available, corrections wait; a follow-up question is then kept as asked and matched by fingerprint only.
"""
import hashlib
import json
import re
import threading
import time
import unicodedata
from datetime import datetime

from ..artifacts import CHINA, failed_first
from ..core import Failure, transient
from ..execution import EVIDENCE_CALLS, preview
from .store import LEVELS, FeedbackStore, run_owner

SIGNALS_PER_PASS = 20
TICK_SECONDS = 60
SAMPLES = 3
DAY = 86400
RECENT_DAYS = 7
CANDIDATES = 20
# Error codes and rules that point at the environment, not at the package: such answers are not given to modeling.
ENVIRONMENT = re.compile(r'TIMEOUT|UNAVAILABLE|CONNECT|RATE_?LIMIT|UNAUTHORI[SZ]ED|FORBIDDEN|SERVICE_RESTARTED|QUOTA|'
                         r'MODEL_(?:UNAVAILABLE|UNCONFIGURED|ERROR)|\b5\d\d\b', re.IGNORECASE)
DETECTED = {'user_vote': '用户投票', 'user_correction': '追问纠错', 'system_rule': '系统识别'}
REASONS = {'wrong_answer': '答案错误', 'missing_data': '数据不对或缺失', 'wrong_process': '流程或口径不对', 'other': '其他'}
CORRECTION = '''你判断用户的追问是否在纠正智能体上一轮的回答。输入：上一轮问题 question、上一轮回答 answer、用户追问 follow_up、追问后的回答 next_answer。
is_correction：用户是否否定或纠正了上一轮回答；correct_statement：用户给出的正确说法，原文或简洁转述，没有给出时为空字符串。用 submit_correction 提交。'''
REWRITE = '''把多轮对话中的最后一个问题改写成可以独立理解的完整问题：只补全上文省略的对象、时间范围和口径，不改变意思，不回答问题。
输入 context 是之前几轮的用户问题，question 是最后一个问题。用 submit_question 提交。'''
MATCH = '''判断新问题与已有问题簇是否在问同一件事。candidates 是已有问题簇的编号和代表问题。
relation：duplicate（同一个问题，换了说法）、more_specific（新问题是某个已有问题更具体的版本）、unrelated（都不是）。
匹配时 cluster 填对应编号，否则为空字符串。用 submit_match 提交。'''


class Paused(Exception):
    """A model step cannot run now; the signal waits for the next pass."""


def fingerprint(text):
    """Lower case without white space and punctuation, so trivially different wordings of one question meet."""
    text = unicodedata.normalize('NFKC', text or '').lower()
    kept = ''.join(ch for ch in text if not unicodedata.category(ch).startswith(('P', 'Z', 'S', 'C')))
    return hashlib.sha256(kept.encode()).hexdigest()[:32]


def environment(signal):
    """Whether a system-detected error only shows the environment failed: timeouts, outages, credentials."""
    rules = (signal.get('error_detection') or {}).get('rules') or []
    codes = [rule.split(':', 1)[1] for rule in rules if rule.startswith('error_code:')]
    code = ((signal.get('qa') or {}).get('error_code') or '')
    found = codes + ([code] if code else [])
    return bool(found) and all(ENVIRONMENT.search(item) for item in found)


class Assistant:
    """The model steps of feedback processing, on the shared default judge configuration."""

    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    def ask(self, system, payload, tool, properties):
        if not self.settings.default_enabled():
            raise Paused()
        try:
            current = self.settings.effective()
        except Failure as exc:
            raise Paused() from exc
        if not all((current['url'], current['key'], current['model'])):
            raise Paused()
        schema = {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
        try:
            response = self.http.json('POST', current['url'], '/chat/completions', token=current['key'], timeout=60, json={
                'model': current['model'], 'messages': [{'role': 'system', 'content': system},
                                                        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                'tools': [{'type': 'function', 'function': {'name': tool, 'parameters': schema}}],
                'tool_choice': {'type': 'function', 'function': {'name': tool}}})
        except Failure as exc:
            if transient(exc):
                raise Paused() from exc
            raise
        try:
            call = response['choices'][0]['message']['tool_calls'][0]['function']
            value = json.loads(call['arguments'])
            if call['name'] != tool or not isinstance(value, dict):
                raise ValueError()
            return value
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise Failure('feedback_model_protocol', '判分模型未返回有效的结构化结果', 422, 'feedback') from exc

    def correction(self, qa, correction):
        value = self.ask(CORRECTION, {'question': qa.get('question', ''), 'answer': (qa.get('answer') or '')[:2000],
                                      'follow_up': correction.get('text', ''), 'next_answer': correction.get('next_answer', '')},
                         'submit_correction', {'is_correction': {'type': 'boolean'}, 'correct_statement': {'type': 'string'}})
        return bool(value.get('is_correction')), str(value.get('correct_statement') or '').strip()[:4000]

    def rewrite(self, question, context):
        try:
            value = self.ask(REWRITE, {'context': context, 'question': question}, 'submit_question', {'question': {'type': 'string'}})
        except (Paused, Failure):
            return question  # Kept as asked: matching then relies on its fingerprint.
        rewritten = str(value.get('question') or '').strip()
        return rewritten[:4000] or question

    def match(self, question, candidates):
        try:
            value = self.ask(MATCH, {'question': question, 'candidates': [
                {'cluster': cluster['cluster_id'], 'question': cluster['body'].get('question', '')} for cluster in candidates]},
                'submit_match', {'relation': {'type': 'string', 'enum': ['duplicate', 'more_specific', 'unrelated']},
                                 'cluster': {'type': 'string'}})
        except (Paused, Failure):
            return None
        known = {cluster['cluster_id'] for cluster in candidates}
        return value.get('cluster') if value.get('relation') in ('duplicate', 'more_specific') and value.get('cluster') in known else None


class Pipeline:
    def __init__(self, config, store, assistant, schedule, clock=time.time):
        self.config, self.store, self.assistant, self.schedule, self.clock = config, store, assistant, schedule, clock
        self.signals = FeedbackStore(store)
        # One writer of clusters and collecting runs: the background pass, an owner sealing a run or deleting one.
        self.lock = threading.RLock()
        self.wake_event, self.stopping = threading.Event(), threading.Event()
        self.thread = None

    @property
    def enabled(self):
        return self.config.flag('feedback_enabled')

    def setting(self, name, default):
        return self.config.number(name, default)

    def start(self):
        if self.enabled and not self.thread:
            self.thread = threading.Thread(target=self._loop, name='evaluation-feedback', daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=20)

    def wake(self):
        self.wake_event.set()

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.process()
            except Exception:
                pass  # Signals stay received and are processed again in the next pass.
            self.wake_event.wait(TICK_SECONDS)
            self.wake_event.clear()

    def process(self):
        """One pass over received signals, ready clusters and collecting runs."""
        with self.lock:
            now = self.clock()
            for row in self.signals.received(SIGNALS_PER_PASS):
                if self.stopping.is_set():
                    return
                try:
                    self.normalize(row, now)
                except Paused:
                    continue
                except Failure as exc:
                    self.signals.mark(row['signal_id'], now, processing_status='failed', status_reason=exc.code)
            self.assemble(now)
            self.seal_due(now)

    # Signals

    def normalize(self, row, now):
        signal = self.signals.body(row)
        if signal is None:
            self.signals.mark(row['signal_id'], now, processing_status='failed', status_reason='purged')
            return
        package = signal.get('package') or {}
        ontology, version = package.get('ontology_id'), package.get('version') or ''
        if not ontology:
            # The question was not answered from one knowledge package; kept until retention ends.
            self.signals.mark(row['signal_id'], now, processing_status='unassigned')
            return
        if row['signal_type'] == 'user_suggestion':
            self.suggestion(row, signal, ontology, version or self.signals.latest_version(ontology), now)
            return
        qa, vote = signal.get('qa') or {}, signal.get('feedback') or {}
        detected = (signal.get('error_detection') or {}).get('detected_by')
        derived, reason = {}, None
        if row['signal_type'] == 'turn_feedback':
            replaced = self.signals.vote(row['run_key'] or row['signal_id'], row['actor_key'], row['signal_seq'],
                                         vote['vote'], vote.get('reason_category'), row['signal_id'], now)
            if replaced is False:
                self.signals.mark(row['signal_id'], now, processing_status='superseded')
                return
            if replaced:
                self.withdraw(replaced, now)
            if vote['vote'] == 'none':
                self.signals.mark(row['signal_id'], now, processing_status='withdrawn')
                return
            level = 'positive' if vote['vote'] == 'up' else self.vote_level(vote)
            if vote.get('expected_answer'):
                derived['reference'] = vote['expected_answer']
        elif detected == 'user_correction':
            corrected, statement = self.assistant.correction(qa, signal.get('correction') or {})
            if not corrected:
                self.signals.mark(row['signal_id'], now, processing_status='not_correction')
                return
            level = 'L3' if statement else 'L2'
            derived['correction'] = (signal.get('correction') or {}).get('text', '')
            if statement:
                derived['reference'] = statement
        elif detected == 'system_rule':
            level = 'L2'
            reason = 'environment' if environment(signal) else None
        else:
            level = self.vote_level(vote) if vote else 'L2'
            if vote.get('expected_answer'):
                derived['reference'] = vote['expected_answer']
        question = qa.get('question', '')
        if qa.get('conversation_context'):
            question = self.assistant.rewrite(question, qa['conversation_context'])
        derived['question'] = question
        mark = fingerprint(question)
        cluster_id = self.qa_cluster(ontology, version, mark, question, positive=level == 'positive', now=now)
        self.signals.mark(row['signal_id'], now, derived=derived, processing_status='clustered', level=level,
                          status_reason=reason, question_fingerprint=mark, cluster_id=cluster_id,
                          ontology_id=ontology, package_version=version)
        self.refresh(cluster_id, now)

    @staticmethod
    def vote_level(vote):
        if vote.get('expected_answer'):
            return 'L3'
        return 'L2' if vote.get('reason_category') or vote.get('comment') else 'L1'

    def withdraw(self, signal_id, now):
        with self.store.lock:
            row = self.store.db.execute('SELECT cluster_id FROM feedback_signals WHERE signal_id=?', (signal_id,)).fetchone()
        self.signals.mark(signal_id, now, processing_status='superseded')
        if row and row['cluster_id']:
            self.refresh(row['cluster_id'], now)

    def qa_cluster(self, ontology, version, mark, question, positive, now):
        found = self.signals.by_fingerprint(ontology, version, 'qa', mark)
        if found:
            return found['cluster_id']
        if not positive:
            candidates = self.signals.open_clusters(ontology, version, 'qa', CANDIDATES)
            matched = self.assistant.match(question, candidates) if candidates else None
            if matched:
                return matched
        return self.signals.create_cluster(ontology, version, 'qa', mark, {'question': question}, now)

    def suggestion(self, row, signal, ontology, version, now):
        content = signal['suggestion']['content'].strip()
        hint = signal['suggestion'].get('scope_hint') or {}
        scope = (f"operation:{hint['operation_key']}" if hint.get('operation_key')
                 else f"entity:{hint['entity_key']}" if hint.get('entity_key') else '')
        mark = fingerprint(scope) if scope else fingerprint(content)
        found = self.signals.by_fingerprint(ontology, version, 'suggestion', mark)
        cluster_id = found['cluster_id'] if found else self.signals.create_cluster(
            ontology, version, 'suggestion', mark, {'scope': scope, 'question': content[:200]}, now)
        self.signals.mark(row['signal_id'], now, derived={'content': content}, processing_status='clustered', level='L3',
                          question_fingerprint=mark, cluster_id=cluster_id, ontology_id=ontology, package_version=version)
        self.refresh(cluster_id, now)

    # Clusters

    def refresh(self, cluster_id, now):
        """Recount a cluster from its signals and decide whether it is ready for modeling."""
        cluster = self.signals.cluster(cluster_id)
        if not cluster:
            return
        members = self.signals.members(cluster_id)
        negative = [m for m in members if m['level'] in ('L1', 'L2', 'L3')]
        window = now - self.setting('feedback_window_days', 7) * DAY
        recent = [m for m in negative if m['received'] >= window]
        down = sum(1 for m in members if m['signal_type'] == 'turn_feedback' and m['vote'] == 'down')
        up = sum(1 for m in members if m['vote'] == 'up')
        errors = sum(1 for m in members if m['signal_type'] == 'error_qa')
        suggestions = sum(1 for m in members if m['signal_type'] == 'user_suggestion')
        expected = any(m['level'] == 'L3' and m['signal_type'] != 'user_suggestion' for m in members)
        users = len({m['actor_key'] for m in negative})
        latest = max([m['received'] for m in members] or [cluster['latest_signal']])
        level = max((m['level'] for m in members), key=lambda item: LEVELS.get(item, 0), default='positive')
        priority = (3 * users + down + 2 * errors + 5 * int(expected) + 3 * suggestions - up
                    + (3 if now - latest < RECENT_DAYS * DAY else 0))
        status, reason = cluster['status'], cluster['status_reason']
        if status != 'included':
            status, reason = self.triage(cluster, negative, recent)
        body = dict(cluster['body'], mostly_upvoted=bool(down) and up >= 2 * down)
        self.signals.update_cluster(cluster_id, body, level=level, status=status, status_reason=reason, distinct_users=users,
                                    downvotes=down, upvotes=up, error_count=errors, suggestion_count=suggestions,
                                    has_expected_answer=int(expected), priority_score=priority, latest_signal=latest)

    def triage(self, cluster, negative, recent):
        if not negative:
            return 'open', None
        if all(m['status_reason'] == 'environment' for m in negative):
            return 'dismissed', 'runtime_error'
        if cluster['cluster_kind'] == 'suggestion' or any(m['level'] in ('L2', 'L3') for m in negative):
            return 'ready', None
        users = len({m['actor_key'] for m in recent})
        downvotes = sum(1 for m in recent if m['vote'] == 'down')
        if users >= self.setting('feedback_min_users', 3) or downvotes >= self.setting('feedback_min_downvotes', 5):
            return 'ready', None
        return 'open', 'insufficient_info'

    # Feedback runs

    def assemble(self, now):
        """Ready clusters join the collecting feedback run of their package version, which is created when needed."""
        for cluster in self.signals.clusters("status='ready' AND run_id IS NULL ORDER BY priority_score DESC LIMIT 50"):
            owner, run = self.collecting_run(cluster['ontology_id'], cluster['package_version'], now)
            self.signals.update_cluster(cluster['cluster_id'], status='included', run_id=run['id'])
            feedback = dict(run['feedback'], clusters=run['feedback']['clusters'] + [cluster['cluster_id']], last_included=now)
            self.store.save(owner, run['id'], dict(run, feedback=feedback))

    def collecting_run(self, ontology, version, now):
        owner = run_owner(ontology)
        for run in self.signals.feedback_runs(ontology, ('collecting',)):
            if (run.get('package_ref') or {}).get('version') == version:
                return owner, run
        ref = {'ontology_id': ontology, 'version': version}
        day = datetime.fromtimestamp(now, CHINA).strftime('%m-%d')
        run = self.store.create(owner, 'runs', dict(
            origin='consumer_feedback', status='collecting', phase='collecting', package_ref=ref, base_ref=dict(ref),
            source={'mode': 'none'}, cases=[], eval_set_name=f'消费反馈 · {day} 起', data_source_name='消费服务真实问答',
            created_at=now, judge=None, feedback={'clusters': [], 'last_included': now, 'stats': {}}))
        return owner, run

    def seal_due(self, now):
        clusters = self.setting('feedback_seal_clusters', 10)
        idle = self.setting('feedback_seal_idle_minutes', 60) * 60
        for owner, run in self.signals.collecting():
            feedback = run.get('feedback') or {}
            if len(feedback.get('clusters') or []) >= clusters or now - feedback.get('last_included', now) >= idle:
                self.seal(owner, run, 'auto', now)

    def seal(self, owner, run, by, now=None):
        """Stop collecting: turn the run's clusters into cases and statistics, then generate its report."""
        now = now or self.clock()
        with self.lock, self.store.lock:
            run = self.store.get(owner, run['id'], 'runs')
            if run['status'] != 'collecting':
                return run
            clusters = sorted((c for c in (self.signals.cluster(cid) for cid in run['feedback']['clusters']) if c),
                              key=lambda c: -c['priority_score'])
            cases, evidence, suggestions, members = [], {}, [], []
            for cluster in clusters:
                rows = self.signals.members(cluster['cluster_id'])
                members += rows
                if cluster['cluster_kind'] == 'suggestion':
                    latest = rows[-1] if rows else None
                    content = self.signals.derived(latest).get('content') if latest else ''
                    if content:
                        suggestions.append({'cluster_id': cluster['cluster_id'], 'content': content,
                                            'scope': cluster['body'].get('scope', ''), 'users': cluster['distinct_users']})
                    continue
                for row in self.samples(rows):
                    signal = self.signals.body(row)
                    if not signal:
                        continue
                    index = len(cases)
                    case, calls = self.case(index, cluster, row, signal)
                    cases.append(case)
                    if calls is not None:
                        evidence[index] = calls
            feedback = dict(run['feedback'], clusters=[c['cluster_id'] for c in clusters], suggestions=suggestions,
                            stats=self.statistics(members), excluded=self.excluded(run), sealed_at=now, sealed_by=by,
                            window={'from': min((m['received'] for m in members), default=now),
                                    'to': max((m['received'] for m in members), default=now)})
            run.update(status='completed', phase='finished', finished_at=now, updated_at=now, cases=cases, feedback=feedback,
                       eval_set_name='消费反馈 · ' + ' 至 '.join(sorted({datetime.fromtimestamp(feedback['window'][key], CHINA)
                                                                     .strftime('%m-%d') for key in ('from', 'to')})))
            run = self.store.save(owner, run['id'], run)
            for index, value in evidence.items():
                self.store.put_evidence(owner, run['id'], index, value)
            with self.store.transaction():
                # Later feedback on the same questions starts new clusters for the next feedback run.
                self.store.db.execute("UPDATE feedback_clusters SET fingerprint=fingerprint||':'||run_id WHERE run_id=? "
                                      "AND instr(fingerprint,':')=0", (run['id'],))
        self.schedule(owner, run['id'], None)
        return run

    @staticmethod
    def samples(rows):
        """Up to SAMPLES signals of a cluster: with an expected answer, with full evidence, newest, from different users."""
        candidates = [row for row in rows if row['level'] in ('L1', 'L2', 'L3')]
        ordered = sorted(candidates, key=lambda row: (row['level'] != 'L3', -row['received']))
        picked, users = [], set()
        for row in ordered:
            if row['actor_key'] in users:
                continue
            picked.append(row)
            users.add(row['actor_key'])
            if len(picked) == SAMPLES:
                break
        return picked

    def case(self, index, cluster, row, signal):
        derived = self.signals.derived(row)
        qa, vote = signal.get('qa') or {}, signal.get('feedback') or {}
        detected = (signal.get('error_detection') or {}).get('detected_by')
        if detected == 'user_correction':
            kind, reason = '追问纠错', f"用户纠正：“{derived.get('correction', '')}”"
        elif detected == 'system_rule':
            rules = '、'.join((signal.get('error_detection') or {}).get('rules') or []) or '错误答案'
            kind, reason = '系统识别', f'系统识别：{rules}'
        else:
            kind = '用户投票'
            reason = '用户反馈：' + REASONS.get(vote.get('reason_category'), '回答有误') + (f"（{vote['comment']}）" if vote.get('comment') else '')
        reference = derived.get('reference', '')
        verdict = 'fail' if row['signal_type'] == 'error_qa' or reference else 'partial'
        evidence = signal.get('evidence') or {}
        failures = [{'tool_name': item.get('tool') or '未知工具',
                     'error': ' '.join(part for part in (item.get('error_code'), item.get('message')) if part)}
                    for item in evidence.get('tool_failures') or []][:20]
        events = [event for event in evidence.get('tool_events') or [] if isinstance(event, dict)]
        case = {'id': str(index), 'question': derived.get('question') or qa.get('question', ''), 'reference': reference,
                'answer': qa.get('answer', ''), 'status': 'settled',
                'judgement': {'verdict': verdict, 'reason': reason[:4000], 'source': 'consumer_feedback'},
                'trace': {'status': 'complete' if evidence else 'unavailable', 'tool_calls': evidence.get('tool_event_total') or len(events),
                          'tool_failures': failures, **({} if evidence else {'error': '消费侧未附带工具调用证据'})},
                'feedback_meta': {'original_question': qa.get('question', '') if derived.get('question') != qa.get('question') else '',
                                  'kind': kind, 'users': cluster['distinct_users'], 'latest': cluster['latest_signal'],
                                  'cluster_id': cluster['cluster_id'], 'mostly_upvoted': bool(cluster['body'].get('mostly_upvoted'))}}
        if not events:
            return case, None
        calls = [{'n': event.get('index') or i + 1, 'tool': str(event.get('tool') or '')[:64],
                  'status': 'failed' if event.get('failed') else 'completed', 'input': preview(event.get('arguments')),
                  ('error' if event.get('failed') else 'output'): preview(event.get('result'))} for i, event in enumerate(events)]
        kept = failed_first(calls, EVIDENCE_CALLS)
        return case, {'calls': kept, 'omitted': max(len(events), evidence.get('tool_event_total') or 0) - len(kept)}

    def statistics(self, members):
        by_type, reasons, users = {}, {}, set()
        for row in members:
            if row['signal_type'] == 'turn_feedback':
                key = 'upvote' if row['vote'] == 'up' else 'vote_down'
            elif row['signal_type'] == 'error_qa':
                key = 'error_qa_' + (row['detected_by'] or 'system_rule')
            else:
                key = 'user_suggestion'
            by_type[key] = by_type.get(key, 0) + 1
            if row['level'] in ('L1', 'L2', 'L3'):
                users.add(row['actor_key'])
            if row['vote'] == 'down':
                signal = self.signals.body(row) or {}
                category = (signal.get('feedback') or {}).get('reason_category') or 'other'
                reasons[category] = reasons.get(category, 0) + 1
        return {'signals': len(members), 'users': len(users), 'by_type': by_type, 'reasons': reasons}

    def excluded(self, run):
        """Feedback on the run's package version that is not given to modeling, by reason."""
        ref = run.get('package_ref') or {}
        found = {}
        for cluster in self.signals.clusters("ontology_id=? AND package_version=? AND status IN ('dismissed','open') "
                                             "AND status_reason IS NOT NULL", (ref.get('ontology_id'), ref.get('version'))):
            count = len(self.signals.members(cluster['cluster_id']))
            found[cluster['status_reason']] = found.get(cluster['status_reason'], 0) + count
        return found

    def release(self, run_id, now=None):
        """A deleted feedback run returns its clusters; they join the next run of their package version."""
        with self.lock:
            for cluster_id in self.signals.release(run_id):
                self.refresh(cluster_id, now or self.clock())

    def prune(self, now=None):
        return self.signals.prune(now)
