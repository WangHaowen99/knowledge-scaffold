package com.kbsuite.lf;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Queue;
import java.util.concurrent.ConcurrentLinkedQueue;

/**
 * One run: its SSE event log (replayable by event id), cancel flag and steering inbox.
 */
final class LfTask {

    /** One SSE event; ids are {@code <taskId>-<n>} like DA's. */
    record SseEvent(String id, String event, Map<String, Object> data) {
    }

    private final String taskId;
    private final String sessionId;
    private final List<SseEvent> events = new ArrayList<>();
    private final Queue<String> steering = new ConcurrentLinkedQueue<>();
    private volatile boolean cancelRequested;
    private String status = "running";
    private long finishedAtMillis;

    LfTask(String taskId, String sessionId) {
        this.taskId = taskId;
        this.sessionId = sessionId;
    }

    String taskId() {
        return taskId;
    }

    String sessionId() {
        return sessionId;
    }

    Queue<String> steering() {
        return steering;
    }

    boolean cancelRequested() {
        return cancelRequested;
    }

    void requestCancel() {
        cancelRequested = true;
    }

    synchronized void emit(String event, Map<String, Object> data) {
        events.add(new SseEvent(taskId + "-" + (events.size() + 1), event, data));
        notifyAll();
    }

    synchronized void finish(String finalStatus) {
        if (finishedAtMillis == 0) {
            status = finalStatus;
            finishedAtMillis = System.currentTimeMillis();
            notifyAll();
        }
    }

    synchronized String status() {
        return status;
    }

    synchronized boolean finished() {
        return finishedAtMillis != 0;
    }

    synchronized long finishedAtMillis() {
        return finishedAtMillis;
    }

    /**
     * Events from {@code from} on, waiting up to {@code timeoutMillis} when there are none yet.
     *
     * @param from index of the first event wanted
     * @param timeoutMillis how long to wait for new events
     * @return new events (empty on timeout or when the task has finished)
     * @throws InterruptedException when the waiting thread is interrupted
     */
    synchronized List<SseEvent> await(int from, long timeoutMillis) throws InterruptedException {
        if (from >= events.size() && finishedAtMillis == 0) {
            wait(timeoutMillis);
        }
        return from >= events.size() ? List.of() : List.copyOf(events.subList(from, events.size()));
    }

    /**
     * Index of the first event after {@code lastEventId}; 0 when the id is unknown.
     *
     * @param lastEventId value of the Last-Event-ID header
     * @return replay start index
     */
    synchronized int indexAfter(String lastEventId) {
        if (lastEventId == null || lastEventId.isBlank()) {
            return 0;
        }
        for (int i = 0; i < events.size(); i++) {
            if (events.get(i).id().equals(lastEventId.trim())) {
                return i + 1;
            }
        }
        return 0;
    }
}
