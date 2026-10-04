# Supply Chain dev transport

This is the deployment-only loopback transport approved for the existing dev
host. It does not implement Credential Broker, obtain a global MCP Key, cache
user credentials, or replace the existing ERP login/user-key flow. It is not a
production TLS solution or a complete application deployment by itself.

## Boundaries

| Loopback listener | Fixed upstream | Allowed traffic |
| --- | --- | --- |
| 18848 | dev host 8848 | Nacos login, read-only McpServer discovery |
| 18249 | dev host 9249 | AGGR-key query; local `/mcp` maps to `/erp/aggr-mcp` POST/GET/DELETE |
| 18672 | dev host 5672 | AMQP bytes only; no management interface |

The literal dev host is defined once in `passthrough.py`. Calls cannot select
an upstream. Only a healthy, enabled, positive-weight instance at that exact
host and port 9249 is projected to 127.0.0.1:18249; unknown instances are not
fabricated. Original discovery input remains unchanged. MCP bodies, keys,
session IDs and SSE bytes are preserved. Redirects are not followed. Access
logging is disabled. The app still performs its original authorization and
HTTPS/literal-loopback checks.

`transport.compose.yaml` owns one new isolated application namespace. Runtime,
BFF and OpenClaw application overlays must explicitly share `app-network`;
they must not share an existing ERP, Ads, CRM or Java container namespace. The
transport binds only loopback, has no published host ports, contains no secrets,
and drops capabilities. Do not add host networking or public transport ports.
The overlay reuses the existing `supply-chain-dev` network and creates no
PostgreSQL, Redis, RabbitMQ or object-storage service.

## Installation gate

Use the reviewed Python-image reference in `SC_PYTHON_IMAGE`, verify its actual
image ID against the release manifest, and use the fixed source commit. Validate
the complete application overlay with `docker compose config --quiet` before
starting it. Never dump expanded configuration, Docker environment values or
raw Nacos responses to deployment logs. User-bound reference signing, ERP
session, model, storage and queue configurations must be delivered separately
through protected configuration; this overlay grants none of those permissions.

Before migration/start, verify storage versioning, supported conditional
version deletion and permissions limited to the actual Runtime namespace. Check
queue permissions and application configuration/attestation. Retain database
backups. Runtime migration uses `AGENT_RUNTIME_DATABASE_URL`; map the protected
database URL without printing it. Execute the installed commands in order:

```text
ebiz-runtime-migrate current
ebiz-runtime-migrate upgrade head
ebiz-runtime-checkpoint-setup
ebiz-supply-chain-bff --migrate-only
```

Do not use `--help` on the pinned Runtime migration CLI, stamp revisions, create
tables manually or invent a Skill/Policy reference to pass configuration checks.
Publish the exact reviewed Agent/Workflow pins using the existing publisher,
sign the Gateway generation, then enable OpenClaw/BFF. Keep public ingress
changes separate and scoped to Supply Chain; preserve all existing routes.

## Focused verification

The Windows Deployment `.venv-verify` is the documented interpreter:

```powershell
.venv-verify\Scripts\python.exe -B -m pytest deploy/supply-chain-dev/tests -q --confcutdir=deploy/supply-chain-dev/tests
.venv-verify\Scripts\python.exe -B -m ruff check deploy/supply-chain-dev
.venv-verify\Scripts\python.exe -B -m ruff format --check deploy/supply-chain-dev
.venv-verify\Scripts\python.exe -B -m mypy deploy/supply-chain-dev/passthrough.py --follow-imports=silent
```

Use workspace evidence paths for pytest/cache output. Mock tests establish only
the transport boundary, not real dev connectivity, AMQP readiness, migration,
OpenClaw installation or Forecast/Decision success. Those require fresh Jenkins
and E2E evidence. Rollback stops only this dedicated stack and restores its
previous pinned image/configuration; it does not remove shared data or services.
