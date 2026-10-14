"""Declarative base for all SQLAlchemy models (Phase 0 defines none)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Single metadata registry Alembic autogenerates against."""
