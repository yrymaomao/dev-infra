# CRM read-only advice in the shared OpenClaw Gateway

## Implemented boundary

One Runtime process receives **one** `SharedToolGatewayComposition`. Enabling
both businesses produces one catalog generation with two workflow pins:

| ToolOffer | Published Workflow | Native tool |
| --- | --- | --- |
| supply-chain-on-demand | inventory-supply-chain-on-demand@1 | inventory_supply_chain_on_demand |
| crm-case-advice | crm-case-advise-on-demand@1 | crm_case_advice |

The source implementation does not change Runtime wire v1 or permit WRITE
projection. `SupplyChainToolGatewayComposition` remains as a compatibility
wrapper for single-offer scripts. Every pin gets its own snapshot preparation
and full-reference-bound authority profile; credentials/scopes are not unioned.

CRM accepts only `{ "case_id": "..." }`. The CRM advisor package supplies a
separate PREVIEW graph with evidence checks, existing model/policy/draft checks
and a unified evidence-backed result. It performs no CRM decision/action/send
write. Case statuses NEW/NEEDS_ACTION are the current reply-eligible subset;
this is not an invented CRM AWAITING_REPLY enum. Other statuses skip the models.

## Deployment inputs (off by default)

Retain the existing Supply Chain configuration and add:

| Input | Required meaning |
| --- | --- |
| CRM_TOOL_GATEWAY_ENABLED | true only when the CRM publication/providers are ready |
| CRM_ADVISOR_WORKFLOW_DIGEST | exact Registry publication checksum, not merely the IR checksum |
| CRM_CREDENTIAL_REF | opaque, tenant-bound CRM read-only broker reference |
| CRM_TOOL_GATEWAY_CID | trusted deployment caller ID; defaults to crm-advisor |
| BFF_CRM_OPENCLAW_ENABLED | true in the existing BFF to authorize crm-case-advice for active runs |
| TOOL_GATEWAY_GENERATION_ID / TOOL_GATEWAY_CATALOG_REVISION | new shared generation/revision |

The same BFF, JWT trust and tenant bindings are reused. The existing BFF policy
endpoint name remains `/internal/supply-chain/v2/openclaw/authorize` for wire
compatibility; only its explicit offer allowlist grows. This remains the current
controlled single-tenant OpenClaw connector policy, not a new multi-user CRM ACL.

`config/crm-openclaw-readonly.overlay.example.json` documents **additions** to the
existing deployment and Runtime plugin policy, not a standalone launch config.
Install/attest `ebiz-agent-crm` and `ebiz-adapter-crm` wheels first. Keep both CRM
plugins on the same `policy_version` and installed RECORD digest. Their import
namespaces are separate because Runtime independently attests each entry point.

The advisor needs exactly these four external operations and MCP tools:

| Operation | Tool |
| --- | --- |
| crm.get_case | query_crm_case_v1 |
| crm.get_case_context | query_crm_case_context_v1 |
| crm.get_case_evidence | query_crm_case_evidence_v1 |
| crm.get_account_capabilities | query_crm_account_capabilities_v1 |

The MCP adapter `allowed_tools` is the sorted union of the existing nine Supply
Chain reads plus these four reads. The CRM provider enables only those four;
the credential broker allows `yeaher.crm` in addition to its current IDs and
must resolve the CRM reference to the authorized tenant's credential. The
gateway auth profile grants `workflow:start`, `runtime:admission`, `crm.compute`,
`crm.preview` and the four corresponding CRM read scopes. Never substitute a
Supply Chain credential merely because both use the same MCP server.

`crm.get_action_execution` is not needed for advice. Evidence loading, decision
recording, action creation and message sending are deliberately excluded, as
are their MCP tools and write scopes. This is why this entry uses four tools,
not the full nine-tool Java surface.

## Activation and verification

1. Compile/check `crm-agent/advisor-contract.yaml`, build the CRM and adapter
   wheels, and record installed RECORD attestation. Preserve existing release
   pins and source WIP; do not silently upgrade other packages.
2. Publish the advisor subset Catalog/imports and compiled Workflow through the
   existing Runtime publication services. Reused capability publications must
   remain byte/contract-identical. Use the returned publication checksum.
3. Add the reviewed provider/plugin pins and environment inputs to the controlled
   deployment. Keep the full original WRITE workflow out of the Gateway catalog.
4. Export/sign one generation containing **both** offers. Retain the previous
   generation for old-operation result validation. Update the protected OpenClaw
   deployment trust and materialize the installed plugin's public tool manifest
   with its `scripts/prepare-plugin-manifest.mjs` entry point.
5. Coordinate reload of Runtime/BFF/OpenClaw; do not delete the operation ledger
   or start a second Runtime. Check both tools are discoverable, execute one
   authorized test Case read, verify the evidence/draft result, and regress the
   existing Supply Chain tool. Real CRM reads require a working Java/MCP auth
   path and upstream OFS/RPS/SES evidence configuration.

Local compiler/engine/mock-CRM and policy tests are not live activation evidence.
Do not mark this rollout active until the signed catalog and a real authorized
Case invocation have been verified. No real email or business WRITE is needed.
