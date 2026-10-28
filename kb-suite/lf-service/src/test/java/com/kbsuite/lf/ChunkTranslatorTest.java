package com.kbsuite.lf;

import com.openjiuwen.core.session.stream.OutputSchema;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

class ChunkTranslatorTest {

    private static List<String> names(LfTask task) throws InterruptedException {
        return task.await(0, 0).stream().map(LfTask.SseEvent::event).toList();
    }

    @Test
    void toolTurnThenAnswerBecomeDaEvents() throws InterruptedException {
        LfTask task = new LfTask("t1", "s1");
        ChunkTranslator tr = new ChunkTranslator(task);
        tr.accept(new OutputSchema("llm_reasoning", 0, Map.of("content", "想一想")));
        tr.accept(new OutputSchema("llm_output", 1, Map.of("content", "先查一下")));
        tr.accept(new OutputSchema("llm_output", 2, Map.of("tool_calls", List.of())));
        tr.accept(new OutputSchema("llm_usage", 3, Map.of("usage_metadata", Map.of("input_tokens", 10))));
        tr.accept(new OutputSchema("tool_call", 4,
                Map.of("tool_call_id", "c1", "tool_name", "search_knowledge", "arguments", "{\"query\":\"油压\"}")));
        tr.accept(new OutputSchema("tool_output", 5,
                Map.of("tool_call_id", "c1", "tool_name", "search_knowledge", "status", "completed", "result", "[1]")));
        tr.accept(new OutputSchema("llm_output", 6, Map.of("content", "报警值 0.15 MPa")));
        tr.accept(new OutputSchema("llm_usage", 7, Map.of("usage_metadata", Map.of())));
        tr.accept(new OutputSchema("answer", 0, Map.of("output", "报警值 0.15 MPa", "result_type", "answer")));
        ChunkTranslator.Outcome outcome = tr.finish(false);

        assertEquals(List.of("thinking_delta", "content_delta", "turn_usage", "turn", "tool_call", "tool_result",
                "content_delta", "turn_usage", "turn", "complete", "done"), names(task));
        List<LfTask.SseEvent> events = task.await(0, 0);
        assertEquals(Map.of("query", "油压"), events.get(4).data().get("input"));
        assertEquals(0, events.get(4).data().get("turn"));
        assertEquals(1, events.get(6).data().get("turn"));
        assertEquals("t1-1", events.get(0).id());
        assertEquals(new ChunkTranslator.Outcome("completed", "报警值 0.15 MPa"), outcome);
    }

    @Test
    void lastTurnTextIsTheAnswerWhenTheFinalChunkIsMissing() throws InterruptedException {
        LfTask task = new LfTask("t2", "s1");
        ChunkTranslator tr = new ChunkTranslator(task);
        tr.accept(new OutputSchema("llm_output", 0, Map.of("content", "结论")));
        tr.accept(new OutputSchema("llm_usage", 1, Map.of()));
        assertEquals("结论", tr.finish(false).output());
    }

    @Test
    void cancelledAndFailedRunsEndAccordingly() throws InterruptedException {
        LfTask cancelled = new LfTask("t3", "s1");
        assertEquals("cancelled", new ChunkTranslator(cancelled).finish(true).status());
        assertEquals(Map.of("status", "cancelled"), cancelled.await(0, 0).get(0).data());

        LfTask failed = new LfTask("t4", "s1");
        ChunkTranslator tr = new ChunkTranslator(failed);
        tr.accept(new OutputSchema("answer", 0, Map.of("output", "model timeout", "result_type", "error")));
        assertEquals("failed", tr.finish(false).status());
        assertEquals(List.of("error", "done"), names(failed));
        assertEquals("ENGINE_ERROR", failed.await(0, 0).get(0).data().get("code"));
    }
}
