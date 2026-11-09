package com.kbsuite.lf;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static com.kbsuite.lf.ChunkTranslator.map;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class LfServerTest {

    private static final String SECRET = "s3cret";

    /** Emits start + one delta, then waits until released (or cancelled), then completes. */
    static final class ScriptedEngine implements Engine {
        final CountDownLatch release = new CountDownLatch(1);
        final List<Engine.RunRequest> requests = new ArrayList<>();
        final List<String> steered = new ArrayList<>();
        volatile LfTask current;

        @Override
        public void run(LfTask task, Engine.RunRequest request) {
            current = task;
            requests.add(request);
            task.emit("start", map("taskId", task.taskId()));
            task.emit("content_delta", map("delta", "答", "turn", 0));
            try {
                while (!task.cancelRequested() && !release.await(20, TimeUnit.MILLISECONDS)) {
                    String s = task.steering().poll();
                    if (s != null) {
                        steered.add(s);
                    }
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            if (task.cancelRequested()) {
                task.emit("done", map("status", "cancelled"));
                task.finish("cancelled");
                return;
            }
            task.emit("complete", map("output", "答"));
            task.emit("done", map("status", "completed", "output", "答"));
            task.finish("completed");
        }
    }

    private LfServer server;
    private ScriptedEngine engine;
    private final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

    @BeforeEach
    void start() throws Exception {
        LfConfig cfg = LfConfig.fromEnv(Map.of("LF_ENGINE_SECRET", SECRET, "LF_MODEL_BASE_URL", "http://model"));
        engine = new ScriptedEngine();
        server = new LfServer(cfg, new TaskRegistry(60), engine);
        server.start(0);
    }

    @AfterEach
    void stop() {
        engine.release.countDown();
        server.stop();
    }

    private HttpRequest.Builder req(String path, String session) {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + server.port() + path))
                .timeout(Duration.ofSeconds(10)).header("X-Engine-Secret", SECRET);
        return session == null ? b : b.header("X-Da-Session-Id", session);
    }

    private CompletableFuture<HttpResponse<String>> runStream(String session, String body) {
        return client.sendAsync(req("/api/agents/run-stream", session)
                .POST(HttpRequest.BodyPublishers.ofString(body)).build(), HttpResponse.BodyHandlers.ofString());
    }

    private String awaitTaskId() throws InterruptedException {
        for (int i = 0; i < 200 && engine.current == null; i++) {
            Thread.sleep(10);
        }
        return engine.current.taskId();
    }

    @Test
    void rejectsMissingSecret() throws Exception {
        HttpResponse<String> r = client.send(HttpRequest.newBuilder(
                URI.create("http://127.0.0.1:" + server.port() + "/api/agents/run-stream"))
                .POST(HttpRequest.BodyPublishers.ofString("{}")).build(), HttpResponse.BodyHandlers.ofString());
        assertEquals(401, r.statusCode());
    }

    @Test
    void streamsTheRunAndPassesScopeHistoryAndMcpServers() throws Exception {
        var pending = runStream("s1", "{\"sessionId\":\"s1\",\"input\":\"问\",\"scope\":{\"kbIds\":[\"kb1\"]},"
                + "\"runtimeConfig\":{\"version\":1,\"mcpServers\":[{\"serverName\":\"team\",\"serverPath\":\"http://b\"}]}}");
        awaitTaskId();
        engine.release.countDown();
        String sse = pending.get(10, TimeUnit.SECONDS).body();
        assertTrue(sse.contains("event: start"), sse);
        assertTrue(sse.contains("event: done\ndata: {\"status\":\"completed\",\"output\":\"答\"}"), sse);
        Engine.RunRequest request = engine.requests.get(0);
        assertEquals(List.of("kb1"), request.kbIds());
        assertEquals("team", request.mcpServers().get(0).get("serverName"));
    }

    @Test
    void secondRunWhileBusyGetsEngineBusy() throws Exception {
        var first = runStream("s1", "{\"sessionId\":\"s1\",\"input\":\"一\"}");
        awaitTaskId();
        String second = runStream("s2", "{\"sessionId\":\"s2\",\"input\":\"二\"}").get(10, TimeUnit.SECONDS).body();
        assertTrue(second.contains("\"code\":\"ENGINE_BUSY\""), second);
        assertTrue(second.contains("event: done"), second);
        engine.release.countDown();
        first.get(10, TimeUnit.SECONDS);
    }

    @Test
    void cancelInjectReplayAndOwnership() throws Exception {
        var pending = runStream("s1", "{\"sessionId\":\"s1\",\"input\":\"问\"}");
        String taskId = awaitTaskId();

        HttpResponse<String> foreign = client.send(req("/api/agents/cancel/" + taskId, "other")
                .POST(HttpRequest.BodyPublishers.noBody()).build(), HttpResponse.BodyHandlers.ofString());
        assertEquals(404, foreign.statusCode());

        HttpResponse<String> injected = client.send(req("/api/agents/inject/" + taskId, "s1")
                .POST(HttpRequest.BodyPublishers.ofString("{\"message\":\"只看 PDF\"}")).build(),
                HttpResponse.BodyHandlers.ofString());
        assertEquals(200, injected.statusCode());
        for (int i = 0; i < 100 && engine.steered.isEmpty(); i++) {
            Thread.sleep(10);
        }
        assertEquals(List.of("只看 PDF"), engine.steered);

        assertEquals(404, client.send(req("/api/agents/answer/" + taskId, "s1")
                .POST(HttpRequest.BodyPublishers.noBody()).build(), HttpResponse.BodyHandlers.ofString()).statusCode());

        HttpResponse<String> cancelled = client.send(req("/api/agents/cancel/" + taskId, "s1")
                .POST(HttpRequest.BodyPublishers.noBody()).build(), HttpResponse.BodyHandlers.ofString());
        assertEquals(200, cancelled.statusCode());
        assertTrue(pending.get(10, TimeUnit.SECONDS).body().contains("\"status\":\"cancelled\""));

        HttpResponse<String> replay = client.send(req("/api/agents/stream/" + taskId, "s1")
                .header("Last-Event-ID", taskId + "-1").GET().build(), HttpResponse.BodyHandlers.ofString());
        assertTrue(!replay.body().contains("event: start") && replay.body().contains("event: content_delta"),
                replay.body());
        HttpResponse<String> status = client.send(req("/api/agents/status/" + taskId, "s1").GET().build(),
                HttpResponse.BodyHandlers.ofString());
        assertTrue(status.body().contains("\"status\":\"cancelled\""), status.body());
    }
}
