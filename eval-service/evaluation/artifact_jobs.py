"""Generate a run's artifacts when it stops, and announce each new state to onto-modeling.

Rendering runs outside the run's two execution workers because suggestions wait on a model. Requests for
the same run coalesce: while one is being generated, further requests only leave the newest state to redo.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

from . import artifacts as output
from .core import Failure, canonical
from .suggestions import failed_cases, passed

META = 'meta'
PUBLIC = ('revision', 'status', 'stem', 'generated_at', 'report', 'suggestions', 'suggestions_error', 'suggestions_model',
          'progress')


class ArtifactJobs:
    def __init__(self, store, suggestions, notifier):
        self.store, self.suggestions, self.notifier = store, suggestions, notifier
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='evaluation-artifacts')
        self.guard = threading.Lock()
        self.pending, self.running = {}, set()

    def meta(self, owner, run_id):
        _, body = self.store.artifact(owner, run_id, META)
        return json.loads(body) if body else None

    def view(self, owner, run_id):
        value = self.meta(owner, run_id)
        return {key: value.get(key) for key in PUBLIC} if value else None

    def schedule(self, owner, run_id, token, force=False):
        """Queue generation. The token is only kept in memory, until the job has read the package."""
        with self.guard:
            force = force or self.pending.get(run_id, {}).get('force', False)
            self.pending[run_id] = dict(owner=owner, token=token, force=force)
            if run_id in self.running:
                return
            self.running.add(run_id)
        self.pool.submit(self._drain, run_id)

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

    def regenerate(self, owner, run_id, token):
        """Show generating at once, so pollers keep waiting for the new revision."""
        meta = self.meta(owner, run_id)
        if meta:
            meta.update(status='generating', suggestions='generating', suggestions_error=None, progress=None)
            self.store.put_artifacts(owner, run_id, meta['revision'], {META: canonical(meta)})
        self.schedule(owner, run_id, token, force=True)
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

    def generate(self, run_id, owner, token, force=False):
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
        stem = output.new_stem(run_id, now)
        summary = output.summarize(run)
        cases = failed_cases(run)
        meta = dict(revision=revision, status='generating' if cases else 'ready', stem=stem, generated_at=now, report='ready',
                    suggestions='generating' if cases else 'ready', suggestions_error=None, suggestions_model=None,
                    fingerprint=fingerprint, summary=summary)
        values = {'report.md': output.fit('report.md', output.render_report(run, stem, now)),
                  'answers.json': output.render_answers(run),
                  'context.json': output.render_context(self.store, run, stem, revision, summary), META: canonical(meta)}
        if not cases:
            values['suggestions.md'] = passed(run, stem, now, summary)
        self.store.put_artifacts(owner, run_id, revision, values)
        self.announce(owner, run, meta)
        if not cases:
            return
        started = time.time()

        def progress(value):
            # Shown on the run page while the judge model works, with how long this generation has waited.
            meta['progress'] = {**value, 'started_at': started}
            self.store.put_artifacts(owner, run_id, revision, {META: canonical(meta)})
        try:
            data_tables = output.tables(self.store, run)
            text, profile = self.suggestions.generate(owner, run, token, stem, now, data_tables,
                                                      self.store.evidence(owner, run_id), progress)
            meta.update(status='ready', suggestions='ready', suggestions_model=profile['model'], progress=None)
            values = {'suggestions.md': output.fit('suggestions.md', text), META: canonical(meta)}
        except Failure as exc:
            meta.update(status='failed', suggestions='failed', suggestions_error=exc.message, progress=None)
            values = {META: canonical(meta)}
        # Jobs of one run are serialized, so a newer request rewrites these values after this one.
        self.store.put_artifacts(owner, run_id, revision, values)
        self.announce(owner, run, meta)

    def announce(self, owner, run, meta):
        base = run.get('base_ref') or {}
        ran = run.get('package_ref') or {}
        self.notifier.enqueue(owner, run['id'], dict(
            ontology_id=base.get('ontology_id') or ran.get('ontology_id'),
            release_id=base.get('release_id') or ran.get('release_id'), version=base.get('version') or ran.get('version'),
            status=run['status'], created_at=run.get('created_at'), finished_at=run.get('finished_at') or run.get('updated_at'),
            artifacts={key: meta.get(key) for key in PUBLIC}, summary=meta['summary']))

    def recover(self):
        """A restart abandons suggestions in progress; say so instead of showing them as generating forever."""
        with self.store.lock:
            rows = self.store.db.execute("SELECT run_id,owner FROM artifacts WHERE name=? AND json_extract(body,'$.status')='generating'",
                                         (META,)).fetchall()
        for row in rows:
            self._abandon(row['owner'], row['run_id'], '服务重启中断了优化建议生成，请点击“重新生成”')

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
