from __future__ import annotations

from ebiz_deployment.supply_chain_bff.level2_models import ReportOutbox
from ebiz_deployment.supply_chain_bff.models import SCHEMA, Base


def test_level2_schema_is_additive_and_keeps_skus_out_of_tables() -> None:
    names = {table.name for table in Base.metadata.tables.values()}
    assert {
        "selection_preview",
        "policy_version",
        "report_schedule",
        "report_run",
        "report_batch",
        "report_outbox",
        "report_inbox",
        "report_activity",
    } <= names
    assert {table.schema for table in Base.metadata.tables.values()} == {SCHEMA}
    for table in Base.metadata.tables.values():
        assert "sku" not in table.columns
        assert "skus" not in table.columns


def test_outbox_payload_is_opaque_batch_metadata_only() -> None:
    assert set(ReportOutbox.__table__.columns.keys()) == {
        "event_id",
        "tenant_id",
        "report_run_id",
        "batch_id",
        "event_type",
        "payload",
        "status",
        "attempt_count",
        "next_attempt_at",
        "lease_owner",
        "lease_expires_at",
        "published_at",
        "created_at",
    }
