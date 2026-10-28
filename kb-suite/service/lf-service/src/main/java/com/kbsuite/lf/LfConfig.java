package com.kbsuite.lf;

import java.util.Map;

/**
 * Service settings, all from environment variables (secrets never come from files in the repo).
 *
 * @param port HTTP port
 * @param engineSecret value every /api/agents call must carry in X-Engine-Secret
 * @param modelBaseUrl OpenAI-compatible endpoint
 * @param modelApiKey key for that endpoint
 * @param modelName model to run
 * @param temperature sampling temperature
 * @param maxTokens completion token cap per model call
 * @param maxIterations ReAct iterations of the main loop
 * @param modelTimeoutSeconds timeout of one model call
 * @param kbMcpUrl kb-service MCP endpoint (streamable HTTP)
 * @param kbMcpKey kb-service key with the retrieve capability
 * @param forkEnabled whether the model may fork sub-goals into branch agents
 * @param forkMaxIterations ReAct iterations of one branch agent
 * @param contextBudget whether the token governor guards the context size
 * @param governorCapTokens context cap enforced by the governor
 * @param historyTurns earlier question/answer pairs carried into a conversation's next turn
 * @param taskRetentionSeconds how long finished tasks stay replayable
 * @param systemPrompt role prompt of the main agent
 */
record LfConfig(
        int port,
        String engineSecret,
        String modelBaseUrl,
        String modelApiKey,
        String modelName,
        double temperature,
        int maxTokens,
        int maxIterations,
        double modelTimeoutSeconds,
        String kbMcpUrl,
        String kbMcpKey,
        boolean forkEnabled,
        int forkMaxIterations,
        boolean contextBudget,
        long governorCapTokens,
        int historyTurns,
        long taskRetentionSeconds,
        String systemPrompt) {

    static final String DEFAULT_SYSTEM_PROMPT = String.join("\n",
            "你是 Loop Forest 分析助手，运行在 kb-suite 里，所有结论都要以知识库资料为依据。",
            "1. 先用 kb-service 的知识库工具查证（search_knowledge 混合检索、search_wiki 查 Wiki、",
            "   read_document 按顺序读文档、grep_documents 查精确字串、list_knowledge_bases 看有哪些库），再作答。",
            "2. 每条结论注明出处（文档标题、章节、页码或行号）；知识库里没有的，直接说明没找到，不要用常识补全。",
            "3. 问题可以拆成几个能独立查证的子问题时，用分叉工具分别探索，最后汇总、对照不一致之处。",
            "4. 用中文回答，先给结论，再给依据。");

    /**
     * Reads the settings; the secret, model and kb-service key fall back to the variables
     * kb-suite already defines (DA_ENGINE_SECRET, KBS_LLM_*, KBS_MCP_KEY).
     *
     * @param env process environment
     * @return settings
     */
    static LfConfig fromEnv(Map<String, String> env) {
        String secret = either(env, "LF_ENGINE_SECRET", "DA_ENGINE_SECRET");
        if (secret.isBlank()) {
            throw new IllegalStateException("LF_ENGINE_SECRET (or DA_ENGINE_SECRET) is required");
        }
        String base = either(env, "LF_MODEL_BASE_URL", "KBS_LLM_BASE_URL");
        if (base.isBlank()) {
            throw new IllegalStateException("LF_MODEL_BASE_URL (or KBS_LLM_BASE_URL) is required");
        }
        return new LfConfig(
                intOf(env, "LF_PORT", 8193),
                secret,
                base,
                either(env, "LF_MODEL_API_KEY", "KBS_LLM_API_KEY"),
                env.getOrDefault("LF_MODEL_NAME", "qwen-plus"),
                doubleOf(env, "LF_TEMPERATURE", 0.7),
                intOf(env, "LF_MAX_TOKENS", 8000),
                intOf(env, "LF_MAX_ITERATIONS", 36),
                doubleOf(env, "LF_MODEL_TIMEOUT_S", 600),
                env.getOrDefault("LF_KB_MCP_URL", ""),
                either(env, "LF_KB_MCP_KEY", "KBS_MCP_KEY"),
                boolOf(env, "LF_FORK_ENABLED", true),
                intOf(env, "LF_FORK_MAX_ITERATIONS", 12),
                boolOf(env, "LF_CONTEXT_BUDGET", true),
                longOf(env, "LF_GOVERNOR_CAP_TOKENS", 200_000L),
                intOf(env, "LF_HISTORY_TURNS", 6),
                longOf(env, "LF_TASK_RETENTION_S", 1800),
                env.getOrDefault("LF_SYSTEM_PROMPT", "").isBlank()
                        ? DEFAULT_SYSTEM_PROMPT : env.get("LF_SYSTEM_PROMPT"));
    }

    private static String either(Map<String, String> env, String key, String fallbackKey) {
        String v = env.get(key);
        if (v == null || v.isBlank()) {
            v = env.get(fallbackKey);
        }
        return v == null ? "" : v.trim();
    }

    private static int intOf(Map<String, String> env, String key, int fallback) {
        String v = env.get(key);
        return v == null || v.isBlank() ? fallback : Integer.parseInt(v.trim());
    }

    private static long longOf(Map<String, String> env, String key, long fallback) {
        String v = env.get(key);
        return v == null || v.isBlank() ? fallback : Long.parseLong(v.trim());
    }

    private static double doubleOf(Map<String, String> env, String key, double fallback) {
        String v = env.get(key);
        return v == null || v.isBlank() ? fallback : Double.parseDouble(v.trim());
    }

    private static boolean boolOf(Map<String, String> env, String key, boolean fallback) {
        String v = env.get(key);
        return v == null || v.isBlank() ? fallback : Boolean.parseBoolean(v.trim());
    }
}
