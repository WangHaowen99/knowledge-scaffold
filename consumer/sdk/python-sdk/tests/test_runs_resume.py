from __future__ import annotations

import unittest

from onto_agent_sdk.protocols.agentic import AgenticRunsApi
from onto_agent_sdk.types import normalize_agentic_envelope


class _FakeResponse:
    def __init__(self) -> None:
        self.closed = False

    def iter_lines(self, *, decode_unicode: bool):
        del decode_unicode
        yield 'id: 8'
        yield 'event: agent.v2'
        yield 'data: {"protocol":"agent-stream.v2","seq":8,"event_id":"run-1:8","type":"turn.finished","payload":{"status":"awaiting_input"}}'
        yield ''

    def close(self) -> None:
        self.closed = True


class _FakeHttp:
    def __init__(self) -> None:
        self.calls = []
        self.response = _FakeResponse()

    def get(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return self.response


class AgenticRunResumeTests(unittest.TestCase):
    def test_resume_uses_both_supported_cursor_forms(self) -> None:
        http = _FakeHttp()
        api = AgenticRunsApi(http)  # type: ignore[arg-type]

        with api.resume('run/id', after_seq=7) as stream:
            event = next(stream)

        self.assertEqual(http.calls, [(
            '/agentic/runs/run%2Fid/events',
            {
                'params': {'after_seq': 7},
                'headers': {'Last-Event-ID': '7'},
                'stream': True,
            },
        )])
        self.assertEqual(event.kind, 'run.awaiting')
        self.assertEqual(event.sequence, 8)
        self.assertEqual(event.event_id, 'run-1:8')
        self.assertTrue(http.response.closed)

    def test_normalizer_exposes_replay_cursor(self) -> None:
        event = normalize_agentic_envelope({
            'protocol': 'agent-stream.v2',
            'type': 'token.delta',
            'seq': '12',
            'event_id': 'run-2:12',
            'payload': {'delta': 'done'},
        })

        self.assertEqual(event.sequence, 12)
        self.assertEqual(event.event_id, 'run-2:12')


if __name__ == '__main__':
    unittest.main()
