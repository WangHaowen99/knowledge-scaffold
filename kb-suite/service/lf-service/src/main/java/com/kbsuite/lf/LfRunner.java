package com.kbsuite.lf;

import com.openjiuwen.agents.reactrails.loopforest.LoopForestSuite;
import com.openjiuwen.agents.reactrails.loopforest.execution.SubAgentExecutor;
import com.openjiuwen.core.foundation.llm.Model;
import com.openjiuwen.core.foundation.llm.modelclients.DefaultModelClientFactories;
import com.openjiuwen.core.foundation.llm.schema.ModelClientConfig;
import com.openjiuwen.core.foundation.llm.schema.ModelRequestConfig;
import com.openjiuwen.core.session.AgentSession;
import com.openjiuwen.core.session.stream.StreamMode;
import com.openjiuwen.core.singleagent.agents.ReActAgent;
import com.openjiuwen.core.singleagent.agents.ReActAgentConfig;
import com.openjiuwen.core.singleagent.schema.AgentCard;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Iterator;
import java.util.List;
import java.util.Map;

import static com.kbsuite.lf.ChunkTranslator.map;
import static com.kbsuite.lf.ChunkTranslator.truncate;

/**
 * Runs one task on a loop-forest agent.
 *
 * <p>The main agent is assembled the way {@code LoopForestAgent} does it (ReActAgent with a
 * widened context window, plus the {@link LoopForestSuite} rails and tools), but built here
 * so the run can stream its chunks, take a role prompt, mount MCP servers and be cancelled
 * or steered. Fork branches run on plain ReActAgents with the same model and MCP tools.
 */
final class LfRunner implements Engine {

    private static final Logger LOGGER = LoggerFactory.getLogger(LfRunner.class);

    static final String BRANCH_PROMPT = String.join("\n",
            "你在执行一个大任务里的一个分支子任务，只负责这一个子目标。",
            "用 kb-service 的知识库工具查证后，给出简洁的结论并注明出处（文档标题、章节、页码或行号）；",
            "知识库里找不到的，直接说明没找到。");

    private final LfConfig cfg;
    private final TaskRegistry registry;

    LfRunner(LfConfig cfg, TaskRegistry registry) {
        this.cfg = cfg;
        this.registry = registry;
        DefaultModelClientFactories.ensureRegistered();
    }

    @Override
    public void run(LfTask task, RunRequest request) {
        task.emit("start", map("taskId", task.taskId(), "agentType", "loopforest"));
        ChunkTranslator translator = new ChunkTranslator(task);
        String status;
        try (McpMounts mounts = McpMounts.register(cfg, task.taskId(), request.kbIds(), request.mcpServers())) {
            int iterations = request.maxTurns() != null && request.maxTurns() > 0
                    ? request.maxTurns() : cfg.maxIterations();
            ReActAgent host = agent("loop-forest", cfg.systemPrompt(), iterations);
            host.registerRail(new ControlRail(task, true)).toCompletableFuture().join();
            mounts.attach(host);
            SubAgentExecutor branches = (userInput, subGoal) -> runBranch(task, mounts, subGoal);
            try (LoopForestSuite.Installation ignored = LoopForestSuite.builder()
                    .forkEnabled(cfg.forkEnabled())
                    .executor(branches)
                    .contextBudget(cfg.contextBudget())
                    .governorCapTokens(cfg.governorCapTokens())
                    .build()
                    .install(host)) {
                AgentSession session = AgentSession.createAgentSession("lf-" + task.taskId(), Map.of(), null);
                Iterator<Object> chunks = host.stream(compose(request), session, List.of(StreamMode.OUTPUT));
                while (chunks.hasNext()) {
                    translator.accept(chunks.next());
                }
            }
            ChunkTranslator.Outcome outcome = translator.finish(task.cancelRequested());
            status = outcome.status();
            if ("completed".equals(status)) {
                registry.remember(task.sessionId(), new TaskRegistry.Turn(request.input(), outcome.output()),
                        cfg.historyTurns());
            }
        } catch (RuntimeException | Error failure) {
            LOGGER.warn("task {} failed", task.taskId(), failure);
            task.emit("error", map("code", "ENGINE_ERROR",
                    "message", truncate(String.valueOf(failure.getMessage()), 500)));
            task.emit("done", map("status", "failed"));
            status = "failed";
        }
        task.finish(status);
    }

    private String runBranch(LfTask task, McpMounts mounts, String subGoal) {
        if (task.cancelRequested()) {
            return "（任务已取消，分支未执行）";
        }
        task.emit("branch", map("phase", "started", "goal", truncate(subGoal, 300)));
        try {
            ReActAgent branch = agent("loop-forest-branch", BRANCH_PROMPT, cfg.forkMaxIterations());
            branch.registerRail(new ControlRail(task, false)).toCompletableFuture().join();
            mounts.attach(branch);
            Object result = branch.invoke("子任务：" + subGoal, null);
            String output = result instanceof Map<?, ?> m ? String.valueOf(m.get("output")) : String.valueOf(result);
            task.emit("branch", map("phase", "finished", "goal", truncate(subGoal, 300), "chars", output.length()));
            return truncate(output, 6000);
        } catch (RuntimeException failure) {
            LOGGER.warn("branch of task {} failed", task.taskId(), failure);
            task.emit("branch", map("phase", "failed", "goal", truncate(subGoal, 300)));
            return "分支执行失败：" + truncate(String.valueOf(failure.getMessage()), 300);
        }
    }

    private ReActAgent agent(String name, String prompt, int maxIterations) {
        ReActAgent agent = new ReActAgent(AgentCard.builder().name(name).build());
        ReActAgentConfig config = ReActAgentConfig.builder().build();
        config.configureMaxIterations(maxIterations);
        // loop-forest default: the stock 10-round window drops the task statement by call 11
        config.configureContextEngine(200, 60, false);
        config.configurePromptTemplate(List.of(Map.of("role", "system", "content", prompt)));
        agent.configure(config);
        ModelClientConfig client = ModelClientConfig.builder()
                .clientId(name + "-" + System.nanoTime())
                .clientProvider("OpenAI")
                .apiKey(cfg.modelApiKey())
                .apiBase(cfg.modelBaseUrl())
                .verifySsl(true)
                .timeout(cfg.modelTimeoutSeconds())
                .build();
        ModelRequestConfig request = ModelRequestConfig.builder()
                .modelName(cfg.modelName())
                .temperature(cfg.temperature())
                .topP(0.95)
                .maxTokens(cfg.maxTokens())
                .build();
        agent.setLlm(new Model(client, request));
        return agent;
    }

    private String compose(RunRequest request) {
        List<TaskRegistry.Turn> turns = request.history();
        if (turns.isEmpty()) {
            return request.input();
        }
        StringBuilder text = new StringBuilder("【本会话之前的问答，供参考】\n");
        for (TaskRegistry.Turn turn : turns) {
            text.append("问：").append(truncate(turn.question(), 800)).append('\n')
                    .append("答：").append(truncate(turn.answer(), 1500)).append("\n\n");
        }
        return text.append("【本轮问题】\n").append(request.input()).toString();
    }
}
