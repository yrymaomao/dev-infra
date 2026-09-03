"""Package-aware Alembic migration entrypoint."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def upgrade(database_url: str) -> None:
    package_root = Path(__file__).resolve().parent
    source_root = Path(__file__).resolve().parents[3]
    ini = package_root / "alembic.ini"
    scripts = package_root / "migrations"
    if not ini.is_file():
        ini = source_root / "bff-alembic.ini"
        scripts = source_root / "bff_migrations"
    config = Config(str(ini))
    config.set_main_option("script_location", str(scripts))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
