"""The release material must pin the real Alembic head, never a stale revision."""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

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
