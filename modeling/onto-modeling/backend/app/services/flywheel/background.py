"""Runs the flywheels of users who are away, on the delegations they registered.

Notifications land in the local evaluation index and assistant turns end on this server, so a periodic
round is enough to pick both up. A run that is already optimizing is still settled without any
delegation: finishing a turn and checking its result are local. That holds even for users the rounds no
longer act for (a former owner after the project changed hands), since such a run keeps the other users'
flywheels waiting (store.others_working).
"""
import asyncio
import logging

from .. import ownership
from ..workspace import WorkspaceError
from . import grants, service, store
from .assistant import Assistant
from .caller import Caller

_log = logging.getLogger(__name__)
ROUND_SECONDS = 15
# 通知丢失时靠对账补上：约每 10 分钟一次
RECONCILE_ROUNDS = 40


def _caller(manager, assistant, pid, state):
    identity = state['identity']
    delegated = {name: grants.credential(state, name) for name in grants.SERVICES}
    running = store.active(state) is not None
    if not identity or not (running or any(delegated.values())):
        return None
    try:
        meta = manager.get_project(pid)
    except WorkspaceError as exc:
        # 读不到元数据（503）只是暂时的：本轮跳过；项目确实不在了（404）才撤销登记
        if exc.status == 404 and any(delegated.values()):
            grants.withdraw(pid, identity['subject'], '项目已删除')
        return None  # 项目已删除：不再代为执行
    # 不沿用全局管理员身份：后台只代项目所有者执行（单人所有权）
    if any(delegated.values()) and not grants.acts_for(manager, meta, identity['username']):
        # 不再代为执行就撤销登记：评测服务保存的登录不能一直续期到授权到期
        grants.withdraw(pid, identity['subject'], '项目已归档' if meta.get('archived') else '你已不是该项目的所有者')
        delegated = dict.fromkeys(grants.SERVICES)
        if not running:
            return None
    if meta.get('archived') or not ownership.owns(meta, identity['username']):
        if not store.holds_workspace(state):
            return None
        # 不再代为执行，但改动工作区的运行照样收尾：不带任何凭据，也不启动下一步
        return Caller(manager, pid, identity['subject'], identity['username'], None, False, assistant)
    return Caller(manager, pid, identity['subject'], identity['username'], delegated['evaluation'], True,
                  assistant, hub_delegation=delegated['hub'])


def run_round(manager, assistant, reconcile=False):
    for pid, state in store.all_states():
        try:
            grants.retry_revocations(pid, state)
        except Exception:  # 撤销重试出错不影响本轮其他工作，下一轮再试
            _log.warning('flywheel revocation retry failed', exc_info=True)
        try:
            # 选定身份时可能撤销登记（grants.withdraw），出错同样只影响这一个飞轮
            caller = _caller(manager, assistant, pid, state)
            if caller is not None:
                service.advance(caller, reconcile)
        except Exception:  # 一个飞轮出错不影响其他用户，下一轮重试
            _log.warning('flywheel background round failed', exc_info=True)


async def scheduler_loop(manager):
    assistant = Assistant(asyncio.get_running_loop())
    rounds = 0
    while True:
        await asyncio.sleep(ROUND_SECONDS)
        rounds += 1
        try:
            await asyncio.to_thread(run_round, manager, assistant, rounds % RECONCILE_ROUNDS == 0)
        except Exception:
            _log.warning('flywheel background round failed', exc_info=True)
