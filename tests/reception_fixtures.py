"""Reception profiles as the tests see them: one builder per business agent."""

from __future__ import annotations

from datetime import timedelta

from ebiz_deployment.openclaw_reception.conversation_api import (
    OwnerPolicy,
    constant_owner,
    tenant_owner,
)
from ebiz_deployment.openclaw_reception.profile import ReceptionProfile
from ebiz_deployment.supply_chain_bff.config import BffSettings
from ebiz_deployment.supply_chain_bff.cursor import CursorSigner
from ebiz_deployment.supply_chain_bff.eta import EtaProfile

TENANT = "tenant-a"
USER = "alice"


def bff_settings(**overrides: object) -> BffSettings:
    values: dict[str, object] = dict(
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_test",
        cursor_hmac_key=b"c" * 32,
        jwt_secret="j" * 32,
        runtime_url="http://127.0.0.1:8000",
        skill_input_ref="payload://skill/current",
        runtime_credential_ref="opaque:runtime-service",
        eta_profile=EtaProfile(
            version="test",
            fixed_seconds=1,
            per_item_seconds=1,
            concurrency=4,
            uncertainty_ratio=0.25,
        ),
    )
    values.update(overrides)
    return BffSettings(**values)  # type: ignore[arg-type]


def signer(ttl_seconds: int = 60, **kwargs: object) -> CursorSigner:
    return CursorSigner(b"x" * 32, ttl=timedelta(seconds=ttl_seconds), **kwargs)  # type: ignore[arg-type]


def supply_chain(settings: BffSettings | None = None) -> ReceptionProfile:
    from ebiz_deployment.supply_chain_bff.reception import supply_chain_profile

    return supply_chain_profile(settings or bff_settings())


def crm(settings: BffSettings | None = None) -> ReceptionProfile:
    from ebiz_deployment.crm_reception.profile import crm_profile

    return crm_profile(settings or bff_settings(crm_openclaw_tenant_id=TENANT))


def owner_for(profile: ReceptionProfile, *, tenant: str = TENANT, user: str = USER) -> OwnerPolicy:
    if profile.agent_id == "crm":
        return tenant_owner(enabled=True, tenant_id=tenant)
    return constant_owner(enabled=True, tenant_id=tenant, principal_id=user)


PROFILE_BUILDERS = {"supply-chain": supply_chain, "crm": crm}
PROFILE_IDS = tuple(PROFILE_BUILDERS)
