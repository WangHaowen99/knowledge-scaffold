"""Coalesced background recovery, independent of request worker threads."""
import fcntl
import logging
import math
import threading
import time

logger = logging.getLogger(__name__)


class Maintenance:
    def __init__(self, store, interval=60):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError('Maintenance interval must be positive')
        self.store = store
        self.interval = interval
        self.condition = threading.Condition()
        self.stopping = False
        self.pending = False
        self.running = False
        self.thread = threading.Thread(target=self._run, name='hub-maintenance', daemon=True)

    def start(self):
        self.thread.start()

    def notify(self):
        with self.condition:
            if not self.stopping and not self.running:
                self.pending = True
                self.condition.notify()

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify()
        self.thread.join()

    def _run(self):
        earliest = time.monotonic()
        due = earliest + self.interval
        while True:
            with self.condition:
                while not self.stopping:
                    remaining = (earliest if self.pending else due) - time.monotonic()
                    if remaining <= 0:
                        break
                    self.condition.wait(remaining)
                if self.stopping:
                    return
                self.pending = False
                self.running = True
            try:
                self.run_once()
            except Exception:
                # Never log stored records, credentials or exception text/paths.
                logger.error('Hub background maintenance failed')
            finally:
                with self.condition:
                    self.running = False
                    earliest = time.monotonic() + self.interval
                    due = earliest

    def run_once(self):
        # The persistent lock file is never removed. Its timestamp also prevents
        # different processes from immediately repeating a completed scan.
        with (self.store.root / '.maintenance.lock').open('a+') as task:
            try:
                fcntl.flock(task, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                task.seek(0)
                try:
                    previous = float(task.read().strip() or '0')
                except ValueError:
                    previous = 0
                if 0 <= time.time() - previous < self.interval:
                    return
                try:
                    with self.store.lock(blocking=False):
                        try:
                            self.store.recover_all()
                        finally:
                            task.seek(0)
                            task.truncate()
                            task.write(str(time.time()))
                            task.flush()
                except BlockingIOError:
                    # A writer is active; the periodic wakeup retries without
                    # accumulating tasks or blocking application shutdown.
                    return
            finally:
                fcntl.flock(task, fcntl.LOCK_UN)


class QueryMaintenance:
    """Notify only after the final response body has been sent, including errors."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def sent(message):
            await send(message)
            if (message['type'] == 'http.response.body' and not message.get('more_body', False)
                    and scope.get('hub_query_complete')):
                worker = getattr(scope['app'].state, 'hub_maintenance', None)
                if worker is not None:
                    worker.notify()
        await self.app(scope, receive, sent)
