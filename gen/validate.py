"""XSD validation against the public ISO 20022 schemas in schemas/."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from lxml import etree

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


@lru_cache(maxsize=None)
def _schema(name: str) -> etree.XMLSchema:
    return etree.XMLSchema(etree.parse(str(SCHEMA_DIR / f"{name}.xsd")))


def validate(xml: bytes, schema: str) -> list[str]:
    """Return a list of validation errors; empty means valid."""
    s = _schema(schema)
    s.validate(etree.fromstring(xml))
    return [f"line {e.line}: {e.message}" for e in s.error_log]
