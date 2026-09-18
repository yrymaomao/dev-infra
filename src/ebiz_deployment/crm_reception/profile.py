"""The CRM reception profile."""

from __future__ import annotations

from ebiz_deployment.bff_persistence import SCHEMA
from ebiz_deployment.openclaw_reception.conversation_api import OwnerPolicy, tenant_owner
from ebiz_deployment.openclaw_reception.profile import ReceptionProfile, session_key_prefix_for
from ebiz_deployment.supply_chain_bff.config import BffSettings

from .result import project_case_advice

PROFILE_VERSION = "crm-reception.v1"
API_PREFIX = "/api/crm/v2/openclaw"
INTERNAL_PREFIX = "/internal/crm/v2/openclaw"
PAYLOAD_PERMISSION = "crm:reception"
OFFER_ID = "crm-case-advice"


def crm_profile(settings: BffSettings) -> ReceptionProfile:
    return ReceptionProfile(
        agent_id=settings.crm_openclaw_agent_id,
        profile_version=PROFILE_VERSION,
        schema=SCHEMA,
        api_prefix=API_PREFIX,
        internal_prefix=INTERNAL_PREFIX,
        offer_ids=frozenset({settings.crm_openclaw_offer_id}),
        session_key_prefix=session_key_prefix_for(settings.crm_openclaw_agent_id),
        payload_permission=PAYLOAD_PERMISSION,
        project_result=project_case_advice,
    )


def crm_owner(settings: BffSettings) -> OwnerPolicy:
    """The principal comes from the JWT the session exchange minted; only the tenant is fixed."""

    return tenant_owner(
        enabled=settings.crm_openclaw_enabled, tenant_id=settings.crm_openclaw_tenant_id
    )


__all__ = [
    "API_PREFIX",
    "INTERNAL_PREFIX",
    "OFFER_ID",
    "PAYLOAD_PERMISSION",
    "PROFILE_VERSION",
    "crm_owner",
    "crm_profile",
]
