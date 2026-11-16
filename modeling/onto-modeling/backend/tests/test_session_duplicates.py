"""Duplicate submission regressions using synthetic projects and fake engines."""

import asyncio

import pytest

from app.services.agent_sessions import SessionError, load_history
from test_session_pool import (
    FakeClient,
    _feed,
    _settle,
    _turn_result,
    pool_env,
    scripted_env,
)


COMMANDS = ["/ontology-extract-object", "/ontology-validate"]


def _messages(session):
    return [event["text"] for event in session.events if event["type"] == "user_message"]


async def _reject_duplicate(manager, cwd, command):
    with pytest.raises(SessionError) as duplicate:
        await asyncio.wait_for(
            manager.send("project_alpha", cwd, f"  {command}\n"), timeout=1
        )
    assert duplicate.value.status == 409


@pytest.mark.parametrize("command", COMMANDS)
def test_duplicate_rejected_before_first_message_is_persisted(pool_env, tmp_path, monkeypatch, command):
    manager, _, _ = pool_env

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        deliver = manager._deliver

        async def delayed_deliver(session, cwd, text, *args, **kwargs):
            entered.set()
            await release.wait()
            return await deliver(session, cwd, text, *args, **kwargs)

        monkeypatch.setattr(manager, "_deliver", delayed_deliver)
        first = asyncio.create_task(manager.send("project_alpha", tmp_path, command))
        try:
            await entered.wait()
            assert _messages(manager.get("project_alpha")) == []
            await _reject_duplicate(manager, tmp_path, command)
        finally:
            release.set()
            session = await first
            await _settle(manager, "project_alpha")
            await manager.shutdown()
        assert _messages(session) == [command]

    asyncio.run(scenario())


@pytest.mark.parametrize("command", COMMANDS)
def test_connecting_session_rejects_duplicate_even_while_state_is_idle(pool_env, tmp_path, monkeypatch, command):
    manager, _, _ = pool_env

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_connect(client):
            entered.set()
            await release.wait()
            client.connected = True

        monkeypatch.setattr(FakeClient, "connect", delayed_connect)
        session = await manager.send("project_alpha", tmp_path, command)
        try:
            await entered.wait()
            assert session.state == "idle" and session.is_connecting()
            await _reject_duplicate(manager, tmp_path, command)
            assert session.pending_prompts == [command]
        finally:
            release.set()
            await _settle(manager, "project_alpha")
            assert session.client.queries == [command]
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", COMMANDS)
def test_queued_duplicate_is_not_saved_or_run_after_cancellation(pool_env, tmp_path, command):
    manager, _, _ = pool_env

    async def scenario():
        await manager.send("project_beta", tmp_path, "synthetic busy task")
        await _settle(manager, "project_beta")
        session = await manager.send("project_alpha", tmp_path, command)
        try:
            assert session.state == "queued"
            await _reject_duplicate(manager, tmp_path, command)
            assert session.pending_prompts == [command]
            assert _messages(session) == [command]
            await manager.interrupt("project_alpha")
            await manager.close("project_beta")
            await asyncio.sleep(0)
            assert session.state == "idle" and session.engine is None
            assert session.pending_prompts == []
            assert manager.queue_position("project_alpha") is None
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("interrupted", [False, True])
def test_running_duplicate_rejected_and_retry_allowed_after_turn(scripted_env, tmp_path, command, interrupted):
    manager, _, _ = scripted_env

    async def scenario():
        session = await manager.send("project_alpha", tmp_path, command)
        await _settle(manager, "project_alpha")
        client = session.client
        try:
            await _reject_duplicate(manager, tmp_path, command)
            assert client.queries == [command]
            assert _messages(session) == [command]
            assert [e["text"] for e in load_history("project_alpha") if e["type"] == "user_message"] == [command]
            if interrupted:
                await manager.interrupt("project_alpha")
                assert client.interrupted
            await _feed(client, _turn_result())
            assert session.state == "idle"
            assert client.queries == [command]  # No duplicate starts after stopping.
            await manager.send("project_alpha", tmp_path, command)
            assert client.queries == [command, command]
            assert _messages(session) == [command, command]
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("followup", ["/ontology-extract-operation", "synthetic clarification"])
def test_distinct_followup_allows_returning_to_previous_command(scripted_env, tmp_path, followup):
    """运行中发了别的（插话或代存别的命令）之后，可以再次主动提交原命令。

    本分支运行中的命令由平台代存到这一轮结束（F7）：原命令再提交时进代存，不立即交给引擎；
    已经代存着一条命令时要带 replace 才能换成原命令。
    """
    manager, _, _ = scripted_env

    async def scenario():
        command = COMMANDS[0]
        session = await manager.send("project_alpha", tmp_path, command)
        await _settle(manager, "project_alpha")
        try:
            await _reject_duplicate(manager, tmp_path, command)
            await _reject_duplicate(manager, tmp_path, command)
            await manager.send("project_alpha", tmp_path, followup)
            await _reject_duplicate(manager, tmp_path, followup)
            _, outcome = await manager.submit("project_alpha", tmp_path, command, meta={"replace": True})
            await _reject_duplicate(manager, tmp_path, command)
            assert session.state == "running"
            assert outcome["delivery"] == "next"
            assert manager.next_view("project_alpha", "default")["command"] == command
            assert session.client.queries.count(command) == 1
            expected = [command] + ([] if followup.startswith("/") else [followup])
            assert _messages(session) == expected
            assert [e["text"] for e in load_history("project_alpha") if e["type"] == "user_message"] == expected
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_old_failed_submission_does_not_unlock_a_newer_identical_command(scripted_env, tmp_path, monkeypatch):
    manager, _, _ = scripted_env

    async def scenario():
        session = await manager.send("project_alpha", tmp_path, "synthetic setup")
        await _settle(manager, "project_alpha")
        entered, release = asyncio.Event(), asyncio.Event()
        deliver = manager._deliver
        calls = 0

        async def delayed_failure(session, cwd, text, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                raise RuntimeError("synthetic delayed failure")
            return await deliver(session, cwd, text, *args, **kwargs)

        monkeypatch.setattr(manager, "_deliver", delayed_failure)
        command = COMMANDS[0]
        first = asyncio.create_task(manager.send("project_alpha", tmp_path, command))
        try:
            await entered.wait()
            await manager.send("project_alpha", tmp_path, "/ontology-extract-operation")
            await manager.submit("project_alpha", tmp_path, command, meta={"replace": True})
            release.set()
            with pytest.raises(RuntimeError, match="synthetic delayed failure"):
                await first
            await _reject_duplicate(manager, tmp_path, command)
            assert session.client.queries == ["synthetic setup"]
            assert manager.next_view("project_alpha", "default")["command"] == command
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await manager.shutdown()

    asyncio.run(scenario())


def test_failed_delivery_does_not_block_explicit_retry(pool_env, tmp_path, monkeypatch):
    manager, _, _ = pool_env

    async def scenario():
        session = await manager.send("project_alpha", tmp_path, "synthetic setup")
        await _settle(manager, "project_alpha")
        session.set_state("idle")
        query = session.client.query

        async def fail_once(text):
            monkeypatch.setattr(session.client, "query", query)
            raise RuntimeError("synthetic send failure")

        monkeypatch.setattr(session.client, "query", fail_once)
        try:
            with pytest.raises(RuntimeError, match="synthetic send failure"):
                await manager.send("project_alpha", tmp_path, COMMANDS[1])
            await manager.send("project_alpha", tmp_path, COMMANDS[1])
            assert session.client.queries == ["synthetic setup", COMMANDS[1]]
            assert _messages(session) == ["synthetic setup", COMMANDS[1], COMMANDS[1]]
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_failed_connection_allows_one_explicit_retry_without_replaying_failed_prompt(pool_env, tmp_path, monkeypatch):
    manager, _, _ = pool_env
    connect = FakeClient.connect

    async def fail_once(client):
        monkeypatch.setattr(FakeClient, "connect", connect)
        raise RuntimeError("synthetic connection failure")

    monkeypatch.setattr(FakeClient, "connect", fail_once)

    async def scenario():
        session = await manager.send("project_alpha", tmp_path, COMMANDS[0])
        await _settle(manager, "project_alpha")
        try:
            assert session.state == "error"
            await manager.send("project_alpha", tmp_path, COMMANDS[0])
            await _settle(manager, "project_alpha")
            assert session.client.queries == [COMMANDS[0]]
            assert _messages(session) == [COMMANDS[0], COMMANDS[0]]
            assert any(event["type"] == "session_error" for event in session.events)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())
