package com.kbsuite.lf;

import java.util.List;
import java.util.Map;

/** Executes a task to completion, emitting its events; tests substitute a scripted engine. */
interface Engine {

    /**
     * One run-stream request.
     *
     * @param input user text
     * @param kbIds knowledge bases in scope ({@code scope.kbIds})
     * @param mcpServers {@code runtimeConfig.mcpServers}
     * @param maxTurns per-request iteration cap, or null
     * @param history earlier exchanges of the conversation
     */
    record RunRequest(String input, List<String> kbIds, List<Map<String, Object>> mcpServers,
            Integer maxTurns, List<TaskRegistry.Turn> history) {
    }

    /**
     * Runs the task; must end with {@link LfTask#finish(String)}.
     *
     * @param task task to run
     * @param request what to run
     */
    void run(LfTask task, RunRequest request);
}
