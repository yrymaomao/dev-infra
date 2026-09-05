# Agent Runtime deployment composition

This repository owns deployment composition only. Supply Chain v4 business
contracts and algorithms live in `ebiz-agents`; generic execution lives in
Agent Runtime; ERP protocol translation lives in Base AI.

## Runtime 0.1.6 Model Seam release

Deployment Composition `0.1.2` adds the immutable bundle
`runtime-model-seam-0.1.6`. The bundle contains exact wheel names, SHA-256
digests and source commits for Runtime/contracts/workflow `0.1.6`, Base AI and
the OpenAI Adapter `0.1.1`, Supply Chain `4.0.0`, and all three Capability Set
v2 wheels. Schema, registry, configuration and clean-venv digests are separate
materials in the same snapshot.

Release assembly accepts wheels only. It rejects source paths, editable
installs, `PYTHONPATH`, wheelhouse extras, digest drift, raw assertion keys and
an active/previous key overlap shorter than 65 seconds. Key material remains in
Secret Manager/KMS; the bundle stores references and the
`crm-confirm-action/v1` profile digest only.

CRM migration, shared-MySQL replay, profile digest, keyring, and readiness are
independent gates. Any missing gate keeps CRM WRITE disabled while CRM READ,
the Model Seam and Manifest Profile remain active. Final release outputs
(`release-manifest.json`, `SHA256SUMS`, SPDX SBOM, migration report,
cross-repository test list and rollback instructions) cannot be generated
until every joint gate is true.

The checked lock is generated from reviewed local wheels during feature
development. Promotion does not resolve from the lock's build-machine path; it
uses only the exact wheel names and hashes in the reviewed Release Bundle.

## Runtime governance profile

Composition 0.1.2 adds a versioned `runtime.governance` block for Runtime 0.1.6
Capacity Profiles and registry-import. It records the three independent feature
switches, Capacity cache TTL, platform hard caps, delegation audience and trusted
proxy CIDRs. The deployment launcher compares every `APP_*` process value with
this reviewed block and fails before Runtime starts on a missing, malformed or
drifted value. `canonical_digest` is the configuration material digest used by
the Release Manifest.

PostgreSQL remains authoritative for published Capacity Profiles, assignments
and execution leases. `APP_REDIS_URL` enables only the advisory 60-second cache;
Redis outage does not relax Capacity enforcement. The checked example keeps
compiler, publisher and enforcement disabled for staged rollout. The delegation
secret remains an external secret reference and is never included in the
versioned configuration or its digest.

## Supply Chain v4 release

The immutable deployment selection is:

- Agent `inventory-supply-chain@4.0.0`, workflow
  `inventory-supply-chain-daily@4`.
- public Catalogs `inventory.core@2`, `commerce-sales.analytics@2`, and
  `supply-chain.planning@2` (ten exact capability versions in total).
- Base AI `0.1.1`, OpenAI Adapter `0.1.1`, ERP Adapter `yeaher.erp@0.1.0`, and
  the six public planning Providers
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
  --base-ai-provider-attestation <base-ai-provider-attestation.json> `
  --tenant-id <tenant> --actor-id <uuid> --trace-id <trace>
```

`write_base_ai_provider_attestation()` writes Runtime's closed, credential-free
attestation document from the exact Deployment pins. It contains the three
`BaseAIProviderDeployment` records and their `PluginHostPolicy`, never resolved
secrets. `build_capability_publish_commands()` supplies that attestation to all
three Runtime publisher calls. After publication, the release client uses the
existing Workflow draft/validate/publish and Agent draft/publish APIs. It fails
closed unless every response preserves the exact version identity, lifecycle
status, digest, immutable pins, and expected row-version transition.

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

Before promotion, run the narrow release-material guard with only the reviewed
configuration/diff files and exact wheels being promoted:

```powershell
$releaseMaterial = @(
  (Resolve-Path config\deployment.example.json).Path
  (Get-ChildItem C:\ebizhub\.local\deployment-v4-wheel\*.whl -File).FullName
)
& .venv-deployment\Scripts\ebiz-release-material-guard.exe $releaseMaterial
```

The guard does not walk the repository or read operator-local directories. It
rejects credential-shaped values and concrete non-loopback runtime endpoints
inside the explicitly named text files and wheel members; symbolic environment
references, reserved documentation domains, and deterministic loopback endpoints
remain valid.

## Deterministic local and real-dev

`ebiz-local-dev-assets` creates ignored local configuration with deterministic
ERP responses, a loopback structured-model endpoint, an exact Base AI provider
attestation, and Skill JSON seed bytes. A Skill file path is never passed to
the Agent: `SUPPLY_CHAIN_SKILL_INPUT_REF` must be populated with a governed
Runtime EvidenceRef UUID after those bytes are stored through Runtime's
PayloadStore/EvidenceStore seam. The separately built
`ebiz-deployment-local-evidence-fixture` wheel supplies a local-only Catalog
and inert Provider for truthful provenance; it is not an Agent import or a
production/real-dev pin. Publish that Catalog with Runtime's existing
`agent_runtime.cli.capability_publish`, then run
`ebiz-local-dev-skill-seed`. The seed command accepts only
`APP_ENV=local_dev` plus `LOCAL_DEV_E2E=true`, verifies the generated file
digest, and prints only the stable EvidenceRef UUID for
`SUPPLY_CHAIN_SKILL_INPUT_REF`.

Install or force-install every exact wheel before running `ebiz-local-dev-assets`.
Asset generation pins the installed wheel RECORD digests into the Plugin Host
Policy and Base AI attestation; any later reinstall invalidates those files.
After generating assets, do not mutate the environment until the smoke finishes;
if any wheel changes, regenerate all assets before publishing a Catalog. The
generated happy-path snapshot uses current UTC and sets
`SUPPLY_CHAIN_EXPECTED_RESULT_STATUS=COMPLETE`; an intentional BLOCKED test must
set that expectation explicitly and cannot satisfy the happy-path gate.

The live-smoke command validates flat v4 input, starts the published Agent via
`POST /v1/agent-executions`, polls Runtime, validates the public result Schema,
and requires the exact materialized EvidenceRef count:

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
wheel. Include the separate local-evidence fixture wheel only in the
deterministic-local wheelhouse; production and real-dev attestations must not
contain it. Install from those wheels into a new environment with `--no-index` and
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
- The deterministic local Agent E2E completed on 2026-08-31 with the exact
  reviewed Runtime, Base AI, Agent/Catalog, Deployment, and fixture wheels. The
  fresh run published the local fixture and v4 Catalogs, seeded the governed
  Skill, published the Workflow and Agent through Runtime's public APIs, and
  reached a terminal `COMPLETE` result with five public EvidenceRefs against
  fresh PostgreSQL, Redis, and versioned MinIO containers. Repeat this gate for
  every promoted wheel set. It remains `LOCAL_DEV_E2E`, not UAT or production
  E2E, and never treats a raw path or direct database row as evidence.
