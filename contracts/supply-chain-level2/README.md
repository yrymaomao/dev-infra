# Supply Chain Expert Level 2 contracts

This directory is the source of truth for the Level 2 public surface.

- `openapi.yaml` owns browser/BFF HTTP semantics.
- `mcp-tools.schema.json` owns the five additive read-only MCP request and response shapes.
- `mq/report-batch-requested.v1.schema.json` owns the opaque RabbitMQ event; SKU arrays are deliberately forbidden.
- `policy.v1.schema.json` owns immutable policy documents.
- `forecast.v1.schema.json` owns deterministic 13-week forecast output.
- `fixtures/` contains executable, non-secret contract examples.

Runtime 0.1.6 remains business-neutral. Tenant identity is injected from trusted execution context and therefore does not appear in MCP request payloads.
