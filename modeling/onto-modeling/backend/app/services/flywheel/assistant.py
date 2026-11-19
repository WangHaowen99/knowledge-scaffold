"""The project assistant session as the flywheel uses it.

Sessions live on the event loop while the flywheel runs in worker threads; calls cross over with
run_coroutine_threadsafe. A run remembers its conversation and where the history stood when the
command was sent, and its outcome is read from the events after that point.
"""
import asyncio

from .. import agent_sessions
from ..agent_sessions import SessionError
from ..workspace import WorkspaceError

SEND_TIMEOUT = 60
INTERRUPT_TIMEOUT = 30
# 本轮结束的标志：先出现的那个决定结果
TERMINAL = ('result', 'session_interrupted', 'session_error')


class Assistant:
    def __init__(self, loop, sessions=None):
        self.loop = loop
        self.sessions = sessions or agent_sessions.manager

    def _call(self, coroutine, timeout):
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)
        except SessionError as exc:
            raise WorkspaceError(str(exc), exc.status, 'assistant_unavailable') from exc

    def busy(self, pid):
        session = self.sessions.get(pid)
        return bool(session and (agent_sessions._busy(session) or session.pending_prompts))

    def start(self, pid, cwd, text, username):
        try:
            conversation = self.sessions.resolve_conversation(pid)
            start = len(self.sessions.get_history(pid, conversation))
        except SessionError as exc:
            raise WorkspaceError(str(exc), exc.status, 'assistant_unavailable') from exc
        session = self._call(self.sessions.send(pid, cwd, text, conversation, username=username), SEND_TIMEOUT)
        return {'conversation_id': session.conversation_id, 'history_start': start}

    def outcome(self, pid, conversation, start):
        """running, finished, failed or interrupted (also when the engine went away mid-turn)."""
        session = self.sessions.get(pid)
        if session and session.conversation_id == conversation and self.busy(pid):
            return 'running'
        try:
            events = self.sessions.get_history(pid, conversation)[start:]
        except SessionError:
            return 'interrupted'
        event = next((item for item in events if item.get('type') in TERMINAL), None)
        if event is None or event['type'] == 'session_interrupted':
            return 'interrupted'
        if event['type'] == 'session_error' or event.get('is_error'):
            return 'failed'
        return 'finished'

    def turn_ended(self, pid, conversation, start):
        """Whether the turn sent at ``start`` has ended, whatever else the session runs now; None when the
        history cannot be read."""
        try:
            events = self.sessions.get_history(pid, conversation)[start:]
        except SessionError:
            return None
        return any(item.get('type') in TERMINAL for item in events)

    def interrupt(self, pid, conversation):
        self._call(self.sessions.interrupt(pid, conversation), INTERRUPT_TIMEOUT)
