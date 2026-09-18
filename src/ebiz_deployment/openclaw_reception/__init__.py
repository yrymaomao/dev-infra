"""Generic OpenClaw reception: one implementation, one profile per business agent.

The BFF receives workbench conversations, persists turns and their Adapter
events, binds OpenClaw runs to a tenant and principal, answers the Tool
Gateway's current-authorization queries and projects verified results for
display. None of that is Supply Chain specific, so it lives here once and each
business agent (Supply Chain, CRM) contributes a :class:`ReceptionProfile`:
its route prefixes, its offer ids, its payload permission and its result
projection. Design decision D-5 of the OpenClaw x CRM integration.
"""

from .profile import ReceptionProfile, session_key_prefix_for

__all__ = ["ReceptionProfile", "session_key_prefix_for"]
