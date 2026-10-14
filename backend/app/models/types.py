"""Column types that need normalisation on the way into the database."""

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

# Deliberately permissive: it rejects obvious nonsense without pulling in an
# email-validation dependency, and the database check constraint guarantees the
# stored form is lower-case.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def normalize_email(value: str) -> str:
    return value.strip().lower()


class Email(TypeDecorator):
    """Always stores a lower-cased, trimmed address so uniqueness is case-insensitive."""

    impl = String(320)
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: object) -> str | None:
        return normalize_email(value) if value is not None else None
