package com.kbsuite.lf;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.openjiuwen.core.session.stream.OutputSchema;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Turns agent-core stream chunks into DA-compatible SSE events on a task.
 *
 * <p>Chunk types written by ReActAgent: {@code llm_reasoning} / {@code llm_output} (text
 * deltas, or the turn's tool calls), {@code llm_usage} (end of a model call),
 * {@code tool_call} / {@code tool_output} (tool lifecycle) and {@code answer} (final result).
 * One model call is one DA "turn"; tool events belong to the turn that requested them.
 */
final class ChunkTranslator {

    private static final ObjectMapper JSON = new ObjectMapper();

    private final LfTask task;
    private int turn;
    private boolean turnClosed;
    private StringBuilder turnText = new StringBuilder();
    private String lastText = "";
    private String answer;
    private String error;

    ChunkTranslator(LfTask task) {
        this.task = task;
    }

    void accept(Object chunk) {
        if (!(chunk instanceof OutputSchema schema)) {
            return;
        }
        Map<String, Object> payload = asMap(schema.getPayload());
        switch (String.valueOf(schema.getType())) {
            case "llm_reasoning" -> delta("thinking_delta", payload.get("content"));
            case "llm_output" -> {
                if (payload.get("content") != null) {
                    delta("content_delta", payload.get("content"));
                }
            }
            case "tool_call" -> task.emit("tool_call", map(
                    "id", payload.get("tool_call_id"),
                    "toolName", payload.get("tool_name"),
                    "input", arguments(payload.get("arguments")),
                    "turn", turn));
            case "tool_output" -> task.emit("tool_result", map(
                    "id", payload.get("tool_call_id"),
                    "toolName", payload.get("tool_name"),
                    "output", "error".equals(payload.get("status"))
                            ? map("error", payload.get("error")) : payload.get("result"),
                    "turn", turn));
            case "llm_usage" -> closeTurn(payload.get("usage_metadata"));
            case "answer" -> {
                if ("error".equals(payload.get("result_type"))) {
                    error = String.valueOf(payload.get("output"));
                } else {
                    answer = String.valueOf(payload.getOrDefault("output", ""));
                }
            }
            default -> {
                // other chunk types (retries, external tool requests) are not part of the DA surface
            }
        }
    }

    /**
     * How a run ended.
     *
     * @param status completed, cancelled or failed (DA's done.status values)
     * @param output answer text; empty unless completed
     */
    record Outcome(String status, String output) {
    }

    /**
     * Emits the terminal events.
     *
     * @param cancelled whether the run was cancelled
     * @return how the run ended
     */
    Outcome finish(boolean cancelled) {
        if (cancelled) {
            task.emit("done", map("status", "cancelled"));
            return new Outcome("cancelled", "");
        }
        if (error != null) {
            task.emit("error", map("code", "ENGINE_ERROR", "message", truncate(error, 500)));
            task.emit("done", map("status", "failed"));
            return new Outcome("failed", "");
        }
        String output = answer != null && !answer.isBlank() ? answer : currentOrLastText();
        task.emit("complete", map("output", output));
        task.emit("done", map("status", "completed", "output", output));
        return new Outcome("completed", output);
    }

    private void delta(String event, Object content) {
        String text = content == null ? "" : String.valueOf(content);
        if (text.isEmpty()) {
            return;
        }
        if (turnClosed) {
            turn++;
            turnClosed = false;
            turnText = new StringBuilder();
        }
        if ("content_delta".equals(event)) {
            turnText.append(text);
        }
        task.emit(event, map("delta", text, "turn", turn));
    }

    private void closeTurn(Object usage) {
        if (!turnText.isEmpty()) {
            lastText = turnText.toString();
        }
        task.emit("turn_usage", map("turn", turn, "usage", usage instanceof Map<?, ?> ? usage : Map.of()));
        task.emit("turn", map("turn", turn));
        turnClosed = true;
    }

    private String currentOrLastText() {
        return !turnText.isEmpty() ? turnText.toString() : lastText;
    }

    private static Object arguments(Object raw) {
        if (raw instanceof String text && !text.isBlank()) {
            try {
                return JSON.readValue(text, Object.class);
            } catch (Exception ignored) {
                return text;
            }
        }
        return raw == null ? Map.of() : raw;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asMap(Object payload) {
        if (payload instanceof Map<?, ?> m) {
            return (Map<String, Object>) m;
        }
        return Map.of("content", String.valueOf(payload));
    }

    static Map<String, Object> map(Object... kv) {
        Map<String, Object> out = new LinkedHashMap<>();
        for (int i = 0; i + 1 < kv.length; i += 2) {
            if (kv[i + 1] != null) {
                out.put(String.valueOf(kv[i]), kv[i + 1]);
            }
        }
        return out;
    }

    static String truncate(String text, int max) {
        return text == null || text.length() <= max ? text : text.substring(0, max) + "…";
    }
}
