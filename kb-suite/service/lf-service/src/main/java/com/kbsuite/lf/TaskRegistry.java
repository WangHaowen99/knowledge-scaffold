package com.kbsuite.lf;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Running and recently finished tasks, plus a short per-conversation history.
 *
 * <p>Only one task runs at a time: loop-forest registers its tools (the fork tool) in the
 * process-global resource manager under fixed ids, so two concurrent installations would
 * collide. A second request is answered with ENGINE_BUSY, which the harness provider
 * retries, the same way DA's busy conversation is handled.
 */
final class TaskRegistry {

    /** One finished exchange of a conversation. */
    record Turn(String question, String answer) {
    }

    private final Map<String, LfTask> tasks = new ConcurrentHashMap<>();
    private final Map<String, Deque<Turn>> history = new ConcurrentHashMap<>();
    private final long retentionMillis;
    private LfTask running;

    TaskRegistry(long retentionSeconds) {
        this.retentionMillis = retentionSeconds * 1000;
    }

    /**
     * Opens a task for the conversation unless one is already running.
     *
     * @param sessionId conversation id
     * @return the new task, or empty when the engine is busy
     */
    synchronized Optional<LfTask> tryStart(String sessionId) {
        prune();
        if (running != null) {
            return Optional.empty();
        }
        LfTask task = new LfTask(UUID.randomUUID().toString().replace("-", ""), sessionId);
        tasks.put(task.taskId(), task);
        running = task;
        return Optional.of(task);
    }

    synchronized void finished(LfTask task) {
        if (running == task) {
            running = null;
        }
    }

    Optional<LfTask> get(String taskId) {
        return Optional.ofNullable(tasks.get(taskId));
    }

    List<Turn> history(String sessionId) {
        Deque<Turn> turns = history.get(sessionId);
        if (turns == null) {
            return List.of();
        }
        synchronized (turns) {
            return List.copyOf(turns);
        }
    }

    void remember(String sessionId, Turn turn, int keep) {
        if (keep <= 0) {
            return;
        }
        Deque<Turn> turns = history.computeIfAbsent(sessionId, k -> new ArrayDeque<>());
        synchronized (turns) {
            turns.addLast(turn);
            while (turns.size() > keep) {
                turns.removeFirst();
            }
        }
    }

    private void prune() {
        long cutoff = System.currentTimeMillis() - retentionMillis;
        tasks.values().removeIf(t -> t.finished() && t.finishedAtMillis() < cutoff);
    }
}
