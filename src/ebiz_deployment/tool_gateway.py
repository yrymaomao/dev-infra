"""One deployment-owned Gateway composition with independently scoped workflow offers.

One Runtime process receives one composition with one pin per business agent
offer. Every offer carries its own authority profile (scopes and credential
reference - never unioned) and names the BFF reception profile that answers the
Gateway's current-policy question for it: the policy port routes each query by
the agent prefix of the signed identity's ``session_key`` to that profile's
``/internal/<agent>/v2/openclaw/authorize``.

The offers of one agent id form one *instance* (design 2026-09-16 §12.2): the
OpenClaw host process that serves that agent loads one signed catalog generation
containing only that instance's offers, under that instance's own generation id.
The composition therefore owns one ``GatewayCatalogGeneration`` per instance and
answers each instance's catalog and invocation requests from that generation
alone; a single-instance deployment keeps exactly the one generation it had.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
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
from ebiz_runtime_contracts.tool_gateway_catalog import (
    CatalogSearchReply,
    CatalogSearchRequest,
    CatalogVisibilityReply,
    CatalogVisibilityRequest,
    InvocationRequest,
    ToolOffer,
    ToolOfferReference,
)
from ebiz_runtime_contracts.tool_gateway_policy import (
    PolicyTarget,
    ToolGatewayPolicyReply,
    ToolGatewayPolicyRequest,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

_SESSION_KEY_PREFIX = re.compile(r"^agent:[a-z0-9][a-z0-9._-]{0,63}:openclaw:$")
_AGENT_OF_PREFIX = re.compile(r"^agent:(?P<agent>[a-z0-9][a-z0-9._-]{0,63}):openclaw:$")
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


@dataclass(frozen=True, slots=True)
class GatewayInstance:
    """The offers one OpenClaw host instance serves and the generation it loads.

    One instance is one agent id, one BFF reception profile and one host OS
    process (design §12.2). Its signed generation carries only these offers.
    """

    agent_id: str
    session_key_prefix: str
    generation: GatewayCatalogGeneration
    offers: tuple[GatewayWorkflowOffer, ...]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(offer.pin.name for offer in self.offers)


def gateway_instances(
    *,
    tenant_id: str,
    offers: tuple[GatewayWorkflowOffer, ...],
    generation_id: str,
    catalog_revision: str,
    instance_generation_ids: Mapping[str, str] | None = None,
) -> tuple[GatewayInstance, ...]:
    """Group offers by agent instance, each under its own generation id.

    ``generation_id`` is the id of every instance that ``instance_generation_ids``
    does not name; with one instance that is today's single generation, unchanged.
    Every instance must end up with a distinct id, because the Runtime catalog
    answers requests by generation id and an OpenClaw instance pins exactly one.
    """

    overrides = dict(instance_generation_ids or {})
    grouped: dict[str, list[GatewayWorkflowOffer]] = {}
    for offer in offers:
        grouped.setdefault(offer.authority.session_key_prefix, []).append(offer)
    instances: list[GatewayInstance] = []
    for prefix, group in grouped.items():
        matched = _AGENT_OF_PREFIX.fullmatch(prefix)
        if matched is None:  # pragma: no cover - GatewayPolicyRoute already refused it
            raise ValueError("Tool Gateway policy route needs an agent session-key prefix")
        agent_id = matched.group("agent")
        instances.append(
            GatewayInstance(
                agent_id=agent_id,
                session_key_prefix=prefix,
                generation=GatewayCatalogGeneration(
                    tenant_id=tenant_id,
                    generation_id=overrides.pop(agent_id, generation_id),
                    catalog_revision=catalog_revision,
                    pins=tuple(offer.pin for offer in group),
                ),
                offers=tuple(group),
            )
        )
    if overrides:
        raise ValueError("instance generation ids name agents that serve no offer")
    ids = [instance.generation.generation_id for instance in instances]
    if len(set(ids)) != len(ids):
        raise ValueError("each OpenClaw host instance needs its own generation id")
    return tuple(instances)


#: The public catalog reads the Runtime API routes to the container's catalog
#: service. ``InstanceRoutedCatalog`` forwards exactly these; a Runtime that adds
#: one must be re-reviewed here (guarded by tests).
ROUTED_CATALOG_METHODS = ("visibility", "search", "describe", "prepare_invocation")


class InstanceRoutedCatalog:
    """Answer each OpenClaw instance from that instance's own catalog generation.

    Runtime's ``ToolGatewayCatalogService`` owns exactly one generation and
    refuses any other generation id. One Runtime process serving several host
    instances therefore keeps one service per instance generation and selects it
    by the ``generation_id`` the caller pins. An unknown id is handed to the
    default service, which authorizes first and then raises the same publication
    conflict Runtime raises, so nobody can enumerate generation ids without a
    current authorization.
    """

    def __init__(self, services: Mapping[str, ToolGatewayCatalogService], *, default: str) -> None:
        if not services or default not in services:
            raise ValueError("instance catalogs require a default generation")
        self._services = dict(services)
        self._default = default

    @property
    def generation_ids(self) -> tuple[str, ...]:
        return tuple(self._services)

    def _for(self, generation_id: str) -> ToolGatewayCatalogService:
        return self._services.get(generation_id, self._services[self._default])

    async def visibility(
        self, authorization: str | None, request: CatalogVisibilityRequest
    ) -> CatalogVisibilityReply:
        return await self._for(request.generation_id).visibility(authorization, request)

    async def search(
        self, authorization: str | None, request: CatalogSearchRequest
    ) -> CatalogSearchReply:
        return await self._for(request.generation_id).search(authorization, request)

    async def describe(
        self, authorization: str | None, *, reference: ToolOfferReference
    ) -> ToolOffer:
        return await self._for(reference.generation_id).describe(authorization, reference=reference)

    async def prepare_invocation(
        self, authorization: str | None, request: InvocationRequest
    ) -> ToolOffer:
        return await self._for(request.reference.generation_id).prepare_invocation(
            authorization, request
        )


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
        instance_generation_ids: Mapping[str, str] | None = None,
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
        if len({offer.pin.name.casefold() for offer in offers}) != len(offers):
            raise ValueError("Tool Gateway native tool names must be unique")
        self.instances = gateway_instances(
            tenant_id=tenant_id,
            offers=offers,
            generation_id=generation_id,
            catalog_revision=catalog_revision,
            instance_generation_ids=instance_generation_ids,
        )
        self._tenant_id = tenant_id
        self.offers = offers
        self.routes = policy_routes(offers)
        self._bff_url = bff_url.rstrip("/")
        self._connector_credential = connector_credential
        self._jwt_key = jwt_key
        self._jwt_issuer = jwt_issuer
        self._jwt_audience = jwt_audience

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def generations(self) -> tuple[GatewayCatalogGeneration, ...]:
        return tuple(instance.generation for instance in self.instances)

    @property
    def generation(self) -> GatewayCatalogGeneration:
        """The one generation of a single-instance deployment (the pre-§12.2 export path)."""

        if len(self.instances) != 1:
            raise ValueError(
                "this composition serves several OpenClaw instances; export per instance"
            )
        return self.instances[0].generation

    async def compose(
        self, container: Any, authority: ExecutionAdmissionAuthority
    ) -> GatewayServices:
        workflows = PostgresWorkflowRegistry(container.unit_of_work_factory)
        capabilities = PostgresCapabilityRegistry(container.unit_of_work_factory)
        publications = {}
        for offer in self.offers:
            pin = offer.pin
            publication = await workflows.get_published(self._tenant_id, pin.code, pin.version)
            if publication.checksum != pin.publication_digest:
                raise ValueError("published Workflow differs from the deployment pin")
            publications[pin.offer_id] = publication
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
            catalogs: dict[str, ToolGatewayCatalogService] = {}
            for instance in self.instances:
                catalogs[
                    instance.generation.generation_id
                ] = await ToolGatewayCatalogService.create(
                    generation=instance.generation,
                    projector=projector,
                    authorizer=authorizer,
                )
            catalog: ToolGatewayCatalogService
            if len(catalogs) == 1:
                (catalog,) = catalogs.values()
            else:
                # Runtime types the container's catalog as its own service; the
                # router forwards exactly the public reads the API routes use.
                catalog = cast(
                    ToolGatewayCatalogService,
                    InstanceRoutedCatalog(
                        catalogs, default=self.instances[0].generation.generation_id
                    ),
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
            for instance in self.instances:
                generation = instance.generation
                for offer in instance.offers:
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
                                pinned_workflow=publications[pin.offer_id],
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
    "ROUTED_CATALOG_METHODS",
    "SUPPLY_CHAIN_ROUTE",
    "BffToolGatewayPolicyPort",
    "DynamicGatewayContext",
    "GatewayAuthorityProfile",
    "GatewayInstance",
    "GatewayPolicyRoute",
    "GatewayWorkflowOffer",
    "InstanceRoutedCatalog",
    "SharedToolGatewayComposition",
    "SupplyChainToolGatewayComposition",
    "gateway_instances",
    "policy_routes",
]
