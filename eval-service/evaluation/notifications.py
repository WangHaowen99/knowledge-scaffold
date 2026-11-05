"""Completion notifications to onto-modeling: at-least-once delivery from a SQLite outbox.

A row is written before delivery is attempted and leaves ``pending`` only when modeling confirms it, so a
restart or a modeling outage delays a notification but cannot lose it. Modeling deduplicates by run and
sequence, and treats the notification as a hint: reports are always read back with the user's login.
"""
import threading
import time
import uuid

from .core import Failure, canonical

PATH = '/api/internal/evaluation/notifications'
MAX_BACKOFF = 3600
IDLE_WAIT = 30
BATCH = 20
# The request itself is wrong and resending cannot help; anything else may recover (outage, upgrade, credentials).
REJECTED = (400, 413, 422)
# Usually a wrong modeling address or notification token: a few attempts, then the user is told to check them.
MISCONFIGURED = (401, 403, 404)
MISCONFIGURED_ATTEMPTS = 3
# Other failures are retried with backoff up to this many deliveries in all, about a day; then they are failed.
MAX_ATTEMPTS = 30


def backoff(attempts):
    return min(MAX_BACKOFF, 30 * 2 ** min(attempts, 10))


class Notifier:
    def __init__(self, config, store, http):
        self.config, self.store, self.http = config, store, http
        self.max_attempts = config.number('notification_max_attempts', MAX_ATTEMPTS)
        self.wake, self.stopping = threading.Event(), threading.Event()
        self.thread = None

    @property
    def enabled(self):
        return bool(self.config.modeling_url and self.config.modeling_service_token)

    def enqueue(self, owner, run_id, body, recipient=None):
        """Queue the latest state of a run; it replaces any older notification still waiting for that run.

        ``recipient`` is the user modeling files the notification under when it is not the run's owner."""
        if not self.enabled:
            return
        with self.store.transaction():
            row = self.store.db.execute('SELECT max(sequence) FROM notifications WHERE run_id=?', (run_id,)).fetchone()
            sequence = (row[0] or 0) + 1
            self.store.db.execute("UPDATE notifications SET status='superseded' WHERE run_id=? AND status='pending'", (run_id,))
            now = time.time()
            value = dict(body, event_id=f'{run_id}:{sequence}', sequence=sequence, run_id=run_id, owner=recipient or owner)
            self.store.db.execute('INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                  (uuid.uuid4().hex, run_id, owner, sequence, canonical(value), 'pending', 0, now, None, now, None))
        self.wake.set()

    def status(self, run_id):
        with self.store.lock:
            row = self.store.db.execute('SELECT status,attempts,next_attempt,last_error,delivered FROM notifications '
                                        'WHERE run_id=? ORDER BY sequence DESC LIMIT 1', (run_id,)).fetchone()
        if not row:
            return {'status': 'none' if self.enabled else 'disabled'}
        return dict(status=row['status'], attempts=row['attempts'], next_attempt=row['next_attempt'] if row['status'] == 'pending' else None,
                    last_error=row['last_error'], delivered_at=row['delivered'])

    def retry(self, owner, run_id):
        """Send the latest notification of a run again now, also after modeling rejected it or delivery gave up."""
        if not self.enabled:
            raise Failure('notification_disabled', '未配置建模通知凭据，无法发送通知', 409)
        with self.store.transaction():
            row = self.store.db.execute('SELECT id,status FROM notifications WHERE run_id=? AND owner=? ORDER BY sequence DESC LIMIT 1',
                                        (run_id, owner)).fetchone()
            if not row:
                raise Failure('notification_missing', '此任务还没有可发送的通知', 409)
            if row['status'] != 'delivered':
                self.store.db.execute("UPDATE notifications SET status='pending',attempts=0,next_attempt=? WHERE id=?",
                                      (time.time(), row['id']))
        self.wake.set()
        return self.status(run_id)

    def deliver_due(self, now=None):
        """Send every notification that is due; returns how many were attempted."""
        now = now or time.time()
        with self.store.lock:
            rows = self.store.db.execute("SELECT id,body,attempts FROM notifications WHERE status='pending' AND next_attempt<=? "
                                         'ORDER BY next_attempt LIMIT ?', (now, BATCH)).fetchall()
        for row in rows:
            self._deliver(row['id'], row['body'], row['attempts'])
        return len(rows)

    def _deliver(self, rid, body, attempts):
        try:
            self.http.json('POST', self.config.modeling_url, PATH, limit=65536, timeout=15,
                           headers={'X-Evaluation-Service-Token': self.config.modeling_service_token,
                                    'Content-Type': 'application/json'}, content=body.encode())
        except Failure as exc:
            status = exc.upstream_status
            attempts += 1
            if status in REJECTED:
                outcome = 'rejected'
            elif attempts >= (MISCONFIGURED_ATTEMPTS if status in MISCONFIGURED else self.max_attempts):
                outcome = 'failed'
            else:
                outcome = 'pending'
            error = f'建模服务返回 {status}' if status else '建模服务不可达'
            if outcome == 'failed' and status in MISCONFIGURED:
                error += '，请检查评测的建模服务地址和通知凭据'
            self._finish(rid, "status=?,attempts=?,next_attempt=?,last_error=?",
                         (outcome, attempts, time.time() + backoff(attempts - 1), error))
        else:
            self._finish(rid, 'status=?,attempts=?,last_error=NULL,delivered=?', ('delivered', attempts + 1, time.time()))

    def _finish(self, rid, assignments, values):
        # A row superseded while it was being sent stays superseded; delivery of a newer row follows.
        with self.store.transaction():
            self.store.db.execute(f"UPDATE notifications SET {assignments} WHERE id=? AND status='pending'", (*values, rid))

    def _wait(self):
        with self.store.lock:
            row = self.store.db.execute("SELECT min(next_attempt) FROM notifications WHERE status='pending'").fetchone()
        due = row[0] - time.time() if row and row[0] is not None else IDLE_WAIT
        return min(IDLE_WAIT, max(0.05, due))

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.deliver_due()
            except Exception:
                pass  # Rows stay pending; the next pass retries them. Never kill the delivery thread.
            self.wake.wait(self._wait())
            self.wake.clear()

    def start(self):
        if self.enabled and not self.thread:
            self.thread = threading.Thread(target=self._loop, name='evaluation-notifier', daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=20)
