"""The per-agent contract of the generic reception."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ebiz_deployment.bff_persistence import SCHEMA

_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROFILE_VERSION = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
_ROUTE_PREFIX = re.compile(r"^/[A-Za-z0-9._/-]*[A-Za-z0-9]$")
_OFFER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_PERMISSION = re.compile(r"^[a-z][a-z0-9-]*:[a-z][a-z0-9-]*$")

#: Projects one verified operation result into the safe display document that
#: is stored on the ``operation.updated`` event. Receives the repository (for
#: restricted payload loads), the tenant of the conversation and the raw
#: Runtime result; must raise ``ValueError`` on a tenant mismatch or any
#: contract violation - nothing unverified is ever shown.
ResultProjector = Callable[[Any, str, object], Awaitable[dict[str, Any]]]
#: Optionally rewrites the assistant text once the turn's projected analyses
#: are known (Supply Chain strips model-invented currency symbols). Identity
#: by default.
ReplyFinalizer = Callable[[str, list[dict[str, Any]]], str]


def session_key_prefix_for(agent_id: str) -> str:
    """The prefix every run binding of one OpenClaw agent instance carries.

    The Gateway policy port routes an authorization query to the profile whose
    prefix matches ``identity.session_key``, so the prefix must be derived from
    the same ``agent_id`` on both sides of the deployment.
    """

    if _AGENT_ID.fullmatch(agent_id) is None:
        raise ValueError("OpenClaw agent id must be a normalized identifier")
    return f"agent:{agent_id}:openclaw:"


def keep_reply(text: str, analyses: list[dict[str, Any]]) -> str:
    del analyses
    return text


@dataclass(frozen=True, slots=True)
class ReceptionProfile:
    """Everything the generic reception needs to know about one business agent."""

    agent_id: str
    profile_version: str
    schema: str
    api_prefix: str
    internal_prefix: str
    offer_ids: frozenset[str]
    session_key_prefix: str
    payload_permission: str
    project_result: ResultProjector
    finalize_reply: ReplyFinalizer = keep_reply

    def __post_init__(self) -> None:
        if _AGENT_ID.fullmatch(self.agent_id) is None:
            raise ValueError("reception profile agent_id must be a normalized identifier")
        if _PROFILE_VERSION.fullmatch(self.profile_version) is None:
            raise ValueError("reception profile_version must be a bounded version label")
        if self.schema != SCHEMA:
            raise ValueError("reception profiles share the single BFF schema")
        for label, prefix in (("api", self.api_prefix), ("internal", self.internal_prefix)):
            if _ROUTE_PREFIX.fullmatch(prefix) is None:
                raise ValueError(f"reception {label}_prefix must be an absolute route prefix")
        if self.api_prefix == self.internal_prefix:
            raise ValueError("reception api_prefix and internal_prefix must differ")
        if not self.offer_ids or any(_OFFER_ID.fullmatch(o) is None for o in self.offer_ids):
            raise ValueError("reception offer_ids must name at least one normalized offer")
        if self.session_key_prefix != session_key_prefix_for(self.agent_id):
            raise ValueError("reception session_key_prefix must derive from agent_id")
        if _PERMISSION.fullmatch(self.payload_permission) is None:
            raise ValueError("reception payload_permission must be a bounded scope")
        if not callable(self.project_result) or not callable(self.finalize_reply):
            raise ValueError("reception profile hooks must be callables")

    def owns_session_key(self, session_key: str) -> bool:
        return session_key.startswith(self.session_key_prefix)


__all__ = [
    "ReceptionProfile",
    "ReplyFinalizer",
    "ResultProjector",
    "keep_reply",
    "session_key_prefix_for",
]
