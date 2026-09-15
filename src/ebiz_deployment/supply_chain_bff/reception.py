"""The Supply Chain reception profile: what the Supply Chain page contributes.

Behaviour is unchanged from the pre-extraction BFF: the same prefixes, the
same ``supply-chain:level2`` payload permission, the same report-v2 projection
(``conversation_result.project_analysis``) registered as the profile's
``project_result`` hook, the same currency qualification of the reply, and the
same deployment-constant tenant/principal comparison in the owner policy.
"""

from __future__ import annotations

from ebiz_deployment.bff_persistence import SCHEMA
from ebiz_deployment.openclaw_reception.conversation_api import OwnerPolicy, constant_owner
from ebiz_deployment.openclaw_reception.profile import ReceptionProfile, session_key_prefix_for

from .config import BffSettings
from .conversation_result import project_analysis, qualify_unowned_currency_symbols

PROFILE_VERSION = "supply-chain-reception.v1"
API_PREFIX = "/api/supply-chain/v2/openclaw"
INTERNAL_PREFIX = "/internal/supply-chain/v2/openclaw"
PAYLOAD_PERMISSION = "supply-chain:level2"


def supply_chain_profile(settings: BffSettings) -> ReceptionProfile:
    return ReceptionProfile(
        agent_id=settings.openclaw_agent_id,
        profile_version=PROFILE_VERSION,
        schema=SCHEMA,
        api_prefix=API_PREFIX,
        internal_prefix=INTERNAL_PREFIX,
        offer_ids=frozenset({settings.openclaw_offer_id}),
        session_key_prefix=session_key_prefix_for(settings.openclaw_agent_id),
        payload_permission=PAYLOAD_PERMISSION,
        project_result=project_analysis,
        finalize_reply=qualify_unowned_currency_symbols,
    )


def supply_chain_owner(settings: BffSettings) -> OwnerPolicy:
    return constant_owner(
        enabled=settings.openclaw_enabled,
        tenant_id=settings.openclaw_tenant_id,
        principal_id=settings.openclaw_principal_id,
    )


__all__ = [
    "API_PREFIX",
    "INTERNAL_PREFIX",
    "PAYLOAD_PERMISSION",
    "PROFILE_VERSION",
    "supply_chain_owner",
    "supply_chain_profile",
]
