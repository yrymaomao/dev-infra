# CRM OpenClaw host instance: two host processes, two signed generations

Runbook for work package P1-E of the OpenClaw x CRM integration (integrated
design `2026-09-16-openclaw-crm-integration-design.md` §5.8, §12.2, §14.3,
§16 R1-11, §17). It complements `docs/crm-profile.md` (the BFF/Runtime side)
and the Runtime repository's `docs/openclaw-integration-release-checklist.md`
("启用顺序"). Nothing here is a record of a production deployment; the local
proof is listed at the end with its limits.

## Why two host processes

The Adapter plugin (`plugin.ts`) is a module-level singleton keyed by
`deploymentPath`; a second path in one process throws
`deployment rotation requires restart`. The Runtime catalog service owns one
generation and refuses any other generation id, and invocations are keyed by
the full offer reference (which embeds the generation id). Therefore:

- each business agent is **one OpenClaw host OS process** with **one** signed
  generation that contains **only that agent's offer**, under **its own
  generation id**;
- one Runtime process keeps **one composition** with one
  `GatewayCatalogGeneration` per instance (`SharedToolGatewayComposition.instances`)
  and answers each instance from that instance's own catalog
  (`InstanceRoutedCatalog`), so the Supply Chain host can never even see
  `crm_case_advice`;
- the Supply Chain instance keeps `TOOL_GATEWAY_GENERATION_ID` exactly as a
  single-instance deployment signs it today; the CRM instance loads
  `CRM_TOOL_GATEWAY_GENERATION_ID` (default `<TOOL_GATEWAY_GENERATION_ID>-crm`).
  Both share `TOOL_GATEWAY_CATALOG_REVISION`, tenant and audience.

## Shared vs distinct between the two instances

Enforced statically by `ebiz_deployment.openclaw_instances.assert_instances_isolated`
(`DISTINCT_FIELDS` / `SHARED_FIELDS`) over `config/openclaw/<instance>/host.json`
plus the Adapter `deployment.json`; the committed examples pass it
(`tests/test_openclaw_instances.py`).

| Must differ (process-owned) | Why |
| --- | --- |
| `agentId` (`main` vs `crm`) | one BFF reception profile and one session-key prefix per agent |
| host `port`, `OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH` | one listening host process, its own state and config |
| env backing `OPENCLAW_GATEWAY_TOKEN` (`EBIZ_SUPPLY_CHAIN_GATEWAY_TOKEN` / `EBIZ_CRM_GATEWAY_TOKEN`) | one host secret each |
| `pluginInstallDir` | `openclaw.plugin.json/contracts.tools` is materialized per generation and must equal exactly that generation's offer names + the three v1 controls |
| `deploymentPath` | the plugin singleton key |
| `connector` (`supply-chain` / `crm`) | the SQLite ledger keys rows by `(connector, occurrence)` |
| `connectorCredentialEnv` / `ingressCredentialEnv` (`^EBIZ_[A-Z0-9_]+$`) | one bearer pair per instance; the BFF dials each instance's ingress with its own `BFF_*_OPENCLAW_INGRESS_CREDENTIAL` |
| `credentialUrl` / `endUrl` | each BFF profile issues and ends its own runs (`/api/supply-chain/v2/openclaw/...` vs `/api/crm/v2/openclaw/...`) |
| signed generation file, `generationId`, `expectedPayloadDigest` | one generation per instance |
| `ledgerPath` | SQLite holds a process-lifetime ownership lock |
| `workspaceDir`, reception file | one embedded-agent workspace and one reception per agent |

| May / must be shared | Note |
| --- | --- |
| the sealed host build and the reviewed visibility patch (`hostRoot`) | one `compatibility.json` pin serves both |
| `gatewayOrigin` (one Runtime), `tenantId`, `audience` | one deployment, one tenant (D-2) |
| the signing key | a distinct `key_id` per instance is cleaner but not required; the trust JSON of each instance lists only the keys it accepts |
| `BFF_OPENCLAW_CONNECTOR_CREDENTIAL`, `TOOL_GATEWAY_JWT_KEY` | one BFF, one run-JWT verifier; the *values* are shared, the env *names* on the two hosts are not |

## Environment

BFF and Runtime variables are in `docs/crm-profile.md`; the ones that
matter for the hosts:

| Variable | Process | Meaning |
| --- | --- | --- |
| `TOOL_GATEWAY_GENERATION_ID` | Runtime | the Supply Chain instance's generation id (unchanged) |
| `CRM_TOOL_GATEWAY_GENERATION_ID` | Runtime | the CRM instance's generation id; default `<TOOL_GATEWAY_GENERATION_ID>-crm`; must differ |
| `TOOL_GATEWAY_CATALOG_REVISION` | Runtime | shared catalog revision of both generations |
| `BFF_OPENCLAW_AGENT_ID` / `BFF_CRM_OPENCLAW_AGENT_ID` | BFF + Runtime | `main` / `crm`; the instance keys of the composition |
| `BFF_OPENCLAW_INGRESS_URL` / `BFF_CRM_OPENCLAW_INGRESS_URL` | BFF | `http://127.0.0.1:18789/...` / `http://127.0.0.1:18790/...`, one per host |
| `BFF_OPENCLAW_INGRESS_CREDENTIAL` / `BFF_CRM_OPENCLAW_INGRESS_CREDENTIAL` | BFF | equal to the value behind each host's `ingressCredentialEnv` |
| `AGENT_RUNTIME_DATABASE_URL` | export job | Registry read for `ebiz-gateway-generation-export` |
| `OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH`, `OPENCLAW_GATEWAY_TOKEN` | each host | per-process values (see `config/openclaw/instances.env.example`) |
| `EBIZ_SUPPLY_CHAIN_CONNECTOR`, `EBIZ_SUPPLY_CHAIN_INGRESS` | Supply Chain host | connector / ingress bearers |
| `EBIZ_CRM_CONNECTOR`, `EBIZ_CRM_INGRESS` | CRM host | connector / ingress bearers |

## Enablement order (per the release checklist)

Nothing below is a record of having been done.

1. **Pin.** CRM agent wheel (`ebiz-agent-crm` 2.0.0), CRM adapter wheel, Runtime
   0.1.6, the Adapter package (wire v1 with `exact_bare_token`,
   `codex/adapter-exact-bare-token` @ `9fbf1ee`; Phase 2 wire v2 is not used),
   the sealed host (`2026.9.2`, `8dcf395…`, patch SHA-256
   `e698250b…`). Record RECORD digests (`CRM_AGENT_RECORD_DIGEST`,
   `CRM_ADAPTER_RECORD_DIGEST`), the base tarball digest and the
   `compatibility.json` pin.
2. **Migrate with admission off.** `BFF_CRM_OPENCLAW_ENABLED=false`,
   `CRM_TOOL_GATEWAY_ENABLED=false`; Alembic to `0009_conversation_profile_index`.
3. **Publish** `crm@2`, `crm-advise@1`, `crm-case-advise-on-demand@1` with
   `ebiz-crm-advise-publish --expect-digest <reviewed>`; pin the printed checksum
   as `CRM_ADVISOR_WORKFLOW_DIGEST`.
4. **Export, sign, materialize - once per instance.**

   ```text
   # Runtime env + AGENT_RUNTIME_DATABASE_URL; CRM_TOOL_GATEWAY_ENABLED=true here so the CRM pin is exported
   ebiz-gateway-generation-export --out-dir <new dir> --audience <deployment audience> --format json
   #  -> generation-main.json (only inventory_supply_chain_on_demand, id TOOL_GATEWAY_GENERATION_ID)
   #  -> generation-crm.json  (only crm_case_advice, id CRM_TOOL_GATEWAY_GENERATION_ID)
   #  prints expected_payload_digest per instance; files are never overwritten
   node <adapter>/scripts/sign-generation.mjs generation-main.json <key.pem> <key_id> <sc-root>/generation-main.signed.json
   node <adapter>/scripts/sign-generation.mjs generation-crm.json  <key.pem> <key_id> <crm-root>/generation-crm.signed.json
   # per instance: current-trust.json = {path, deploymentRoot, publicKeys, expectedPayloadDigest, tenantId, audience, generationId}
   node <sc-install>/scripts/prepare-plugin-manifest.mjs  <sc-install>  <sc-root>/current-trust.json
   node <crm-install>/scripts/prepare-plugin-manifest.mjs <crm-install> <crm-root>/current-trust.json
   ```

   Record base and materialized manifest digests. Keep the previous signed
   generation of each instance in its `retainedGenerations` for old-operation
   result validation. Render each `deployment.json` from
   `config/openclaw/<instance>/deployment.example.json` with
   `render_adapter_deployment` (reception inlined from
   `config/<instance>-reception.v1.json`, trust pins from the export receipt),
   then run `assert_instances_isolated` over both instances.
5. **Enable the gateway side**: `CRM_TOOL_GATEWAY_ENABLED=true` (Runtime now
   composes both instances), `BFF_CRM_OPENCLAW_ENABLED=true`; start the CRM host
   process with its own env block, then check the R1-11 list below.
6. **Fake CRM chain**: with a Fake CRM behind `yeaher.crm`, discover/describe/
   invoke `crm_case_advice` from the CRM host, run binding,
   `/internal/crm/v2/openclaw/authorize`, four-segment result projection.
7. **Real dev CRM** only after the crm-service JWT change and the
   `session-principal` route are deployed; until then every CRM call segment
   answers 401 and the acceptance log says so.
8. **Rotation**: a new generation means a new export/sign/materialize for that
   instance and a restart of that host process only (same PID lifecycle is
   supported by the host: stop the plugin service, materialize, register).

## R1-11 checklist

- [ ] `ebiz-gateway-generation-export` receipt shows two instances, tool names
      exactly `[inventory_supply_chain_on_demand]` and `[crm_case_advice]`,
      distinct generation ids, same tenant / audience / catalog revision.
- [ ] Each signed envelope's payload digest equals the receipt's
      `expected_payload_digest`; each `current-trust.json` pins its own
      generation id, digest, tenant and audience.
- [ ] Each install directory's `openclaw.plugin.json/contracts.tools` equals its
      generation's offer names + `ebiz_operation_status`, `ebiz_operation_result`,
      `ebiz_operation_cancel`; `assertPluginManifest(<sc-install>, <crm generation>)`
      and the reverse are refused.
- [ ] `assert_instances_isolated` passes over both instances.
- [ ] Each host registers exactly one factory whose names contain only its own
      offer; each ingress answers 403 without its bearer and refuses the other
      instance's bearer.
- [ ] A second `deploymentPath` in one host process is refused
      (`deployment rotation requires restart`); a restart of the plugin service
      with a new generation id under the same path and PID is accepted only when
      the new trust pins, the new signed artifact and the re-materialized
      manifest agree.

## Local proof (2026-09-16)

Evidence root: `C:\ebizhub\.local\test-runs\crm-host-20260916\`. Everything
below ran without VPN, Runtime, BFF, model or Registry; the two payloads are
fixture projections of the real two-instance composition (see
`export/receipt.json`), signed with a throwaway key generated for this run.

| Step | Script / command | Evidence |
| --- | --- | --- |
| export | `export_fixture_generations.py` (WSL venv, deployment code path) | `export/receipt.json`, `export/r1/generation-{main,crm}.json`, `export/r2/…` |
| sign + materialize | `sign-and-materialise.mjs` → adapter `scripts/sign-generation.mjs`, `scripts/prepare-plugin-manifest.mjs`, `dist/plugin-manifest.js` | `manifest-proof.json` (21 steps), `instances/<name>/{generations,plugin/package,current-trust.json,deployment.json,host.json}` |
| isolation | `validate_instances.py` (`assert_instances_isolated`) | `instance-isolation.json` |
| singleton + same-PID rotation | `singleton-proof.mjs` (installed `dist/plugin.js`, fake host API, sealed host entry) | `singleton-proof.json` |
| host bring-up | `host-bringup.mjs` (`openclaw.mjs gateway run`, ports 28789/28790) | `host-bringup.json`, `instances/<name>/openclaw.log`, `host-stdio.redacted.log` |

Results of that run:

- export: `main` → `crm-host-local-1` / `[inventory_supply_chain_on_demand]`,
  `crm` → `crm-host-local-1-crm` / `[crm_case_advice]`; tenant `tenant-local`,
  audience `ebizhub-openclaw-local`, catalog revision `crm-host-local-catalog-1`
  (r2: `crm-host-local-2`, `crm-host-local-2-crm`).
- `sign-generation.mjs` exit 0 for all three envelopes;
  `prepare-plugin-manifest.mjs` exit 0 for both install directories;
  `contracts.tools` = own offer + three controls; `assertPluginManifest`
  accepts own / refuses the other generation
  (`plugin manifest requires current generation materialization`); wrong
  tenant, generation id or digest pins refused by `loadGeneration`.
- `assert_instances_isolated` passes over the materialised material; copying
  the CRM `ledgerPath` into the Supply Chain instance is refused.
- singleton: the CRM `deploymentPath` in the process that owns the Supply Chain
  path throws `deployment rotation requires restart`; after the plugin service
  stops, the same path with the r2 artifact, r2 trust pins and re-materialised
  manifest registers again in the same PID, while r2 pins over the r1 artifact
  are refused.
- host bring-up: `openclaw.mjs gateway run` (OpenClaw 2026.9.2 `8dcf395`, the
  sealed checkout verified offline by the Adapter's `assertSupportedHost`) on
  ports 28789 (Supply Chain, generation `crm-host-local-2` after the rotation
  above) and 28790 (CRM, `crm-host-local-1-crm`); both ingresses answer 403
  without their bearer, accept their own bearer and refuse the other instance's;
  each host logged one full registration with one factory and a
  `reception-config` digest that equals the digest computed with its own
  generation names and cannot be produced with the other instance's names;
  both processes were stopped with `taskkill /T`.

Limits: no turn was submitted (no model, Runtime or BFF ran), so
`factory-result` names were not observed; the same-PID rotation was exercised
through the installed plugin's `registerAdapter` with a host-shaped API, not
through the host's `gateway.restart.request`; port 18789 was held by an
unrelated process on the machine and left untouched. None of this is UAT or
production evidence.
