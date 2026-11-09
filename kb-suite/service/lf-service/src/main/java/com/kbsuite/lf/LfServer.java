package com.kbsuite.lf;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import static com.kbsuite.lf.ChunkTranslator.map;

/**
 * HTTP + SSE front of the loop-forest engine: the subset of DA-java's {@code /api/agents}
 * that kb-suite's harness provider uses, with the same headers, events and error codes.
 *
 * <pre>
 * GET  /ready
 * POST /api/agents/run-stream        {sessionId, input, scope:{kbIds}, maxTurns, runtimeConfig:{version:1, mcpServers}}
 * GET  /api/agents/stream/{taskId}   replay after Last-Event-ID, then live
 * GET  /api/agents/status/{taskId}
 * POST /api/agents/cancel/{taskId}
 * POST /api/agents/inject/{taskId}   {message}
 * POST /api/agents/answer/{taskId}   404: loop-forest never asks the user
 * </pre>
 */
public final class LfServer {

    private static final Logger LOGGER = LoggerFactory.getLogger(LfServer.class);
    private static final ObjectMapper JSON = new ObjectMapper()
            .disable(SerializationFeature.FAIL_ON_EMPTY_BEANS);
    private static final String API = "/api/agents/";
    private static final long KEEPALIVE_MILLIS = 15_000;

    private final LfConfig cfg;
    private final TaskRegistry registry;
    private final Engine engine;
    private final ExecutorService runs = Executors.newCachedThreadPool(daemon("lf-run"));
    private HttpServer http;

    LfServer(LfConfig cfg, TaskRegistry registry, Engine engine) {
        this.cfg = cfg;
        this.registry = registry;
        this.engine = engine;
    }

    public static void main(String[] args) throws IOException {
        LfConfig cfg = LfConfig.fromEnv(System.getenv());
        TaskRegistry registry = new TaskRegistry(cfg.taskRetentionSeconds());
        LfServer server = new LfServer(cfg, registry, new LfRunner(cfg, registry));
        server.start(cfg.port());
        LOGGER.info("loop-forest service listening on :{} model={} fork={} kbMcp={}",
                server.port(), cfg.modelName(), cfg.forkEnabled(), !cfg.kbMcpUrl().isBlank());
    }

    void start(int port) throws IOException {
        http = HttpServer.create(new InetSocketAddress(port), 64);
        http.createContext("/", this::handle);
        http.setExecutor(Executors.newCachedThreadPool(daemon("lf-http")));
        http.start();
    }

    void stop() {
        if (http != null) {
            http.stop(0);
        }
        runs.shutdownNow();
    }

    int port() {
        return http.getAddress().getPort();
    }

    private void handle(HttpExchange ex) throws IOException {
        try (ex) {
            String method = ex.getRequestMethod();
            String path = ex.getRequestURI().getPath();
            if ("GET".equals(method) && "/ready".equals(path)) {
                respond(ex, 200, map("status", "UP"));
                return;
            }
            if (!path.startsWith(API)) {
                respond(ex, 404, map("error", "not found"));
                return;
            }
            if (!authorized(ex)) {
                respond(ex, 401, map("error", "invalid engine secret"));
                return;
            }
            String rest = path.substring(API.length());
            if ("POST".equals(method) && "run-stream".equals(rest)) {
                runStream(ex);
                return;
            }
            int slash = rest.indexOf('/');
            if (slash < 0) {
                respond(ex, 404, map("error", "not found"));
                return;
            }
            String action = rest.substring(0, slash);
            Optional<LfTask> task = registry.get(rest.substring(slash + 1)).filter(t -> owns(ex, t));
            switch (method + " " + action) {
                case "GET stream" -> {
                    if (task.isEmpty()) {
                        respond(ex, 404, map("error", "Task not found"));
                    } else {
                        stream(ex, task.get(), task.get().indexAfter(ex.getRequestHeaders().getFirst("Last-Event-ID")));
                    }
                }
                case "GET status" -> respond(ex, task.isEmpty() ? 404 : 200, task
                        .<Map<String, Object>>map(t -> map("taskId", t.taskId(), "status", t.status()))
                        .orElse(map("error", "Task not found")));
                case "POST cancel" -> cancel(ex, task);
                case "POST inject" -> inject(ex, task);
                case "POST answer" -> respond(ex, 404, map("error", "no pending question"));
                default -> respond(ex, 404, map("error", "not found"));
            }
        } catch (IOException | RuntimeException failure) {
            LOGGER.debug("request failed", failure);
        }
    }

    private void runStream(HttpExchange ex) throws IOException {
        Map<String, Object> body = readBody(ex);
        String sessionId = text(body.get("sessionId"));
        if (sessionId.isBlank()) {
            sessionId = text(ex.getRequestHeaders().getFirst("X-Da-Session-Id"));
        }
        if (sessionId.isBlank() || text(body.get("input")).isBlank()) {
            respond(ex, 400, map("error", "sessionId and input are required"));
            return;
        }
        Map<String, Object> runtime = asMap(body.get("runtimeConfig"));
        if (!runtime.isEmpty() && !Integer.valueOf(1).equals(runtime.get("version"))) {
            respond(ex, 400, map("error", "runtimeConfig.version unsupported"));
            return;
        }
        Optional<LfTask> started = registry.tryStart(sessionId);
        if (started.isEmpty()) {
            busy(ex);
            return;
        }
        LfTask task = started.get();
        Engine.RunRequest request = new Engine.RunRequest(
                text(body.get("input")),
                strings(asMap(body.get("scope")).get("kbIds")),
                maps(runtime.get("mcpServers")),
                body.get("maxTurns") instanceof Number n ? n.intValue() : null,
                registry.history(sessionId));
        runs.submit(() -> {
            try {
                engine.run(task, request);
            } finally {
                if (!task.finished()) {
                    task.emit("done", map("status", "failed"));
                    task.finish("failed");
                }
                registry.finished(task);
            }
        });
        stream(ex, task, 0);
    }

    /** DA answers a busy conversation with an SSE error, which the provider retries. */
    private void busy(HttpExchange ex) throws IOException {
        LfTask scratch = new LfTask("busy" + UUID.randomUUID().toString().replace("-", ""), "");
        scratch.emit("error", map("code", "ENGINE_BUSY",
                "message", "loop-forest is running another task; retry shortly"));
        scratch.emit("done", map("status", "failed"));
        scratch.finish("failed");
        stream(ex, scratch, 0);
    }

    private void cancel(HttpExchange ex, Optional<LfTask> task) throws IOException {
        if (task.isEmpty() || task.get().finished()) {
            respond(ex, 404, map("error", "Task not found or not running"));
            return;
        }
        task.get().requestCancel();
        task.get().steering().clear();
        respond(ex, 200, map("taskId", task.get().taskId(), "status", "cancelled"));
    }

    private void inject(HttpExchange ex, Optional<LfTask> task) throws IOException {
        String message = text(readBody(ex).get("message"));
        if (task.isEmpty() || task.get().finished() || task.get().cancelRequested()) {
            respond(ex, 404, map("error", "Task not found or not running"));
            return;
        }
        if (message.isBlank()) {
            respond(ex, 400, map("error", "message is required"));
            return;
        }
        task.get().steering().add(message);
        respond(ex, 200, map("taskId", task.get().taskId(), "status", "ok"));
    }

    private void stream(HttpExchange ex, LfTask task, int from) throws IOException {
        ex.getResponseHeaders().set("Content-Type", "text/event-stream; charset=utf-8");
        ex.getResponseHeaders().set("Cache-Control", "no-cache");
        ex.sendResponseHeaders(200, 0);
        OutputStream out = ex.getResponseBody();
        int next = from;
        try {
            while (true) {
                List<LfTask.SseEvent> events = task.await(next, KEEPALIVE_MILLIS);
                if (events.isEmpty()) {
                    if (task.finished()) {
                        break;
                    }
                    out.write(": keepalive\n\n".getBytes(StandardCharsets.UTF_8));
                    out.flush();
                    continue;
                }
                for (LfTask.SseEvent event : events) {
                    out.write(("id: " + event.id() + "\nevent: " + event.event() + "\ndata: "
                            + serialize(event.data()) + "\n\n").getBytes(StandardCharsets.UTF_8));
                    next++;
                }
                out.flush();
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    private boolean authorized(HttpExchange ex) {
        String given = ex.getRequestHeaders().getFirst("X-Engine-Secret");
        return given != null && MessageDigest.isEqual(
                given.getBytes(StandardCharsets.UTF_8), cfg.engineSecret().getBytes(StandardCharsets.UTF_8));
    }

    /** Control calls naming another conversation than the task's are treated as unknown (as DA does). */
    private static boolean owns(HttpExchange ex, LfTask task) {
        String presented = ex.getRequestHeaders().getFirst("X-Da-Session-Id");
        return presented == null || presented.isBlank() || presented.equals(task.sessionId());
    }

    private static void respond(HttpExchange ex, int status, Map<String, Object> body) throws IOException {
        byte[] bytes = serialize(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        ex.getResponseBody().write(bytes);
    }

    private static String serialize(Object value) {
        try {
            return JSON.writeValueAsString(value);
        } catch (IOException | RuntimeException failure) {
            try {
                return JSON.writeValueAsString(map("value", String.valueOf(value)));
            } catch (IOException ignored) {
                return "{}";
            }
        }
    }

    private static Map<String, Object> readBody(HttpExchange ex) throws IOException {
        byte[] raw = ex.getRequestBody().readAllBytes();
        if (raw.length == 0) {
            return Map.of();
        }
        return asMap(JSON.readValue(raw, Object.class));
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asMap(Object value) {
        return value instanceof Map<?, ?> m ? (Map<String, Object>) m : Map.of();
    }

    private static List<String> strings(Object value) {
        return value instanceof List<?> list
                ? list.stream().filter(v -> v != null && !String.valueOf(v).isBlank()).map(String::valueOf).toList()
                : List.of();
    }

    private static List<Map<String, Object>> maps(Object value) {
        return value instanceof List<?> list
                ? list.stream().filter(v -> v instanceof Map<?, ?>).map(LfServer::asMap).toList()
                : List.of();
    }

    private static String text(Object value) {
        return value == null ? "" : String.valueOf(value).trim();
    }

    private static java.util.concurrent.ThreadFactory daemon(String prefix) {
        return runnable -> {
            Thread thread = new Thread(runnable, prefix + "-" + UUID.randomUUID().toString().substring(0, 8));
            thread.setDaemon(true);
            return thread;
        };
    }
}
