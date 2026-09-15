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
| `BFF_CRM_WORKBENCH_OPERATIONS_ENABLED` | Phase 2: mounts the workbench operation routes (default `false`; requires `BFF_CRM_OPENCLAW_ENABLED`); they reuse `BFF_RUNTIME_URL` and `APP_JWT_SECRET`, no new credential |

Runtime process (`ebiz-runtime-deployment`), on top of `SUPPLY_CHAIN_TOOL_GATEWAY_*` /
`TOOL_GATEWAY_*` / `BFF_OPENCLAW_*`:

| Variable | Meaning |
| --- | --- |
| `CRM_TOOL_GATEWAY_ENABLED` | adds the CRM pin to the shared composition |
| `CRM_ADVISOR_WORKFLOW_DIGEST` | the checksum printed by the publish job; must equal `crm_release.advise_workflow.digest` |
| `CRM_CREDENTIAL_REF` | opaque, tenant-bound CRM read-only broker reference (one per tenant) |
| `CRM_TOOL_GATEWAY_CID` | trusted caller id of the CRM offer (default `crm-advisor`) |
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
4. Export and sign one catalog generation containing both offers
   (`TOOL_GATEWAY_GENERATION_ID` / `TOOL_GATEWAY_CATALOG_REVISION`), keep the
   previous generation for old-operation result validation, and materialize
   the CRM host instance's plugin manifest for exactly `crm_case_advice` and
   the fixed control tools. Each OpenClaw instance gets its own persistent
   SQLite volume, identity and ingress.
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
- the same catalog generation exported for both instances, each materializing
  only its own tool names (`inventory_supply_chain_on_demand` vs
  `crm_case_advice`);
- the shared connector credential and run-JWT verification material on both
  instances (one BFF, one Runtime identity provider).

## Workbench operation routes (Phase 2, P2-G)

The emf-crm operation page (`/crm/operations/:operationId`, branch
`codex/emf-crm-operation-page`) reads and decides one governed WRITE operation
through two routes on the CRM profile's API prefix. They are mounted only when
`BFF_CRM_WORKBENCH_OPERATIONS_ENABLED=true` (default `false`) and implement
v1.5 sections 6.3, 8.3, 10.2, 12.2, 13.1 and the integrated design section 12.3.

| Route | Reply |
| --- | --- |
| `GET /api/crm/v2/openclaw/operations/{operationId}` | `200 {status, case, evidence[], approval \| null, effects[], audit_href}` |
| `POST /api/crm/v2/openclaw/operations/{operationId}/approval/decision` with `{approval_id, row_version, decision: APPROVED \| REJECTED, reason}` | `200 {status}`; `409` stale `row_version` / already decided / not the operation's pending approval; `403` not an authorized approver; `410` approval deadline passed |

Bodies are bare (no `{requestId, data}` envelope). Errors use the standard
BFF envelope `{error_code, phase, category, retryable, safe_message,
request_id}`. Operation ids must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`.

### Reply shape

- `status` is the Runtime's wire v2 `OperationStatusV2` document forwarded
  **unchanged** after it passed the BFF mirror
  (`crm_reception/operation_contracts.py`; the pinned `ebiz-runtime-contracts`
  0.1.6 wheel predates wire v2, so the mirror carries the same enums, bounds,
  settlement pairs and projection rules, verified against a byte copy of the
  P2-A `operation_status` fixtures under `tests/fixtures/tool-gateway-v2/`).
  The BFF never rewrites `state`, `revision`, `interaction.path` or timings;
  the page validates `interaction.path` as a server-fixed relative path.
- `case` `{case_id, case_version}` from the approval's frozen preview, else
  from the execution outputs, else `null`.
- `evidence[]` items are exactly `{source_type, ref, summary}` (`ref` is the
  opaque evidence id). Whitelist, never a blacklist: content references,
  buyer contact details and conversation text cannot reach the reply because
  no other key is copied.
- `approval` is present while the execution waits on an approval:
  `{approval_id, row_version, node_ref, capability_ref, input_hash,
  preview_hash, proposal {action_type, impact_summary, risk_summary,
  target_thread_ref, body_hash}, draft {body, language} | null, deadline_at,
  decidable}`. `draft.body` is the only verbatim text (D-4). `deadline_at` is
  the earliest of the owner's `approval_usable_until` (v1.5 section 10.2:
  min of workflow deadline, approval TTL, evidence age) and the approval's
  `expires_at`. `decidable` is true only when the approval is `PENDING`, the
  operation is `waiting_approval`, `deadline_at` is in the future and the
  session's roles intersect the approval's `approver_roles`. An approval
  whose preview, hashes, node or capability are missing is not shown at all.
- `effects[]` `{node_ref, journal_state, receipt_ref, observed_at}` from the
  owner's settlement view (`business_effect_journal` states `PENDING`,
  `APPLIED_UNVERIFIED`, `UNKNOWN`, `CONFIRMED`, `VERIFY_FAILED`, `FAILED`,
  `COMPENSATED`); `partial` is never hidden and nothing claims a rollback.
- `audit_href` is `/crm/audit?execution=<execution_id>` once the operation is
  mapped to an execution, else `null`.

### Request order and hiding (A02)

1. operation id syntax (no call);
2. the workbench `_token_` (header or cookie, as the session exchange reads
   it) through crm-service `GET /ai/read/v1/crm/session-principal`; a
   missing, invalid (401) or machine (403) session is hidden;
3. the tenant fence: the session's tenant must be `BFF_CRM_OPENCLAW_TENANT_ID`
   - **no Runtime call happens before this passes**;
4. the Runtime owner read with a tenant-bound token; the operation must then
   belong to this profile (`offer_id` in the profile's offers, session key
   under `agent:crm:openclaw:`) or it is hidden;
5. approval, execution and settlement facts are read only afterwards.

Unknown, foreign-tenant, other-profile and unauthenticated requests answer one
byte-identical `404 CRM_OPERATION_NOT_FOUND`. A Runtime or session-service
outage is `503` with `retryable: true`, never a `404`.

### Sourcing choice and credential path

The BFF process is not the Runtime process and holds no Gateway tool
identity; the Gateway's `status` authorization is bound to the operation's
owner and session, which v1.5 section 6.3 says the approver route must not
reuse. The routes therefore read the **Runtime owner's authenticated APIs**
keyed by the operation -> execution mapping, through the seam
`crm_reception/operation_runtime.py` (`OperationRuntime` Protocol; tests
inject a fake, production uses `HttpOperationRuntime`).

Credential path: the same one the dispatcher and the Level 2 worker use - an
HS256 JWT minted with `APP_JWT_SECRET` against `BFF_RUNTIME_URL`
(`aud=agent-runtime`), 60 s TTL, one per call, never persisted and never sent
to the browser. It is bound to the deployment tenant and to the *human* the
session resolved: `sub = uuid5("ebizhub:openclaw:principal:<tenant>:<actorId>")`
(the same actor id the Gateway records when that person starts a
conversation), `actor_type = HUMAN`, `roles` = the session roles,
`scopes = [workflow:read]` for reads and `[workflow:read, approval:decide]`
for the decision. The Runtime's own `ApprovalService.decide` records that
actor as `decided_by` and checks the roles against `approver_roles`, so the
page cannot name a tenant, actor or role in any request field. No
`credential_ref` is attached: the workbench never starts executions.

Runtime routes called:

| Seam method | Route | Status |
| --- | --- | --- |
| `read_operation` | `GET /v1/workbench/operations/{operation_id}` -> `{status: OperationStatusV2, offer_id, session_key}` | **P2-E open item** (see below) |
| `read_execution` | `GET /v1/executions/{execution_id}` -> `waiting.approval_id`, `waiting.approval_row_version`, `outputs` | exists (`agent_runtime.api.executions`) |
| `read_approval` | `GET /v1/approvals/{approval_id}` -> approval record + `approver_roles`, `preview_hash`, `capability_ref`, `approval_usable_until`, frozen `preview {case_id, case_version, proposal, draft, evidence_refs}` | **P2-E open item** |
| `read_settlement` | `GET /v1/executions/{execution_id}/write-settlement` -> `WriteSettlementView` safe fields + `effects[{node_key, status, result_ref, observed_at}]` | **P2-E open item** (`AgentRuntimeService.get_write_settlement(auth, execution_id)` exists, no HTTP route) |
| `decide` | `POST /v1/approvals/{approval_id}/decision` `{outcome, reason, expected_row_version}` | exists |

Runtime error mapping on the decision: `RUNTIME_STATE_CONFLICT` (409) ->
`409 CRM_APPROVAL_CONFLICT`; `APPROVAL_EXPIRED` (the Runtime answers 409,
category `permanent`) -> `410 CRM_APPROVAL_EXPIRED`; `APPROVAL_ROLE_DENIED`
or any 403 -> `403 CRM_APPROVAL_FORBIDDEN`; 404 -> the hidden 404; anything
else -> 503. A `REJECTED` decision is forwarded as `REJECTED`; the BFF never
invents an approval, never approves on its own and only forwards a decision
whose `approval_id` is the execution's current waiting item.

Open items for P2-E (the three routes marked above): a workbench read of the
operation projection that accepts the Runtime JWT (`workflow:read`), is
tenant-fenced and returns the owner facts `offer_id` and `session_key` next
to the unchanged `OperationStatusV2`; an authenticated approval read; an HTTP
face for `get_write_settlement`. The status route must emit
`interaction.path = /crm/operations/{operation_id}` for the CRM profile (the
P2-A fixtures still show `/runtime/operations/...`); the BFF forwards the
path as received. Until those routes exist the flag stays `false`.

## Not in Phase 1

No WRITE offer: `crm-case-orchestrate@3` (Phase 2, digest recorded as a
commented placeholder `CRM_ORCHESTRATE_V3_WORKFLOW_DIGEST` in
`config/deployment.env.example`) is not pinned as a Gateway offer, the three
underlying CRM writes are never published, and the Gateway offers no approve,
resume or cancel to the model.
