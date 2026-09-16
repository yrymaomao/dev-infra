# CRM profile: read-only Case advice through the shared OpenClaw reception

Phase 1 of the OpenClaw x CRM integration (integrated design
`docs/superpowers/specs/2026-09-16-openclaw-crm-integration-design.md` of the
Runtime repository, decisions D-1, D-2, D-4, D-5). One offer, one input, no
WRITE. Nothing here is live until every step of the enablement order below has
its evidence.

## Chain

```text
emf-crm Case page
  |  POST /api/crm/v2/openclaw/session          (_token_ header/cookie -> 120 s BFF JWT)
  |  POST /api/crm/v2/openclaw/conversations     (Bearer BFF JWT)
  |  POST .../conversations/{cid}/turns, GET .../turns/{tid}, GET .../turns/{tid}/events (SSE)
  v
BFF: ebiz_deployment.openclaw_reception x crm_reception profile
  |  GET  crm-service /ai/read/v1/crm/session-principal   (session exchange only)
  |  worker: GET/POST <BFF_CRM_OPENCLAW_INGRESS_URL>/../turns  (ingress bearer)
  v
CRM OpenClaw host instance (its own Adapter process, agent id BFF_CRM_OPENCLAW_AGENT_ID)
  |  POST /api/crm/v2/openclaw/credentials, runs/end       (connector bearer -> run JWT)
  |  wire v1 invoke crm_case_advice {case_id}              (run JWT)
  v
Runtime, one gateway_composition, two pins (supply-chain-on-demand, crm-case-advice)
  |  POST /internal/crm/v2/openclaw/authorize               (routed by session_key prefix agent:crm:openclaw:)
  |  crm-case-advise-on-demand@1  (IR4, PREVIEW, scopes: the eight below)
  v
yeaher.crm (ebiz-adapter-crm) -> mcp-server -> crm-service: four reads only
  query_crm_case_v1, query_crm_case_context_v1, query_crm_case_evidence_v1,
  query_crm_account_capabilities_v1
```

The result travels back as an `operation.updated` event; the BFF validates it
against the bundled `case-advice-result` schema, fences the tenant and stores
only the whitelisted projection (`crm_reception/result.py`): the verdicts,
codes and summaries, `draft.body` verbatim, no `content_ref`, no thread or
body hashes, no buyer text or contact detail.

## What the profile is

| Item | Value |
| --- | --- |
| Reception config | `config/crm-reception.v1.json` - keys `version`, `instructions`, `tools` (exactly `crm_case_advice`), `argument_admissions.crm_case_advice = {mode: exact_bare_token, argument: case_id, token_syntax: ascii-identifier-v1}` |
| Offer | `crm-case-advice`, native tool `crm_case_advice`, aliases `crm_case_analysis`, `crm_case_advise_on_demand`, labels `crm, case, evidence, reply-draft` |
| Workflow | `crm-case-advise-on-demand@1`, Registry checksum pinned as `CRM_ADVISOR_WORKFLOW_DIGEST` and in `crm_release.advise_workflow.digest` (reviewed value `54f9a85048b024ca6b488abce7fc2bd3adb106a363a1c58b8ef4422e701a4095`) |
| Gateway scopes | exactly `workflow:start, runtime:admission, crm.case.read, crm.context.read, crm.evidence.read, crm.capability.read, crm.compute, crm.preview` |
| Plugin | the single `ebizhub.crm-agent` 2.0.0 (`ebiz-agent-crm`, `crm_agent.plugin:factory`, permissions `crm.compute, crm.preview`, config `policy_version`). There is no `ebizhub.crm-advisor` plugin. |
| Catalog sets | `crm@2` and `crm-advise@1`, both shipped in the agent wheel and published by the job below |
| Persistence | the same `supply_chain_bff` schema and Alembic chain (head `0009_conversation_profile_index`); rows are told apart by `profile_version = crm-reception.v1` and by the `agent:crm:openclaw:` session-key prefix |
| Session identity | tenant and actor come from crm-service's `session-principal` reply, never from the browser; the profile pins one tenant (`BFF_CRM_OPENCLAW_TENANT_ID`, D-2) and refuses every other tenant before any conversation or result can be addressed |

## Environment

BFF process (`ebiz-supply-chain-bff`):

| Variable | Meaning |
| --- | --- |
| `BFF_OPENCLAW_ENABLED`, `BFF_OPENCLAW_CONNECTOR_CREDENTIAL`, `TOOL_GATEWAY_JWT_KEY` | shared by both profiles: one connector credential for the Adapter- and Gateway-facing routes, one run-JWT key |
| `BFF_CRM_OPENCLAW_ENABLED` | mounts the CRM profile (`false` by default) |
| `BFF_CRM_OPENCLAW_TENANT_ID` | the one tenant this deployment serves |
| `BFF_CRM_OPENCLAW_AGENT_ID` | the CRM host instance's agent id (default `crm`); the Runtime launcher reads the same variable to route policy queries |
| `BFF_CRM_OPENCLAW_OFFER_ID` | default `crm-case-advice` |
| `BFF_CRM_OPENCLAW_INGRESS_URL`, `BFF_CRM_OPENCLAW_INGRESS_CREDENTIAL` | the CRM host instance's loopback ingress (must differ from the Supply Chain one) and its bearer |
| `BFF_CRM_SERVICE_URL` | crm-service origin for `GET /ai/read/v1/crm/session-principal` (HTTPS outside loopback) |

Runtime process (`ebiz-runtime-deployment`), on top of `SUPPLY_CHAIN_TOOL_GATEWAY_*` /
`TOOL_GATEWAY_*` / `BFF_OPENCLAW_*`:

| Variable | Meaning |
| --- | --- |
| `CRM_TOOL_GATEWAY_ENABLED` | adds the CRM pin to the shared composition |
| `CRM_ADVISOR_WORKFLOW_DIGEST` | the checksum printed by the publish job; must equal `crm_release.advise_workflow.digest` |
| `CRM_CREDENTIAL_REF` | opaque, tenant-bound CRM read-only broker reference (one per tenant) |
| `CRM_TOOL_GATEWAY_CID` | trusted caller id of the CRM offer (default `crm-advisor`) |
| `CRM_TOOL_GATEWAY_GENERATION_ID` | the CRM host instance's own generation id (default `<TOOL_GATEWAY_GENERATION_ID>-crm`, must differ) |
| `CRM_ADAPTER_RECORD_DIGEST`, `CRM_AGENT_RECORD_DIGEST`, `CRM_POLICY_VERSION` | release pins consumed by `config/deployment.crm.example.json` and `config/runtime-plugin-policy.crm.example.json` |

`config/deployment.crm.example.json` is the complete two-profile deployment
(four providers, `crm_release`); `config/deployment.example.json` stays the
Supply Chain-only baseline.

## Enablement order

Follows the "启用顺序" of the Runtime repository's
`docs/openclaw-integration-release-checklist.md`; nothing below is a record of
having been done.

1. Pin the CRM agent wheel (`ebiz-agent-crm` 2.0.0), the CRM adapter wheel
   (`ebiz-adapter-crm`), Runtime 0.1.6 and the Adapter package; record the
   RECORD digests (`CRM_AGENT_RECORD_DIGEST`, `CRM_ADAPTER_RECORD_DIGEST`).
2. Migrate the BFF database to `0009_conversation_profile_index` with new
   admission still off (`BFF_CRM_OPENCLAW_ENABLED=false`,
   `CRM_TOOL_GATEWAY_ENABLED=false`).
3. Publish the Catalog sets and the advise workflow:

   ```text
   AGENT_RUNTIME_DATABASE_URL=... ebiz-crm-advise-publish \
     --tenant-id <tenant> --actor-id <uuid> \
     --expect-digest 54f9a85048b024ca6b488abce7fc2bd3adb106a363a1c58b8ef4422e701a4095
   ```

   The job is idempotent, refuses any WRITE binding and exits non-zero unless
   the published checksum equals `--expect-digest`. Pin the printed value as
   `CRM_ADVISOR_WORKFLOW_DIGEST`.
4. Export one catalog generation **per OpenClaw host instance** with
   `ebiz-gateway-generation-export` (the Supply Chain instance keeps
   `TOOL_GATEWAY_GENERATION_ID`, the CRM instance gets
   `CRM_TOOL_GATEWAY_GENERATION_ID`, default `<TOOL_GATEWAY_GENERATION_ID>-crm`;
   both share `TOOL_GATEWAY_CATALOG_REVISION`), sign each with the Adapter's
   `sign-generation.mjs`, keep the previous generation of each instance for
   old-operation result validation, and materialize the CRM host instance's
   plugin manifest for exactly `crm_case_advice` and the fixed control tools.
   Each OpenClaw instance gets its own persistent SQLite volume, identity and
   ingress; see `docs/crm-host-instance.md`.
5. Start the CRM host instance against the BFF with a Fake CRM behind
   `yeaher.crm`; check discover/describe/invoke of `crm_case_advice`, the run
   binding, `/internal/crm/v2/openclaw/authorize`, and the four-segment result
   projection (ADVISED with draft, BLOCKED, TAKEOVER, failure).
6. Enable one tenant (`BFF_CRM_OPENCLAW_ENABLED=true`, `CRM_TOOL_GATEWAY_ENABLED=true`).

**Until the crm-service JWT change and the `session-principal` route
(crm-service branch `codex/crm-session-principal`) are deployed, every CRM call
segment answers 401**: the session exchange returns `401 session invalid`
(crm-service `CRM-401-UNAUTHENTICATED`) and no BFF JWT is minted, and the
Runtime's `yeaher.crm` reads are refused by crm-service. Record that in the
acceptance log rather than working around it.

## What the BFF expects from crm-service (P1-D)

`GET /ai/read/v1/crm/session-principal`, token in the `_token_` header (the
BFF forwards it there; crm-service also accepts `_refresh_token_` and the
`_token_` cookie):

- `200` bare `{"tenantId": "...", "actorId": "...", "roles": [...]}`
- `401` (`CRM-401-UNAUTHENTICATED`, no/invalid/expired token) and `403`
  (`CRM-403-FORBIDDEN`, machine key or no tenant) are passed through with the
  same status and the fixed detail `session invalid`; the `IngressResponse`
  envelope body is never forwarded
- any other status (for example `502` on a session-store failure) becomes
  `503 session service unavailable`

## What the host deployment must configure (P1-E)

- a second OpenClaw instance for CRM with its own ingress URL/credential
  (`BFF_CRM_OPENCLAW_INGRESS_URL`, `BFF_CRM_OPENCLAW_INGRESS_CREDENTIAL`), agent
  id `crm`, its own SQLite volume and trust material;
- one signed catalog generation per instance, each containing only that
  instance's offer and materializing only its tool names
  (`inventory_supply_chain_on_demand` vs `crm_case_advice`) under its own
  generation id (`docs/crm-host-instance.md`);
- the shared connector credential and run-JWT verification material on both
  instances (one BFF, one Runtime identity provider).

## Not in Phase 1

No WRITE offer: `crm-case-orchestrate@3` (Phase 2, digest recorded as a
commented placeholder `CRM_ORCHESTRATE_V3_WORKFLOW_DIGEST` in
`config/deployment.env.example`) is not pinned as a Gateway offer, the three
underlying CRM writes are never published, and the Gateway offers no approve,
resume or cancel to the model.
