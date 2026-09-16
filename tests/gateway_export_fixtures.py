"""An offer projector with the two Phase 1 offers, for export tests and the local proof.

Runtime's ``export_gateway_generation`` only needs ``project_exact(generation,
pin).offer``. This projector answers with a full ``ToolOffer`` per pin, using
the exact input shapes the two receptions admit (``skus`` array, single
``case_id``), so the exported bytes are real wire payloads the Adapter's
``parseGeneration`` admits - not a Registry export, and every test and the
evidence directory say so.
"""

from __future__ import annotations

import hashlib
from typing import Any

from agent_runtime.application.tool_gateway_projection import (
    GatewayCatalogGeneration,
    GatewayPublicationPin,
    GatewayPublicationProjection,
)
from ebiz_runtime_contracts import canonical_json_bytes
from ebiz_runtime_contracts.tool_gateway_catalog import ToolOffer, ToolOfferReference

TOKEN = "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "inventory_supply_chain_on_demand": {
        "type": "object",
        "properties": {
            "skus": {
                "type": "array",
                "items": {"type": "string", "pattern": TOKEN},
                "minItems": 1,
                "maxItems": 200,
            }
        },
        "required": ["skus"],
        "additionalProperties": False,
    },
    "crm_case_advice": {
        "type": "object",
        "properties": {"case_id": {"type": "string", "pattern": TOKEN}},
        "required": ["case_id"],
        "additionalProperties": False,
    },
}
OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "inventory_supply_chain_on_demand": {
        "type": "object",
        "properties": {"result_status": {"type": "string"}},
        "required": ["result_status"],
    },
    "crm_case_advice": {
        "type": "object",
        "properties": {"outcome": {"type": "string"}},
        "required": ["outcome"],
    },
}


def schema_digest(schema: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(schema)).hexdigest()


class FixtureProjector:
    """Projects every pin it knows; unknown names are a hard failure."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def project_exact(
        self, generation: GatewayCatalogGeneration, pin: GatewayPublicationPin
    ) -> GatewayPublicationProjection:
        self.calls.append((generation.generation_id, pin.offer_id))
        input_schema = INPUT_SCHEMAS[pin.name]
        output_schema = OUTPUT_SCHEMAS[pin.name]
        offer = ToolOffer(
            reference=ToolOfferReference(
                generation_id=generation.generation_id,
                offer_id=pin.offer_id,
                version=pin.version,
                publication_digest=pin.publication_digest,
                input_schema_digest=schema_digest(input_schema),
                output_schema_digest=schema_digest(output_schema),
            ),
            name=pin.name,
            aliases=pin.aliases,
            labels=pin.labels,
            effect="PREVIEW",
            input_schema=input_schema,
            output_schema=output_schema,
            catalog_revision=generation.catalog_revision,
        )
        return GatewayPublicationProjection(offer=offer, verification_digest=pin.publication_digest)
