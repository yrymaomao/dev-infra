"""The CRM reception profile: what the CRM Case page contributes to the generic reception.

Phase 1 of the OpenClaw x CRM integration is read-only: one offer
(``crm-case-advice`` -> ``crm-case-advise-on-demand@1``), one input (``case_id``),
a workbench-session exchange for the BFF JWT (decision D-1) and a projection of
the ``case-advice-result`` envelope that shows the draft text verbatim (D-4)
and nothing else a buyer wrote.
"""

from .profile import PROFILE_VERSION, crm_owner, crm_profile

__all__ = ["PROFILE_VERSION", "crm_owner", "crm_profile"]
