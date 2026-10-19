"""Reference data the tools check against.

v1 uses the synthetic bank directory from the generator. In production this
is the adapter you swap for a real BIC directory feed; nothing else changes.
"""

from __future__ import annotations

from functools import lru_cache

from gen.directory import Directory


@lru_cache(maxsize=1)
def bic_directory() -> Directory:
    return Directory()


# Small allowlist of ISO 4217 codes this rail expects. Unknown codes are a
# warning, not an error: the reject reason decides, not us.
KNOWN_CURRENCIES = {"USD", "EUR", "GBP", "MXN", "BRL", "COP", "CLP", "PEN", "CRC", "ARS", "DOP", "PAB"}
