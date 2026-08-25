# Agent Runtime production composition

This directory owns deployment composition only. It does not contain Runtime,
Base AI, Adapter, Cockpit, ERP, or business-Agent code.

The launcher constructs a `BaseAICompositionRoot` and calls
`agent_runtime.cli.api.main(provider_composition=...)`. Runtime independently
discovers the installed Supply Chain wheel through `ebiz_agents.providers`
using the separate Runtime plugin policy.

## Security and read-only boundary

- MCP exposes only `query_inventory_summary`,
  `query_inventory_by_warehouse`, and
  `query_purchase_in_transit_details`.
- ERP exposes only `inventory.get_total_snapshot` and
  `sales_profit.get_sku_windows`.
- OpenAI exposes only `responses.create_structured`.
- The Supply Chain plugin has READ/PREVIEW permissions, no egress, and no
  secret slots.
- No ERP write, notification, or generic unbounded MCP tool is configurable.
- Unknown configuration fields fail validation.

`EnvironmentSecretResolver` accepts symbolic names from `secrets.allowed_env`
only. `HttpsCredentialBrokerResolver` performs a new authenticated HTTPS call
for every `(credential_ref, provider_id)` resolution. It has no token cache,
does not accept a startup user credential, and emits only sanitized errors.
Loopback HTTP exists solely so tests can use a real local socket.

## Artifact pin injection

Build every local dependency as a wheel into the deployment-owned wheelhouse.
`--no-sources` prevents workspace/editable resolution:

```powershell
New-Item -ItemType Directory -Force wheelhouse | Out-Null
uv build --project C:\ebizhub\workspace\ebizhub-agent-runtime\packages\shared-schemas\python --out-dir wheelhouse --wheel --no-sources
uv build --project C:\ebizhub\workspace\ebizhub-agent-runtime\packages\workflow_runtime --out-dir wheelhouse --wheel --no-sources
uv build --project C:\ebizhub\workspace\base-ai\packages\base-ai --out-dir wheelhouse --wheel --no-sources
uv build --project C:\ebizhub\workspace\ebizhub-agent-runtime\packages\agent_runtime --out-dir wheelhouse --wheel --no-sources
uv build --project C:\ebizhub\workspace\base-ai\adapters\mcp --out-dir wheelhouse --wheel --no-sources
uv build --project C:\ebizhub\workspace\base-ai\adapters\erp --out-dir wheelhouse --wheel --no-sources
uv build --project C:\ebizhub\workspace\base-ai\adapters\models\openai --out-dir wheelhouse --wheel --no-sources
uv build --project C:\ebizhub\workspace\ebiz-agents\supply-chain --out-dir wheelhouse --wheel --no-sources
```

Create the independent, reproducible deployment environment. It does not use
Runtime's development venv:

```powershell
uv lock --find-links wheelhouse
$env:UV_PROJECT_ENVIRONMENT = '.venv-deployment'
uv sync --locked --group contract --find-links wheelhouse
```

Obtain all four canonical RECORD attestations from that clean environment:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
.venv-deployment\Scripts\ebiz-runtime-attestation.exe
```

The attestation command derives the Supply Chain digest from the installed
distribution; it accepts no caller-provided digest. It rejects editable,
tampered, shadowed, path-escaping, symlinked, wrong-version, and wrong-entry-
point installations. Copy the emitted values into the deployment environment.
The launcher requires every package version and digest; no defaults exist.

Start from:

- `config/deployment.example.json`
- `config/runtime-plugin-policy.example.json`
- `config/deployment.env.example`

Examples contain placeholders only. They contain no endpoint credential,
MCP key, model key, or static user credential.

## Preflight and launch

Install these production wheels into the same immutable environment:

- `ebizhub-agent-runtime[base-ai]`
- `base-ai`
- `ebiz-adapter-mcp`
- `ebiz-adapter-erp`
- `ebiz-adapter-model-openai`
- `ebiz-agent-inventory-supply-chain`
- `ebiz-deployment-composition`

Set all variables documented by `deployment.env.example`, then run:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
.venv-deployment\Scripts\ebiz-runtime-deployment.exe --check
.venv-deployment\Scripts\ebiz-runtime-deployment.exe --host 0.0.0.0 --port 8000
```

`PYTHONDONTWRITEBYTECODE` must be set before the Python process starts. The
deployment launcher verifies `sys.dont_write_bytecode is True` before building
composition or handing control to Runtime; an environment value added after
process startup is not accepted as proof.

Preflight fails before Runtime startup when any endpoint, model/broker secret,
policy file, package version, RECORD digest, entry point, API version, operation
allowlist, egress host, or Skill root is missing or inconsistent. Runtime then
performs its own installed entry-point, artifact identity, health, database,
checkpoint, object-store, and model-schema checks.

No Runtime service was added to `docker-compose.yml`: this repository currently
has no approved Runtime image or Dockerfile, so claiming a runnable container
would be misleading.

## Full Supply Chain live smoke

This smoke uses real PostgreSQL persistence, LangGraph checkpoint tables,
Redis, MinIO, the installed Supply Chain wheel, ERP/MCP providers, and the
configured production model provider. It does not use an in-memory repository,
fixture capability, deterministic Provider response, or model stub.

Start the infrastructure from WSL. Set `POSTGRES_PORT=5433` when the WSL host
already runs PostgreSQL on 5432:

```powershell
$env:POSTGRES_PORT = '5433'
wsl.exe -- docker compose -f /mnt/c/ebizhub/workspace/dev-infra/docker-compose.yml up -d postgres redis minio
```

Apply Runtime migrations, initialize checkpoints, create and version the
configured MinIO bucket, and complete the normal deployment preflight described
above. Then start `ebiz-runtime-deployment` as a supervised host process. Once
`GET /openapi.json` and `GET /v1/plugins/health` are ready, provide every
`SUPPLY_CHAIN_*` value from `config/deployment.env.example` and run:

```powershell
.venv-deployment\Scripts\ebiz-supply-chain-live-smoke.exe
```

The command publishes the ten installed Capability contracts idempotently,
publishes and executes `inventory-supply-chain-daily@1` over TCP, and verifies
the terminal execution, Runtime events, the exact EvidenceRef count in
PostgreSQL, and the mutually exclusive `complete_result` branch. Missing real
credentials, unavailable external Providers, blocked evidence, model failure,
or an ambiguous result makes the command exit nonzero. The JSON success record
states `real_erp_calls=true` and `production_model_calls=true`; it is emitted
only after those checks finish.

## Tests

Tests run from the locked clean-wheel environment. The composition contract
discovers and starts the real MCP, ERP, and OpenAI factories, checks three live
registrations and startup health, and verifies reverse lifecycle close. Adapter
health is deliberately non-networking; credential-broker tests use a real
loopback HTTP server and the production resolver. The restart contract launches
two independent child processes without a `-B` argument, relies on the required
startup environment invariant, and proves neither startup creates Agent
`__pycache__` or `.pyc` files. No fake factory or resolver is used.

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
.venv-deployment\Scripts\python.exe -B -m pytest -q
.venv-deployment\Scripts\python.exe -B -m ruff check src tests
.venv-deployment\Scripts\python.exe -B -m mypy src
uv build --wheel --no-sources
```

`-B` keeps the parent quality process clean, but is not the production control.
The launcher-enforced `sys.dont_write_bytecode` invariant is authoritative;
attestation continues to reject any pre-existing unrecorded bytecode shadow.
