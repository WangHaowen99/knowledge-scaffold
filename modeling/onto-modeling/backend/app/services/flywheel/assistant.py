"""The project assistant session as the flywheel uses it.

Sessions live on the event loop while the flywheel runs in worker threads; calls cross over with
run_coroutine_threadsafe. A run remembers its conversation and where the history stood when the
command was sent, and its outcome is read from the events after that point.

The command goes in the way a page command does (S-20): the same meta (display text, page origin, command
name), the same duplicate-submission guard and autobuild guard of SessionManager.submit. The assistant
edits a changeset copy; the turn counts as running until the platform merged it into the mainline
(workspace_busy), so the checks that follow see the revised content.
"""
import asyncio
from pathlib import Path

from .. import agent_sessions
from ..agent_sessions import SessionError
from ..workspace import WorkspaceError
from . import store

SEND_TIMEOUT = 60
INTERRUPT_TIMEOUT = 30
# 本轮结束的标志：先出现的那个决定结果
TERMINAL = ('result', 'session_interrupted', 'session_error')
COMMAND = '/ontology-evolution'
COMMAND_LABEL = '评测优化'
ORIGIN = {'kind': 'page', 'page': 'flywheel', 'label': '知识包数据飞轮'}


def _unavailable(exc):
    code = getattr(exc, 'code', '') or ''
    if code == 'message_duplicate':
        # 同一条命令刚交给助手（连点、两个页面同时确认）：和助手正忙一样等下一轮
        return WorkspaceError('助手正在处理相同的命令，请等本轮结束后再优化', 409, store.ASSISTANT_BUSY_CODE)
    if code == 'autobuild_running':
        return WorkspaceError('自主构建运行中，请等其结束后再优化', 409, store.ASSISTANT_BUSY_CODE)
    return WorkspaceError(str(exc), int(getattr(exc, 'status', 503) or 503), 'assistant_unavailable')


class Assistant:
    def __init__(self, loop, sessions=None):
        self.loop = loop
        self.sessions = sessions or agent_sessions.manager

    def _call(self, coroutine, timeout):
        from ...api.errors import ApiError  # 发起一轮的守卫（自主构建、配额）抛契约错误

        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)
        except (SessionError, ApiError) as exc:
            raise _unavailable(exc) from exc

    def busy(self, pid):
        """The assistant works on the project: a turn, queued messages, a command chain, an unmerged changeset, or a
        running autobuild."""
        from ..autobuild import store as autobuild_store

        return bool(self.sessions.workspace_busy(pid) or autobuild_store.active(pid))

    def start(self, pid, cwd, text, username):
        if not (Path(cwd) / '.claude' / 'commands' / f'{COMMAND[1:]}.md').is_file():
            raise WorkspaceError('评测优化命令没有装配，请在能力管理中重新装配后再试', 409, 'pack_not_enabled',
                                 {'command': COMMAND})
        try:
            conversation = self.sessions.resolve_conversation(pid)
            start = len(self.sessions.get_history(pid, conversation))
        except SessionError as exc:
            raise WorkspaceError(str(exc), exc.status, 'assistant_unavailable') from exc
        meta = {'display_text': f'{COMMAND_LABEL}（知识包数据飞轮）', 'origin': dict(ORIGIN), 'command': COMMAND,
                'command_label': COMMAND_LABEL}
        session = self._call(self.sessions.send(pid, cwd, text, conversation, username=username, meta=meta),
                             SEND_TIMEOUT)
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
