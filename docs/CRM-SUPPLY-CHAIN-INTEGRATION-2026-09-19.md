# CRM / Supply Chain source integration — 2026-09-19

Status: **local source integration only; NOT a release approval**.

All six repositories use `codex/crm-supply-chain-integration-20260919`.
The existing Candidate D-R1 branches, installed wheels, acceptance directories,
running services and databases were not replaced or restarted. No push or
deployment was performed. Do not repoint the frozen acceptance environment at
these checkouts until the outstanding gates below are closed.

## Recovery checkpoints and merge inputs

Every original dirty file was inventoried, hashed and copied before committing.
Both staged and unstaged binary patches were captured. Checkpoints are recovery
snapshots, not validated releases. Each repository retains the branch
`codex/crm-wip-checkpoint-20260919`.

| Repository | WIP checkpoint | Supply Chain base | CRM merge input |
| --- | --- | --- | --- |
| emf-ai | b4e92f2b5a262e8d38e401ce2414028dd75d97ba | 29fa12ed66f7d0842df2e4e8d12faddbde0d059a | checkpoint |
| ebiz-agents | 223b778bb29265047f56050030707f887a0ee290 | 2ebfc8d4f5e530d7d00d62db297c3c29c52d48ec | 34ebf5af3118df0cfcbad95812a649384c9c2ea9 |
| ebiz-agents-deployment | bf5ae38ebee1bf626e025ec2ea6b4a9cd47e47d7 | 26197e34e914711d6eb62bfaf7ef434e375609de | 418197c94e418cca7d416a67335be8bc539e35be |
| ebizhub-agent-runtime | a28dea20428508d93bef736282280d3bf4cb5ea2 | 9740f323f4ff61d358204c81f5bfdd863a54b582 | bf0e24f043ab44f0baa72bf64e2a475e6158fd98 |
| base-ai | 9a2bb7d1e8bdea6ab156b4188c803c66e6249b14 | ceeee0b9ee5f3b57a52b9b315cf0f1e526afe783 | 004bda88d22747ec25022716b5b74d145cbf64e1 |
| crm-service | 7f374dfd10d611d15930d6ca75923d5223555255 | not applicable | c50ad4fc927f9ef76adb5306087b3fbfe84432cf |

Base AI's merge input includes the committed CRM adapter and the subsequent
provider StatusProbe-mode contracts; older local CRM adapter WIP was not layered
over that newer implementation.

## Reconciliation decisions

- Frontend: retain CRM's board, Awaiting reply default, row/detail switching,
  permission selector and layout corrections; retain Supply Chain's newer shared
  shell, registry, dictionaries and components. Adapt CRM `iconPath` to the new
  `iconPaths` contract. This merge does **not** change the CRM demo's legacy SSE
  transport into a verified OpenClaw chat connection.
- Agent: use the latest single `ebizhub.crm-agent` plugin and its on-demand advise
  workflow. The older local `crm_advisor` plugin, `advisor-*.yaml`, generated-advisor
  artifacts and their tests are superseded, not installed alongside a duplicate
  advisor. They remain recoverable from the WIP checkpoint.
- Preserve Candidate D's strict capability-version check, updated to the current
  manifests: five result builders are v3, advise builders and MCP operations v1.
  Add a regression comparing every pin to both catalogs. Supply explicit versions
  in the new CRM test invocation helpers; do not weaken the Runtime contract.
- Use sibling primary-workspace Runtime source paths for development. Regenerate
  the agents lock from real package metadata. Remove duplicate lock entries already
  present in the Supply Chain source lock; no private artifact is fabricated.
- Runtime: reintegrate the local CRM bootstrap WIP (MCP before domain adapters,
  governed ToolInvoker injected into CRM/ERP, not MODEL). Preserve the neutral
  boundary. Keep the existing Supply Chain v1 `execution_id` projection byte-for-byte;
  align the incoming v2 freeze test with that actual base, not an older v1 schema.
- Deployment: use the committed shared composition and host-isolated CRM reception
  implementation. Older local readonly overlay documentation/configuration is
  superseded by `docs/crm-profile.md` and `docs/crm-host-instance.md`.
- Do not import the local unsubmitted Supply Chain V7 WIP wholesale. Reject the
  CRM branch's incidental Registry v8 promotion: retain the source base's v7
  release selectors and v1 on-demand offer. See the release discrepancy below;
  retaining source selectors is **not** proof they match Candidate D-R1 artifacts.
- Existing SQL migration files and new reception migrations are source changes
  only; no migration was run against the frozen acceptance database.
- Java: merge session-principal changes with the MCP JWT/session authentication
  checkpoint; both authentication surfaces are retained and verified together.
- Base AI's unrelated local OpenAI-version/Redis-range changes remain in the WIP
  checkpoint. They are not silently treated as an approved package release.
- Final secret scanning found a JWT inherited from the frozen frontend's tracked
  `.env.development` (`DEV_TOKEN`, introduced in its history at `fa84348`). The
  integration copy is cleared; the frozen branch/history is untouched. The owner
  should check validity and revoke/rotate the credential if applicable. A separate
  example-file scan finding was an empty credential-reference assignment spanning
  the next variable; quoting the empty value and separating the public market
  setting removes that false positive without disabling secret scanning.

## Fresh validation

Local evidence root:
`C:/ebizhub/.local/worktree-snapshots/crm-supplychain-20260919`.
It contains per-repository snapshots, recovery commits, source reconciliation,
command manifests, logs, JUnit/JSON results and final Git identities.

| Gate | Result | Evidence |
| --- | --- | --- |
| CRM Java full offline Maven verify | 2,320 tests; 0 failures/errors; 1 skipped | `C:/ebizhub/.local/test-runs/20260919-004423-778-crm/manifest.json` |
| Frontend full Vitest before final icon adaptation | 703 passed | tool execution; final affected subset below |
| Frontend final CRM/registry/dictionaries | 78 passed | `frontend-final.json`, `frontend-final.manifest.json` |
| Frontend production build | exit 0 | `frontend-build.manifest.json` |
| CRM deterministic domain/plugin/golden/contract tests | 237 passed | `crm-core-final.manifest.json`, `crm-core-final.xml` |
| Runtime schemas, boundary guard, bootstrap and Gateway v1/v2 | 594 passed | `runtime-focused-final.manifest.json`, `runtime-focused-final.xml` |
| Base AI CRM adapter, real Agent contract, provider primitives | 454 passed | `base-wsl-final.xml`, `base-wsl-final.log` |
| BFF CRM/SC routes, composition, credentials and reception isolation | 227 passed, 3 skipped | `deployment-routing-final.manifest.json`, `.xml` |

Java used the unified test runner and JDK 21.0.12 with the existing offline Maven
cache. Frontend used bundled Node 24.19.0 and installed locked dependencies.
Base AI used its documented WSL `.venv/bin/python` (3.12.3) after locked offline
sync. Python source-integration tests used the existing Windows deployment
`.venv-verify/Scripts/python.exe` (3.12.14) with explicit primary-checkout source
paths; Runtime tests used the neutral Base AI fixture. These are not attestation
tests of a newly assembled production wheel set.

## Not yet release-ready

1. The Candidate D-R1 submission explicitly freezes **Agent Registry v6 / Workflow
   v6**. Deployment commit `26197e3` nevertheless contains v7 release selectors;
   the CRM branch attempted v8. Its expected on-demand publication is absent from
   the Candidate D Agent's `generated-v6` release. A separately reviewed integration
   release definition is required; never overwrite the existing accepted package
   or substitute a different Registry version under its hashes.
2. Full frontend typecheck still fails in ComTable, FieldRenderer, listing/monitor
   types and MonitorTable. Those five files are Git-object-identical to `29fa12e`;
   the CRM icon-field error introduced by the merge is fixed. The remaining errors
   are recorded in `frontend-typecheck.log`, not waived as a green gate.
3. Broad Python runs are not green. The Windows verification environment has old
   installed distribution metadata, a missing Runtime console entry point, missing
   catalog distributions and a symlink-permission failure. Several tests reference
   retired worktrees. See `base.log`, `deployment.log`, `agents-unit.log`. Do not
   use source-path test successes as proof of clean-wheel compatibility.
4. CRM black-box approval execution did not complete after invocation fixture
   adaptation; the owned test process was stopped. No successful full workflow,
   live OpenClaw harness, approval/write execution or reconciliation is claimed.
5. Supply Chain's frozen migration manifest includes the root lock and pyproject;
   their CRM integration changes invalidate those old hashes. The frozen business
   files and manifests have not been rewritten to hide this. Assemble and verify
   a **new** integration artifact set separately (`supply-chain.log`).
6. Package-level release pins (including Base AI/OpenAI metadata), clean-wheel
   attestation, full Python/static gates, database gates and live CRM testing remain
   required before promotion. Current commits must not be labeled Candidate D-R1.

No VPN connection, real email, order write, deployment, push or frozen-service
restart was authorized or performed. Existing historical worktrees were retained;
none was created or deleted for this integration.
