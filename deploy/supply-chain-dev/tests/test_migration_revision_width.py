"""Regression for a published long revision on a fresh Alembic database."""

import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


def test_long_revision_widens_tracking_before_business_columns():
    root = Path(__file__).resolve().parents[3]
    path = Path(
        os.environ.get(
            "SC_MIGRATION_FIXTURE_PATH",
            str(root / "bff_migrations/versions/0010_supply_chain_user_credentials.py"),
        )
    )
    spec = spec_from_file_location("sc_user_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    calls = []
    migration.op = SimpleNamespace(
        alter_column=lambda *args, **kwargs: calls.append(("alter", args, kwargs)),
        add_column=lambda *args, **kwargs: calls.append(("add", args, kwargs)),
        create_index=lambda *args, **kwargs: calls.append(("index", args, kwargs)),
    )
    assert len(migration.revision) > 32
    migration.upgrade()
    operation, args, kwargs = calls[0]
    assert operation == "alter"
    assert args == ("alembic_version", "version_num")
    assert kwargs["schema"] == "supply_chain_bff"
    assert kwargs["existing_type"].length == 32
    assert kwargs["type_"].length >= len(migration.revision)
    assert [args[0] for op, args, _ in calls if op == "add"] == [
        "selection_preview",
        "selection_preview",
        "report_run",
        "report_run",
        "report_schedule",
        "report_schedule",
    ]
