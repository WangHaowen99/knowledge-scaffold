from __future__ import annotations

import unittest

from onto_agent_sdk.facades.chat import ChatFacade
from onto_agent_sdk.protocols.agentic import AgenticRunsApi
from onto_agent_sdk.types import normalize_agentic_envelope


class _FakeResponse:
    def __init__(self) -> None:
        self.closed = False

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def iter_lines(self, *, decode_unicode: bool):
        del decode_unicode
        yield "id: 8"
        yield "event: agent.v2"
        yield (
            'data: {"protocol":"agent-stream.v2","seq":8,"event_id":"run-1:8","type":"turn.finished",'
            '"payload":{"status":"awaiting_input"}}'
        )
        yield ""

    def close(self) -> None:
        self.closed = True


class _FakeHttp:
    def __init__(self) -> None:
        self.calls = []
        self.response = _FakeResponse()
        self.admission = {
            "run_id": "server-run-id",
            "session_id": "session-1",
            "turn_id": 1,
            "status": "queued",
            "created": True,
            "links": {
                "self": "/agentic/runs/server-run-id",
                "events": "/agentic/runs/server-events-link/events",
                "cancel": "/agentic/runs/server-run-id/cancel",
            },
        }

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self.response

    def post(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self.admission


class AgenticRunResumeTests(unittest.TestCase):
    def test_resume_uses_both_supported_cursor_forms(self) -> None:
        http = _FakeHttp()
        api = AgenticRunsApi(http)  # type: ignore[arg-type]

        with api.resume("run/id", after_seq=7) as stream:
            event = next(stream)

        self.assertEqual(
            http.calls,
            [
                (
                    "/agentic/runs/run%2Fid/events",
                    {
                        "params": {"after_seq": 7},
                        "headers": {"Last-Event-ID": "7"},
                        "stream": True,
                    },
                )
            ],
        )
        self.assertEqual(event.kind, "run.awaiting")
        self.assertEqual(event.sequence, 8)
        self.assertEqual(event.event_id, "run-1:8")
        self.assertTrue(http.response.closed)

    def test_normalizer_exposes_replay_cursor(self) -> None:
        event = normalize_agentic_envelope(
            {
                "protocol": "agent-stream.v2",
                "type": "token.delta",
                "seq": "12",
                "event_id": "run-2:12",
                "payload": {"delta": "done"},
            }
        )

        self.assertEqual(event.sequence, 12)
        self.assertEqual(event.event_id, "run-2:12")

    def test_chat_send_posts_run_then_gets_admission_events_link(self) -> None:
        http = _FakeHttp()
        api = AgenticRunsApi(http)  # type: ignore[arg-type]
        facade = ChatFacade(api, object())  # type: ignore[arg-type]

        with facade.send(
            session_id="session-1",
            message="synthetic request",
            agent_mode="multi_agent",
            model_profile_id="model-demo",
            selected_ontology_id="ontology-demo",
            include_trace=True,
            idempotency_key="ordinary-key",
        ) as stream:
            next(stream)

        self.assertEqual(
            http.calls[0],
            (
                "/agentic/runs",
                {
                    "json": {
                        "session_id": "session-1",
                        "request": "synthetic request",
                        "agent_mode": "multi_agent",
                        "model_profile_id": "model-demo",
                        "arguments": {"selected_ontology_id": "ontology-demo"},
                        "include_trace": True,
                    },
                    "headers": {"Idempotency-Key": "ordinary-key"},
                },
            ),
        )
        self.assertEqual(
            http.calls[1],
            (
                "/agentic/runs/server-events-link/events",
                {
                    "params": {"after_seq": 0},
                    "headers": None,
                    "stream": True,
                },
            ),
        )

    def test_hitl_answer_uses_same_create_then_events_contract(self) -> None:
        http = _FakeHttp()
        api = AgenticRunsApi(http)  # type: ignore[arg-type]
        facade = ChatFacade(api, object())  # type: ignore[arg-type]
        response = {
            "interaction_id": "interaction-1",
            "type": "single_choice",
            "option_id": "option-a",
            "value": "A",
        }

        with facade.send(
            session_id="session-1",
            message="select option A",
            interaction_response=response,
            idempotency_key="interaction-key",
        ) as stream:
            next(stream)

        create_path, create_options = http.calls[0]
        self.assertEqual(create_path, "/agentic/runs")
        self.assertEqual(create_options["json"]["interaction_response"], response)
        self.assertEqual(
            create_options["headers"],
            {"Idempotency-Key": "interaction-key"},
        )
        self.assertEqual(
            http.calls[1][0],
            "/agentic/runs/server-events-link/events",
        )

    def test_events_link_rejects_cross_origin_url(self) -> None:
        http = _FakeHttp()
        api = AgenticRunsApi(http)  # type: ignore[arg-type]

        with self.assertRaisesRegex(ValueError, "relative API path"):
            api.events_from_link("https://example.com/events")

        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
