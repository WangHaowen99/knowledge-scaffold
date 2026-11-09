"""Generate a run's artifacts when it stops, and announce each new state to onto-modeling.

Reports, answers and context are rendered within seconds and never wait behind optimization suggestions,
which may wait on a model for half an hour: each has its own small worker pool. Requests for the same run
coalesce: while one is being generated, further requests only leave the newest state to redo. A suggestion
result is written only while its report revision is still the newest, so a later report is never mixed with
older suggestions.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

from . import artifacts as output, modeling_result
from .core import Failure, canonical
from .execution import expiry
from .feedback.report import render as render_feedback
from .suggestions import failed_cases, passed

META = 'meta'
PUBLIC = ('revision', 'status', 'stem', 'generated_at', 'report', 'suggestions', 'suggestions_error', 'suggestions_model',
          'progress', 'suggestions_stale')
LOGIN_EXPIRED = '登录已过期，请重新登录后点击“重新生成”'


def access(token):
    """The current access token of a login handed over as a string or as a renewing source."""
    return token() if callable(token) else token


class ArtifactJobs:
    def __init__(self, store, suggestions, notifier):
        self.store, self.suggestions, self.notifier = store, suggestions, notifier
        self.reports = ThreadPoolExecutor(max_workers=2, thread_name_prefix='evaluation-reports')
        self.drafts = ThreadPoolExecutor(max_workers=2, thread_name_prefix='evaluation-suggestions')
        self.guard = threading.Lock()
        self.pending, self.running = {}, set()
        self.waiting, self.drafting, self.queue = {}, set(), deque()
        # Runs built from consumer feedback reach modeling only after a package owner confirmed them, and only when
        # the deployment says modeling files notifications by source.
        self.notify_feedback = False

    def meta(self, owner, run_id):
        _, body = self.store.artifact(owner, run_id, META)
        return json.loads(body) if body else None

    def view(self, owner, run_id):
        value = self.meta(owner, run_id)
        if not value:
            return None
        shown = {key: value.get(key) for key in PUBLIC}
        with self.guard:
            if run_id in self.waiting and run_id not in self.drafting:
                # Suggestions not started yet: the run page shows how many generations are ahead of this one.
                shown['queue'] = next((i for i, rid in enumerate(self.queue) if rid == run_id), 0)
        return shown

    def schedule(self, owner, run_id, token, force=False, suggestions=True, actor=None):
        """Queue generation. The login is only kept in memory, until the suggestions have read the package.

        ``token`` is an access token or a function returning a renewed one. ``suggestions=False`` rebuilds the
        report only, after a verdict changed: suggestions stay and are marked stale until the user regenerates them.
        ``actor`` is whose judge configuration drafts the suggestions when that is not the run's owner, as for a
        package owner generating them for a feedback run."""
        with self.guard:
            previous = self.pending.get(run_id, {})
            self.pending[run_id] = dict(owner=owner, token=token, force=force or previous.get('force', False),
                                        suggestions=suggestions or previous.get('suggestions', False),
                                        actor=actor or previous.get('actor'))
            if run_id in self.running:
                return
            self.running.add(run_id)
        self.reports.submit(self._drain, run_id)

    def _drain(self, run_id):
        while True:
            with self.guard:
                job = self.pending.pop(run_id, None)
                if not job:
                    self.running.discard(run_id)
                    return
            try:
                self.generate(run_id, **job)
            except Exception:
                # Stored state tells the user to regenerate; no run data or dependency detail in logs.
                self._abandon(job['owner'], run_id, '生成失败，请点击“重新生成”')

    def regenerate(self, owner, run_id, token, actor=None):
        """Show generating at once, so pollers keep waiting for the new revision."""
        meta = self.meta(owner, run_id)
        if meta:
            meta.update(status='generating', suggestions='generating', suggestions_error=None, progress=None,
                        suggestions_stale=False)
            self.store.put_artifacts(owner, run_id, meta['revision'], {META: canonical(meta)})
        self.schedule(owner, run_id, token, force=True, actor=actor)
        return self.view(owner, run_id)

    def _abandon(self, owner, run_id, reason):
        meta = self.meta(owner, run_id)
        if not meta or meta['status'] != 'generating':
            return
        meta.update(status='failed', suggestions='failed', suggestions_error=reason, progress=None)
        try:
            run = self.store.get(owner, run_id, 'runs')
            self.store.put_artifacts(owner, run_id, meta['revision'], {META: canonical(meta)})
        except Failure:
            return  # Deleted meanwhile.
        self.announce(owner, run, meta)

    def generate(self, run_id, owner, token, force=False, suggestions=True, actor=None):
        try:
            run = self.store.get(owner, run_id, 'runs')
        except Failure:
            return  # Deleted while queued.
        if run['status'] in output.ACTIVE or not output.has_results(run):
            return
        fingerprint = output.fingerprint(run)
        previous = self.meta(owner, run_id) or {}
        if not force and previous.get('fingerprint') == fingerprint:
            return
        now = time.time()
        revision = previous.get('revision', 0) + 1
        cases = failed_cases(run) or output.user_suggestions(run)
        summary = output.summarize(run)
        # A report-only rebuild keeps the file name, so the kept suggestions still name the report they came from.
        keep = not suggestions and previous.get('suggestions') in ('ready', 'failed', 'pending') and bool(previous.get('stem'))
        stem = previous['stem'] if keep else output.new_stem(run_id, now)
        meta = dict(revision=revision, status='generating' if cases else 'ready', stem=stem, generated_at=now, report='ready',
                    suggestions='generating' if cases else 'ready', suggestions_error=None, suggestions_model=None,
                    fingerprint=fingerprint, summary=summary, suggestions_stale=False)
        render = render_feedback if run.get('origin') == 'consumer_feedback' else output.render_report
        values = {'report.md': output.fit('report.md', render(run, stem, now)),
                  'answers.json': output.render_answers(run),
                  'context.json': output.render_context(self.store, run, stem, revision, summary)}
        draft = bool(cases)
        if draft and token is None:
            # A sealed feedback run has no user's login to read the package: a package owner asks for its suggestions.
            draft = False
            meta.update(status='ready', suggestions='pending')
        if not cases:
            values['suggestions.md'] = passed(run, stem, now, summary)
        elif keep:
            draft = False
            _, body = self.store.artifact(owner, run_id, 'suggestions.md')
            had_failures = any((previous.get('summary') or {}).get(key) for key in ('fail', 'partial', 'error'))
            meta.update(status='ready', suggestions='pending', suggestions_model=previous.get('suggestions_model'),
                        suggestions_stale=True)
            if body and previous.get('suggestions') == 'ready' and had_failures:
                values['suggestions.md'] = body
                meta['suggestions'] = 'ready'
        values[META] = canonical(meta)
        self.store.put_artifacts(owner, run_id, revision, values)
        modeling_result.started(self.store, owner, run, now)
        self.announce(owner, run, meta)
        if draft:
            self._queue(owner, run_id, token, revision, actor)

    def _queue(self, owner, run_id, token, revision, actor=None):
        with self.guard:
            self.waiting[run_id] = dict(owner=owner, token=token, revision=revision, actor=actor)
            if run_id in self.queue or run_id in self.drafting:
                return
            self.queue.append(run_id)
        self.drafts.submit(self._draft_next)

    def _draft_next(self):
        with self.guard:
            if not self.queue:
                return
            run_id = self.queue.popleft()
            self.drafting.add(run_id)
        try:
            while True:
                with self.guard:
                    job = self.waiting.pop(run_id, None)
                    if not job:
                        return
                try:
                    self.draft(run_id, **job)
                except Exception:
                    self._abandon(job['owner'], run_id, '优化建议生成失败，请点击“重新生成”')
        finally:
            with self.guard:
                self.drafting.discard(run_id)

    def draft(self, run_id, owner, token, revision, actor=None):
        """Suggestions for one report revision; discarded when a newer report was written meanwhile."""
        try:
            run = self.store.get(owner, run_id, 'runs')
        except Failure:
            return
        meta = self.meta(owner, run_id)
        if not meta or meta['revision'] != revision:
            return
        started = time.time()

        def current():
            return (self.meta(owner, run_id) or {}).get('revision') == revision

        def progress(value):
            # Shown on the run page while the judge model works, with how long this generation has waited.
            if current():
                meta['progress'] = {**value, 'started_at': started}
                self.store.put_artifacts(owner, run_id, revision, {META: canonical(meta)})
        try:
            login = access(token)
            if not login or 0 < expiry(login) <= time.time() + 5:
                raise Failure('login_required', LOGIN_EXPIRED, 401, 'suggestions')
            data_tables = output.tables(self.store, run)
            text, profile = self.suggestions.generate(actor or owner, run, login, meta['stem'], meta['generated_at'], data_tables,
                                                      self.store.evidence(owner, run_id), progress)
            meta.update(status='ready', suggestions='ready', suggestions_model=profile['model'], progress=None)
            values = {'suggestions.md': output.fit('suggestions.md', text), META: canonical(meta)}
        except Failure as exc:
            meta.update(status='failed', suggestions='failed', suggestions_error=exc.message, progress=None)
            values = {META: canonical(meta)}
        if not current():
            return
        self.store.put_artifacts(owner, run_id, revision, values)
        self.announce(owner, run, meta)

    def announce(self, owner, run, meta):
        state = self.store.optimization(run['id'])
        recipient = None
        if run.get('origin') == 'consumer_feedback':
            if not self.notify_feedback or not state or state.get('status') in ('awaiting_review', 'dismissed'):
                return
            # Modeling files notifications by user; the owner who confirmed the run sees it there.
            recipient = state.get('confirmed_by')
        base = run.get('base_ref') or {}
        ran = run.get('package_ref') or {}
        self.notifier.enqueue(owner, run['id'], dict(
            ontology_id=base.get('ontology_id') or ran.get('ontology_id'),
            release_id=base.get('release_id') or ran.get('release_id'), version=base.get('version') or ran.get('version'),
            status=run['status'], created_at=run.get('created_at'), finished_at=run.get('finished_at') or run.get('updated_at'),
            artifacts={key: meta.get(key) for key in PUBLIC}, summary=meta['summary'], origin=run.get('origin') or 'evaluation',
            optimization=modeling_result.public(state)), recipient)

    def recover(self):
        """A restart abandons suggestions in progress; say so instead of showing them as generating forever."""
        with self.store.lock:
            rows = self.store.db.execute("SELECT run_id,owner FROM artifacts WHERE name=? AND json_extract(body,'$.status')='generating'",
                                         (META,)).fetchall()
        for row in rows:
            self._abandon(row['owner'], row['run_id'], '服务重启中断了优化建议生成，请点击“重新生成”')

    def close(self):
        self.reports.shutdown(wait=False, cancel_futures=True)
        self.drafts.shutdown(wait=False, cancel_futures=True)
