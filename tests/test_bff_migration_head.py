"""The release material must pin the real Alembic head, never a stale revision."""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from ebiz_deployment import local_dev_assets
from ebiz_deployment.config import StreamingBffReleaseConfig

ROOT = Path(__file__).resolve().parents[1]


def alembic_head() -> str:
    config = Config(str(ROOT / "bff-alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "bff_migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, "the BFF keeps one linear Alembic chain"
    return heads[0]


def test_alembic_chain_head_is_the_profile_index_revision() -> None:
    assert alembic_head() == "0009_conversation_profile_index"


def test_every_migration_head_pin_matches_the_alembic_head() -> None:
    head = alembic_head()
    literal = StreamingBffReleaseConfig.model_fields["migration_head"].annotation
    assert get_args(literal) == (head,)
    assert local_dev_assets._streaming_bff_release()["migration_head"] == head
    example = json.loads((ROOT / "config/deployment.example.json").read_text(encoding="utf-8"))
    assert example["supply_chain_release"]["streaming_bff"]["migration_head"] == head


def test_profile_index_migration_is_expand_only_with_a_real_downgrade() -> None:
    source = (ROOT / "bff_migrations/versions/0009_conversation_profile_index.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision = "0008_conversation_reception"' in source
    assert "create_index" in source and "drop_index" in source
    assert "drop_table" not in source and "drop_column" not in source


def test_profile_index_migration_round_trips_on_postgresql() -> None:
    import asyncio
    import os

    from alembic import command
    from sqlalchemy import inspect
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    url = os.environ.get("SUPPLY_CHAIN_BFF_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Dedicated PostgreSQL test URL not configured")
    assert str(make_url(url).database).endswith("_test")
    config = Config(str(ROOT / "bff-alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "bff_migrations"))
    config.set_main_option("sqlalchemy.url", url)

    def index_names() -> set[str]:
        async def read() -> set[str]:
            engine = create_async_engine(url)
            try:
                async with engine.connect() as connection:
                    return await connection.run_sync(
                        lambda sync: {
                            index["name"]
                            for index in inspect(sync).get_indexes(
                                "openclaw_conversation", schema="supply_chain_bff"
                            )
                        }
                    )
            finally:
                await engine.dispose()

        return asyncio.run(read())

    command.upgrade(config, "head")
    assert "ix_openclaw_conversation_profile_tenant" in index_names()
    command.downgrade(config, "0008_conversation_reception")
    assert "ix_openclaw_conversation_profile_tenant" not in index_names()
    command.upgrade(config, "head")
    assert "ix_openclaw_conversation_profile_tenant" in index_names()
