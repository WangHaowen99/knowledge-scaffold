# Modeling MCP HTTPS Gateway

This template provides one HTTPS entry point for all project MCP endpoints:

```text
https://mcp.example.com/{endpointId}/read/mcp
https://mcp.example.com/{endpointId}/change/mcp
```

The included `docker-compose.yml` renders `nginx.conf.template` with only the
`MODELING_MCP_*` variables, preserving Nginx runtime variables such as `$host`.
The gateway must share a network with the Modeling service.

Required values:

```text
MODELING_MCP_SERVER_NAME=mcp.example.com
MODELING_MCP_UPSTREAM=modeling:5188
MODELING_MCP_TLS_CERTIFICATE=/etc/nginx/tls/fullchain.pem
MODELING_MCP_TLS_CERTIFICATE_KEY=/etc/nginx/tls/privkey.pem
```

For the provided Compose file, configure:

```text
MODELING_MCP_SERVER_NAME=mcp.example.com
MODELING_MCP_UPSTREAM=modeling:5188
MODELING_MCP_TLS_DIR=/opt/modeling-mcp/tls
CONSUMER_NETWORK=knowledge-scaffold_default
```

Then start the gateway:

```bash
docker compose -f modeling/deploy/mcp-gateway/docker-compose.yml up -d
```

The Modeling service must use the same external origin:

```text
MODELING_MCP_PUBLIC_BASE_URL=https://mcp.example.com
```

Explicit configuration has the highest priority. When it is absent, the
deployment scripts continue the deployment and select the MCP base URL in this
order: detected public IPv4, detected LAN IPv4, then `127.0.0.1`. An
automatically selected public IP uses HTTP on the configured MCP port and emits
a security warning; production Internet access should replace it with this
HTTPS gateway and an explicitly configured URL.

Certificate issuance, renewal, DNS, firewall policy, rate limiting, and public
load balancing remain deployment responsibilities and are intentionally not
embedded in the application process.
