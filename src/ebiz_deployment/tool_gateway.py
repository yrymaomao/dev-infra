"""Deployment-owned composition for one exact Supply Chain on-demand ToolOffer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from agent_runtime.application.auth import AuthContext
from agent_runtime.application.execution_admission import ExecutionAdmissionAuthority
from agent_runtime.application.tool_gateway_authorization import (
    ToolGatewayAuthorizer,
    ToolGatewayPolicyRejected,
    ToolGatewayPolicyUnavailable,
)
from agent_runtime.application.tool_gateway_catalog import ToolGatewayCatalogService
from agent_runtime.application.tool_gateway_composition import GatewayServices
from agent_runtime.application.tool_gateway_delivery import ToolGatewayDeliveryService
from agent_runtime.application.tool_gateway_identity import GatewayIdentityProvider
from agent_runtime.application.tool_gateway_operations import (
    ConfiguredGatewayPreparations,
    GatewayBackendContext,
    GatewayOperationBackend,
    GatewayOperationConflict,
    GatewayOperationService,
    GatewaySnapshotPreparation,
)
from agent_runtime.application.tool_gateway_projection import (
    GatewayCatalogGeneration,
    GatewayPublicationPin,
    RegistryToolOfferProjector,
)
from agent_runtime.application.tool_gateway_reconciliation import GatewayReconciliationTransport
from agent_runtime.application.tool_gateway_snapshots import ToolGatewaySnapshotService
from agent_runtime.db.gateway_operations import GatewayOperationRepository
from agent_runtime.outbox.contracts import OPERATION_RECONCILE_REQUESTED
from agent_runtime.outbox.worker import PostgresOutboxWorker
from agent_runtime.registry.capabilities import PostgresCapabilityRegistry
from agent_runtime.registry.workflows import PostgresWorkflowRegistry
from ebiz_runtime_contracts import ActorRef
from ebiz_runtime_contracts.tool_gateway_catalog import ToolOfferReference
from ebiz_runtime_contracts.tool_gateway_policy import (
    PolicyTarget,
    ToolGatewayPolicyReply,
    ToolGatewayPolicyRequest,
)
from sqlalchemy.ext.asyncio import async_sessionmaker


class BffToolGatewayPolicyPort:
    """Resolve current OpenClaw run authority from the BFF on every Gateway action."""

    def __init__(self, *, client: httpx.AsyncClient, connector_credential: str) -> None:
        if len(connector_credential) < 32:
            raise ValueError("Gateway connector credential is invalid")
        self._client = client
        self._credential = connector_credential

    async def authorize(self, request: ToolGatewayPolicyRequest) -> ToolGatewayPolicyReply:
        response = await self._client.post(
            "/internal/supply-chain/v2/openclaw/authorize",
            headers={"Authorization": f"Bearer {self._credential}"},
            json={
                "tenant_id": request.identity.tenant_id,
                "principal_id": request.identity.principal_id,
                "session_key": request.identity.session_key,
                "action": request.action,
                "candidate_offer_ids": [
                    target.target_id for target in request.candidates if target.kind == "offer"
                ],
            },
        )
        if response.status_code == 403:
            raise ToolGatewayPolicyRejected()
        if response.status_code != 200:
            raise ToolGatewayPolicyUnavailable()
        value = response.json()
        if not isinstance(value, dict) or set(value) != {
            "binding_active",
            "policy_revision",
            "allowed_offer_ids",
        }:
            raise ToolGatewayPolicyRejected()
        allowed = value["allowed_offer_ids"]
        if (
            not isinstance(value["binding_active"], bool)
            or not isinstance(value["policy_revision"], str)
            or not isinstance(allowed, list)
            or not all(isinstance(item, str) for item in allowed)
        ):
            raise ToolGatewayPolicyRejected()
        return ToolGatewayPolicyReply(
            tenant_id=request.identity.tenant_id,
            principal_id=request.identity.principal_id,
            session_key=request.identity.session_key,
            binding_active=value["binding_active"],
            action=request.action,
            policy_revision=value["policy_revision"],
            allowed_targets=tuple(PolicyTarget(kind="offer", target_id=item) for item in allowed),
        )


class DynamicGatewayContext:
    """Create Runtime auth only from a currently authorized signed Gateway identity."""

    def __init__(self, *, connector_id: str, cid: str) -> None:
        self.connector_id = connector_id
        self._cid = cid

    def resolve(
        self, decision: ToolGatewayPolicyReply, reference: ToolOfferReference
    ) -> GatewayBackendContext:
        target = PolicyTarget(kind="offer", target_id=reference.offer_id)
        if (
            not decision.binding_active
            or decision.action != "invoke"
            or target not in (decision.allowed_targets)
        ):
            raise GatewayOperationConflict()
        actor_id = uuid5(
            NAMESPACE_URL,
            f"ebizhub:openclaw:principal:{decision.tenant_id}:{decision.principal_id}",
        )
        return GatewayBackendContext(
            connector_id=self.connector_id,
            owner_id=decision.principal_id,
            session_key=decision.session_key,
            auth=AuthContext(
                tenant_id=decision.tenant_id,
                cid=self._cid,
                actor=ActorRef(actor_id=actor_id, actor_type="service"),
                scopes=frozenset({"workflow:start", "runtime:admission", "supply_chain.preview"}),
            ),
            revision=decision.policy_revision,
        )


@dataclass(slots=True)
class _GatewayLifecycle:
    client: httpx.AsyncClient
    worker: PostgresOutboxWorker
    task: asyncio.Task[None]

    @classmethod
    def start(cls, *, client: httpx.AsyncClient, worker: PostgresOutboxWorker) -> _GatewayLifecycle:
        owner = cls(client=client, worker=worker, task=None)  # type: ignore[arg-type]
        owner.task = asyncio.create_task(owner._run(), name="supply-chain-tool-gateway-relay")
        return owner

    async def _run(self) -> None:
        while True:
            result = await self.worker.run_once(batch_size=50)
            if result.published == 0 and result.retried == 0:
                await asyncio.sleep(0.25)

    async def aclose(self) -> None:
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        await self.client.aclose()


class SupplyChainToolGatewayComposition:
    def __init__(
        self,
        *,
        tenant_id: str,
        workflow_digest: str,
        bff_url: str,
        connector_credential: str,
        jwt_key: str,
        jwt_issuer: str,
        jwt_audience: str,
        cid: str,
        generation_id: str = "supply-chain-v2-dev-1",
        catalog_revision: str = "supply-chain-v2-dev-1",
        offer_id: str = "supply-chain-on-demand",
    ) -> None:
        parsed = urlsplit(bff_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Tool Gateway BFF URL must be loopback HTTP")
        if len(jwt_key) < 32 or len(connector_credential) < 32:
            raise ValueError("Tool Gateway secret material is invalid")
        if len(workflow_digest) != 64 or any(c not in "0123456789abcdef" for c in workflow_digest):
            raise ValueError("Tool Gateway Workflow digest is invalid")
        self._tenant_id = tenant_id
        self._workflow_digest = workflow_digest
        self._bff_url = bff_url.rstrip("/")
        self._connector_credential = connector_credential
        self._jwt_key = jwt_key
        self._jwt_issuer = jwt_issuer
        self._jwt_audience = jwt_audience
        self._cid = cid
        self._generation_id = generation_id
        self._catalog_revision = catalog_revision
        self._offer_id = offer_id

    async def compose(
        self, container: Any, authority: ExecutionAdmissionAuthority
    ) -> GatewayServices:
        workflows = PostgresWorkflowRegistry(container.unit_of_work_factory)
        capabilities = PostgresCapabilityRegistry(container.unit_of_work_factory)
        publication = await workflows.get_published(
            self._tenant_id, "inventory-supply-chain-on-demand", 1
        )
        if publication.checksum != self._workflow_digest:
            raise ValueError("published on-demand Workflow differs from the deployment pin")
        pin = GatewayPublicationPin(
            offer_id=self._offer_id,
            kind="workflow",
            code=publication.code,
            version=publication.version,
            publication_digest=publication.checksum,
            name="inventory_supply_chain_on_demand",
            aliases=("supply_chain_analysis",),
            labels=("inventory", "forecast", "replenishment"),
        )
        generation = GatewayCatalogGeneration(
            tenant_id=self._tenant_id,
            generation_id=self._generation_id,
            catalog_revision=self._catalog_revision,
            pins=(pin,),
        )
        client = httpx.AsyncClient(
            base_url=self._bff_url,
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
        )
        try:
            authorizer = ToolGatewayAuthorizer(
                identity_provider=GatewayIdentityProvider(
                    key=self._jwt_key,
                    algorithm="HS256",
                    issuer=self._jwt_issuer,
                    audience=self._jwt_audience,
                ),
                policy_port=BffToolGatewayPolicyPort(
                    client=client,
                    connector_credential=self._connector_credential,
                ),
            )
            projector = RegistryToolOfferProjector(capabilities, workflows)
            catalog = await ToolGatewayCatalogService.create(
                generation=generation,
                projector=projector,
                authorizer=authorizer,
            )
            projection = await projector.project_exact(generation, pin)
            if container.governed_artifact_service is None:
                raise ValueError("governed artifact service is unavailable")
            snapshots = ToolGatewaySnapshotService(
                projector=projector,
                workflows=workflows,
                reader=None,  # type: ignore[arg-type]
                registration=container.governed_artifact_service,
                resolver=None,  # type: ignore[arg-type]
            )
            preparation = ConfiguredGatewayPreparations(
                preparations=(
                    (
                        projection.offer.reference,
                        GatewaySnapshotPreparation(
                            snapshot_service=snapshots,
                            selector=None,  # type: ignore[arg-type]
                            generation=generation,
                            pin=pin,
                            projection=projection,
                            carrier_receipt=None,
                            bindings=(),
                            pinned_workflow=publication,
                            source_system="ebizhub-openclaw-adapter",
                        ),
                    ),
                )
            )
            context = DynamicGatewayContext(connector_id="openclaw", cid=self._cid)
            repository = GatewayOperationRepository(container.unit_of_work_factory)
            operations = GatewayOperationService(
                repository=repository,
                payload_store=container.payload_store,
                context_resolver=context,  # type: ignore[arg-type]
                authorizer=authorizer,
                preparation=preparation,
            )
            backend = GatewayOperationBackend(
                operations=operations,
                runtime=container.service,
                authority=authority,
                snapshots=snapshots,
            )
            relay = PostgresOutboxWorker(
                factory=async_sessionmaker(container.engine, expire_on_commit=False),
                transport=GatewayReconciliationTransport(
                    repository=repository,
                    operations=operations,
                    backend=backend,
                    payload_store=container.payload_store,
                    poll_interval=timedelta(seconds=1),
                ),
                tenant_id=self._tenant_id,
                lease_owner="supply-chain-tool-gateway",
                event_types=frozenset({OPERATION_RECONCILE_REQUESTED}),
            )
            lifecycle = _GatewayLifecycle.start(client=client, worker=relay)
            return GatewayServices(
                catalog=catalog,
                delivery=ToolGatewayDeliveryService(
                    operations=operations,
                    authorizer=authorizer,
                    sync_wait_seconds=2,
                    poll_interval_seconds=0.1,
                ),
                lifecycle=lifecycle,
            )
        except BaseException:
            await client.aclose()
            raise


__all__ = [
    "BffToolGatewayPolicyPort",
    "DynamicGatewayContext",
    "SupplyChainToolGatewayComposition",
]
