"""Shared declarative base of the deployment-owned BFF persistence.

One PostgreSQL schema (``supply_chain_bff`` - the historical name, kept so the
single Alembic chain and the 0008 foreign key from ``openclaw_run_binding`` to
``openclaw_conversation_turn`` stay intact) carries both the Supply Chain
Level 2 tables and the generic OpenClaw reception tables. Both model modules
import the base from here, so the generic reception package never imports the
Supply Chain package and the Supply Chain package keeps re-exporting the same
``SCHEMA``/``Base`` names it always had.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase

SCHEMA = "supply_chain_bff"


class Base(DeclarativeBase):
    pass


__all__ = ["SCHEMA", "Base"]
