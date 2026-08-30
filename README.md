# Agent Runtime deployment composition

This repository owns deployment composition only. Supply Chain v4 business
contracts and algorithms live in `ebiz-agents`; generic execution lives in
Agent Runtime; ERP protocol translation lives in Base AI.

## Supply Chain v4 release

The immutable deployment selection is:

- Agent `inventory-supply-chain@4.0.0`, workflow
  `inventory-supply-chain-daily@4`.
- public Catalogs `inventory.core@2`, `commerce-sales.analytics@2`, and
  `supply-chain.planning@2` (ten exact capability versions in total).
- Base AI ERP Adapter `yeaher.erp@0.1.0` and the six public planning Providers
  from `ebiz-capability-supply-chain==2.0.0`.
- effects are READ/PREVIEW only.

The Agent wheel is resource-only and intentionally has no
`ebiz_agents.providers` entry point. The public planning wheel owns that entry
point. Supply Chain has no Cockpit operation, endpoint, secret, or Provider.
FBA/MIXED, Knowledge Context, writes, Skill Store, BFF, and frontend services
are outside this release.

## Publication surfaces

Deployment does not create a publisher and never constructs `running_app()`.
Each Catalog is published with Runtime's existing command:

```powershell
python -m agent_runtime.cli.capability_publish `
  --manifest <catalog-contract-root>\capabilities.yaml `
  --contract-root <catalog-contract-root> `
  --policy <publisher-policy> `
  --tenant-id <tenant> --actor-id <uuid> --trace-id <trace>
```

`ebiz_deployment.release.build_capability_publish_commands()` produces the
auditable argument vectors for all three Catalogs. After publication,
`build_agent_draft_payload()` produces the exact ten capability pins and the
workflow pin consumed by Runtime's existing Agent draft/publish API.

## Configuration and security

Start from:

- `config/deployment.example.json`
- `config/runtime-plugin-policy.example.json`
- `config/deployment.env.example`

All identities, versions, workflow digest, and canonical wheel RECORD digests
are mandatory. Unknown fields and extra/missing operations fail closed. ERP
access is limited to five allowlisted read-only MCP tools. The Adapter validates
the credential broker's trusted tenant binding; Deployment never stores an MCP
key or login token.

The attestation command derives digests from installed, non-editable wheels:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
.venv-deployment\Scripts\ebiz-runtime-attestation.exe
```

It covers the Agent, both resource-only Catalog wheels, the planning Provider
wheel, and the three Base AI Adapter wheels. Tampered, editable, shadowed,
symlinked, wrong-version, or wrong-entry-point distributions are rejected.

## Deterministic local and real-dev

`ebiz-local-dev-assets` creates ignored local configuration with deterministic
ERP Evidence and a loopback structured-model endpoint. It is suitable only for
`LOCAL_DEV_E2E`: `real_erp_calls=false`, `production_model_calls=false`, and
`production_e2e_verified=false`.

The live-smoke command validates that local v4 input is `NA_COMPANY` and
AUTO/FBM, then emits Runtime API launch metadata. Runtime remains responsible
for execution and persistence:

```powershell
.venv-deployment\Scripts\ebiz-supply-chain-live-smoke.exe
```

Real-dev files must stay under an ignored `.local` directory and inject the
MCP endpoint, key, broker URL, and model endpoint at process start. This repo
contains no fixed development/production endpoint or credential and does not
call an intranet service during build or test.

## Local infrastructure

`docker-compose.yml` provides PostgreSQL, Redis, and MinIO only. Passwords and
ports must be supplied as environment variables. No Runtime image is claimed:
there is no approved Runtime Dockerfile in this repository.

## Wheel build and verification

Build every local package into an artifact directory under
`C:\ebizhub\.local`, including Runtime/contracts/workflow, Base AI and its
three Adapters, Agent v4, the three capability wheels, and this deployment
wheel. Install from those wheels into a new environment with `--no-index` and
`--no-deps` only after all dependency wheels are present; do not use editable
sources for the clean-wheel proof.

For this repository (not a `test.ps1` supported target), follow
`C:\ebizhub\test.md` and run:

```powershell
.venv-verify\Scripts\python.exe -B -m pytest -q `
  --basetemp=C:\ebizhub\.local\pytest-deployment-v4
.venv-verify\Scripts\python.exe -B -m ruff check src tests
.venv-verify\Scripts\python.exe -B -m mypy src
uv lock --check
uv build --wheel --no-sources --out-dir C:\ebizhub\.local\deployment-v4-wheel
```

## Known external blockers

- The BI source repository and its seven-window ADS migration are not present
  in this workspace, so cross-repository BI verification is pending.
- real-dev ERP MCP, real model, UAT data, owner approval, and production
  credentials/data are pending. No result from this repository is production
  E2E.
- Runtime must propagate the trusted bound credential resolver into the MCP
  bootstrap context before the X-MCP-Key composition can start; Deployment
  intentionally fails closed until that generic Runtime path is available.
