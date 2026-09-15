"""One deployment-owned Gateway composition with independently scoped workflow offers.

One Runtime process receives one composition with one catalog generation and
one pin per business agent offer. Every offer carries its own authority profile
(scopes and credential reference - never unioned) and names the BFF reception
profile that answers the Gateway's current-policy question for it: the policy
port routes each query by the agent prefix of the signed identity's
``session_key`` to that profile's ``/internal/<agent>/v2/openclaw/authorize``.
"""

from __future__ import annotations

import asyncio
import re
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

_SESSION_KEY_PREFIX = re.compile(r"^agent:[a-z0-9][a-z0-9._-]{0,63}:openclaw:$")
_AUTHORIZE_PATH = re.compile(r"^/internal/[a-z0-9][a-z0-9-]*/v2/openclaw/authorize$")


@dataclass(frozen=True, slots=True)
class GatewayPolicyRoute:
    """Which BFF reception profile answers for the run bindings of one agent instance."""

    session_key_prefix: str
    authorize_path: str

    def __post_init__(self) -> None:
        if _SESSION_KEY_PREFIX.fullmatch(self.session_key_prefix) is None:
            raise ValueError("Tool Gateway policy route needs an agent session-key prefix")
        if _AUTHORIZE_PATH.fullmatch(self.authorize_path) is None:
            raise ValueError("Tool Gateway policy route needs a reception authorize path")


SUPPLY_CHAIN_ROUTE = GatewayPolicyRoute(
    session_key_prefix="agent:main:openclaw:",
    authorize_path="/internal/supply-chain/v2/openclaw/authorize",
)


class BffToolGatewayPolicyPort:
    """Resolve current OpenClaw run authority from the BFF on every Gateway action."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        connector_credential: str,
        routes: tuple[GatewayPolicyRoute, ...] = (SUPPLY_CHAIN_ROUTE,),
    ) -> None:
        if len(connector_credential) < 32:
            raise ValueError("Gateway connector credential is invalid")
        if not routes:
            raise ValueError("Tool Gateway policy port requires at least one route")
        prefixes = [route.session_key_prefix for route in routes]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("Tool Gateway policy routes must have unique session-key prefixes")
        self._client = client
        self._credential = connector_credential
        self._routes = routes

    def route_for(self, session_key: str) -> GatewayPolicyRoute:
        for route in self._routes:
            if session_key.startswith(route.session_key_prefix):
                return route
        if len(self._routes) == 1:
            # A single-profile deployment keeps answering for every key; the one
            # BFF profile still fences the prefix itself.
            return self._routes[0]
        raise ToolGatewayPolicyRejected()

    async def authorize(self, request: ToolGatewayPolicyRequest) -> ToolGatewayPolicyReply:
        route = self.route_for(request.identity.session_key)
        response = await self._client.post(
            route.authorize_path,
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
        if request.action in {"status", "result", "cancel"}:
            if any(target.kind != "operation" for target in request.candidates):
                raise ToolGatewayPolicyRejected()
            allowed_targets = request.candidates if value["binding_active"] else ()
        else:
            allowed_targets = tuple(PolicyTarget(kind="offer", target_id=item) for item in allowed)
        return ToolGatewayPolicyReply(
            tenant_id=request.identity.tenant_id,
            principal_id=request.identity.principal_id,
            session_key=request.identity.session_key,
            binding_active=value["binding_active"],
            action=request.action,
            policy_revision=value["policy_revision"],
            allowed_targets=allowed_targets,
        )


@dataclass(frozen=True, slots=True)
class GatewayAuthorityProfile:
    cid: str
    credential_ref: str
    scopes: frozenset[str]
    authorize_path: str = SUPPLY_CHAIN_ROUTE.authorize_path
    session_key_prefix: str = SUPPLY_CHAIN_ROUTE.session_key_prefix

    def __post_init__(self) -> None:
        if not self.cid or not self.credential_ref or len(self.credential_ref) > 256:
            raise ValueError("Tool Gateway authority profile is invalid")
        if not {"workflow:start", "runtime:admission"} <= self.scopes:
            raise ValueError("Tool Gateway admission scopes are required")
        GatewayPolicyRoute(
            session_key_prefix=self.session_key_prefix, authorize_path=self.authorize_path
        )

    @property
    def route(self) -> GatewayPolicyRoute:
        return GatewayPolicyRoute(
            session_key_prefix=self.session_key_prefix, authorize_path=self.authorize_path
        )


@dataclass(frozen=True, slots=True)
class GatewayWorkflowOffer:
    pin: GatewayPublicationPin
    authority: GatewayAuthorityProfile

    def __post_init__(self) -> None:
        if self.pin.kind != "workflow":
            raise ValueError("this composition requires published workflow offers")


def policy_routes(offers: tuple[GatewayWorkflowOffer, ...]) -> tuple[GatewayPolicyRoute, ...]:
    """The distinct BFF routes of a set of offers; one prefix may name only one path."""

    by_prefix: dict[str, GatewayPolicyRoute] = {}
    for offer in offers:
        route = offer.authority.route
        existing = by_prefix.setdefault(route.session_key_prefix, route)
        if existing != route:
            raise ValueError("offers sharing an agent instance must share its authorize path")
    return tuple(by_prefix[prefix] for prefix in sorted(by_prefix))


class DynamicGatewayContext:
    """Create Runtime auth only from a currently authorized signed Gateway identity."""

    def __init__(
        self,
        *,
        connector_id: str,
        tenant_id: str,
        profiles: tuple[tuple[ToolOfferReference, GatewayAuthorityProfile], ...],
    ) -> None:
        if not profiles or len({ref for ref, _ in profiles}) != len(profiles):
            raise ValueError("Tool Gateway requires unique exact authority profiles")
        self.connector_id = connector_id
        self._tenant_id = tenant_id
        self._profiles = dict(profiles)

    def resolve(
        self, decision: ToolGatewayPolicyReply, reference: ToolOfferReference
    ) -> GatewayBackendContext:
        target = PolicyTarget(kind="offer", target_id=reference.offer_id)
        if (
            not decision.binding_active
            or decision.tenant_id != self._tenant_id
            or decision.action != "invoke"
            or target not in (decision.allowed_targets)
            or reference not in self._profiles
        ):
            raise GatewayOperationConflict()
        profile = self._profiles[reference]
        if not decision.session_key.startswith(profile.session_key_prefix):
            # The BFF profile that authorized this run is not the one this offer
            # belongs to: never lend one agent's scopes to another agent's session.
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
                cid=profile.cid,
                actor=ActorRef(actor_id=actor_id, actor_type="service"),
                scopes=profile.scopes,
                credential_ref=profile.credential_ref,
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
        owner.task = asyncio.create_task(owner._run(), name="deployment-tool-gateway-relay")
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


class SharedToolGatewayComposition:
    def __init__(
        self,
        *,
        tenant_id: str,
        offers: tuple[GatewayWorkflowOffer, ...],
        bff_url: str,
        connector_credential: str,
        jwt_key: str,
        jwt_issuer: str,
        jwt_audience: str,
        generation_id: str = "supply-chain-v2-dev-1",
        catalog_revision: str = "supply-chain-v2-dev-1",
    ) -> None:
        parsed = urlsplit(bff_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Tool Gateway BFF URL must be loopback HTTP")
        if len(jwt_key) < 32 or len(connector_credential) < 32:
            raise ValueError("Tool Gateway secret material is invalid")
        if not offers:
            raise ValueError("Tool Gateway requires at least one workflow offer")
        if len({offer.pin.offer_id for offer in offers}) != len(offers):
            raise ValueError("Tool Gateway offer ids must be unique")
        self.generation = GatewayCatalogGeneration(
            tenant_id=tenant_id,
            generation_id=generation_id,
            catalog_revision=catalog_revision,
            pins=tuple(offer.pin for offer in offers),
        )
        self._tenant_id = tenant_id
        self.offers = offers
        self.routes = policy_routes(offers)
        self._bff_url = bff_url.rstrip("/")
        self._connector_credential = connector_credential
        self._jwt_key = jwt_key
        self._jwt_issuer = jwt_issuer
        self._jwt_audience = jwt_audience

    async def compose(
        self, container: Any, authority: ExecutionAdmissionAuthority
    ) -> GatewayServices:
        workflows = PostgresWorkflowRegistry(container.unit_of_work_factory)
        capabilities = PostgresCapabilityRegistry(container.unit_of_work_factory)
        generation = self.generation
        publications = []
        for offer in self.offers:
            pin = offer.pin
            publication = await workflows.get_published(self._tenant_id, pin.code, pin.version)
            if publication.checksum != pin.publication_digest:
                raise ValueError("published Workflow differs from the deployment pin")
            publications.append(publication)
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
                    routes=self.routes,
                ),
            )
            projector = RegistryToolOfferProjector(capabilities, workflows)
            catalog = await ToolGatewayCatalogService.create(
                generation=generation,
                projector=projector,
                authorizer=authorizer,
            )
            if container.governed_artifact_service is None:
                raise ValueError("governed artifact service is unavailable")
            snapshots = ToolGatewaySnapshotService(
                projector=projector,
                workflows=workflows,
                reader=None,  # type: ignore[arg-type]
                registration=container.governed_artifact_service,
                resolver=None,  # type: ignore[arg-type]
            )
            preparations = []
            profiles = []
            for offer, publication in zip(self.offers, publications, strict=True):
                pin = offer.pin
                projection = await projector.project_exact(generation, pin)
                profiles.append((projection.offer.reference, offer.authority))
                preparations.append(
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
                    )
                )
            preparation = ConfiguredGatewayPreparations(preparations=tuple(preparations))
            context = DynamicGatewayContext(
                connector_id="openclaw",
                tenant_id=self._tenant_id,
                profiles=tuple(profiles),
            )
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
                lease_owner="deployment-tool-gateway",
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


class SupplyChainToolGatewayComposition(SharedToolGatewayComposition):
    """Compatibility entry point for existing single-offer deployment scripts."""

    def __init__(
        self,
        *,
        workflow_digest: str,
        cid: str,
        credential_ref: str,
        offer_id: str = "supply-chain-on-demand",
        **settings: Any,
    ) -> None:
        super().__init__(
            offers=(
                GatewayWorkflowOffer(
                    pin=GatewayPublicationPin(
                        offer_id=offer_id,
                        kind="workflow",
                        code="inventory-supply-chain-on-demand",
                        version=2,
                        publication_digest=workflow_digest,
                        name="inventory_supply_chain_on_demand",
                        aliases=("supply_chain_analysis",),
                        labels=("inventory", "forecast", "replenishment"),
                    ),
                    authority=GatewayAuthorityProfile(
                        cid=cid,
                        credential_ref=credential_ref,
                        scopes=frozenset(
                            {
                                "workflow:start",
                                "runtime:admission",
                                "inventory.read",
                                "sales_profit.read",
                                "supply_chain.preview",
                            }
                        ),
                    ),
                ),
            ),
            **settings,
        )


__all__ = [
    "SUPPLY_CHAIN_ROUTE",
    "BffToolGatewayPolicyPort",
    "DynamicGatewayContext",
    "GatewayAuthorityProfile",
    "GatewayPolicyRoute",
    "GatewayWorkflowOffer",
    "SharedToolGatewayComposition",
    "SupplyChainToolGatewayComposition",
    "policy_routes",
]
