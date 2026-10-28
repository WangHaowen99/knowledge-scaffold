package com.kbsuite.lf;

import com.openjiuwen.core.foundation.tool.mcp.McpServerConfig;
import com.openjiuwen.core.runner.Runner;
import com.openjiuwen.core.runner.base.Result;
import com.openjiuwen.core.singleagent.BaseAgent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * MCP servers of one run: kb-service (scoped by X-KB-Scope) plus the servers the request
 * carries in {@code runtimeConfig.mcpServers} (e.g. a team's coordination tools, bridged by
 * the harness provider). Registered process-wide under ids unique to the run and removed
 * (which also disconnects them) when the run ends.
 */
final class McpMounts implements AutoCloseable {

    private static final Logger LOGGER = LoggerFactory.getLogger(McpMounts.class);
    private static final double CALL_TIMEOUT_S = 300;

    private final List<McpServerConfig> configs = new ArrayList<>();

    private McpMounts() {
    }

    /**
     * Registers the run's MCP servers; servers that fail to connect are skipped.
     *
     * @param cfg service settings
     * @param taskId run id (makes the registration ids unique)
     * @param kbIds knowledge bases the run may read (empty: every one the key sees)
     * @param runtimeServers {@code runtimeConfig.mcpServers} of the request
     * @return the registrations, to attach to agents and close at the end
     */
    static McpMounts register(LfConfig cfg, String taskId, List<String> kbIds,
            List<Map<String, Object>> runtimeServers) {
        McpMounts mounts = new McpMounts();
        if (!cfg.kbMcpUrl().isBlank()) {
            Map<String, String> headers = new LinkedHashMap<>();
            headers.put("Authorization", "Bearer " + cfg.kbMcpKey());
            if (!kbIds.isEmpty()) {
                headers.put("X-KB-Scope", String.join(",", kbIds));
            }
            mounts.add(config("kb", taskId, cfg.kbMcpUrl(), "streamable-http", headers, CALL_TIMEOUT_S));
        }
        for (Map<String, Object> server : runtimeServers) {
            if (Boolean.FALSE.equals(server.get("enabled"))) {
                continue;
            }
            String name = first(server, "serverName", "name", "serverId", "id");
            String url = first(server, "serverPath", "url");
            if (name == null || url == null) {
                LOGGER.warn("runtime mcp server skipped: name or url missing");
                continue;
            }
            Map<String, String> headers = new LinkedHashMap<>();
            if (server.get("authHeaders") instanceof Map<?, ?> raw) {
                raw.forEach((k, v) -> headers.put(String.valueOf(k), String.valueOf(v)));
            }
            double timeout = server.get("callTimeoutSeconds") instanceof Number n ? n.doubleValue() : CALL_TIMEOUT_S;
            String type = first(server, "clientType", "type");
            mounts.add(config(name, taskId, url, type == null ? "streamable-http" : type, headers, timeout));
        }
        return mounts;
    }

    /**
     * Makes the registered servers' tools visible to {@code agent}.
     *
     * @param agent main or branch agent
     */
    void attach(BaseAgent agent) {
        for (McpServerConfig config : configs) {
            var added = agent.getAbilityManager().add(config);
            if (!added.isAdded()) {
                LOGGER.warn("mcp server {} not visible: {}", config.getServerName(), added.getReason());
            }
        }
    }

    @Override
    public void close() {
        for (McpServerConfig config : configs) {
            try {
                Runner.resourceMgr().removeMcpServer(List.of(config.getServerId()), null, null, null, true);
            } catch (RuntimeException ex) {
                LOGGER.warn("mcp server {} not unregistered: {}", config.getServerId(), ex.getMessage());
            }
        }
        configs.clear();
    }

    private void add(McpServerConfig config) {
        List<Result<String>> results = Runner.resourceMgr().addMcpServer(config, "lf-service", null);
        if (results.stream().anyMatch(Result::isError)) {
            LOGGER.warn("mcp server {} unavailable, its tools are skipped", config.getServerName());
            return;
        }
        configs.add(config);
    }

    private static McpServerConfig config(String name, String taskId, String url, String type,
            Map<String, String> headers, double timeoutSeconds) {
        McpServerConfig config = McpServerConfig.builder()
                .serverId(name + "@" + taskId)
                .serverName(name)
                .serverPath(url)
                .clientType(type)
                .params(new LinkedHashMap<>())
                .build();
        config.setAuthHeaders(headers);
        config.setCallTimeoutSeconds(timeoutSeconds);
        return config;
    }

    private static String first(Map<String, Object> server, String... keys) {
        for (String key : keys) {
            Object value = server.get(key);
            if (value != null && !String.valueOf(value).isBlank()) {
                return String.valueOf(value);
            }
        }
        return null;
    }
}
