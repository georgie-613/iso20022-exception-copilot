"""Fake bank directory.

Every BIC here is synthetic. All of them use a location code whose second
character is '0', which in the BIC convention marks a test-and-training
BIC, so none of them can be a live production BIC.
"""

from __future__ import annotations

import random

from .model import Agent

# (bank code, country, location, name)
_BANKS: list[tuple[str, str, str, str]] = [
    # Our bank (debtor agent / instructing agent)
    ("FSYN", "US", "N0", "First Synthetic Bank"),
    # US correspondents (instructed agent on the USD leg)
    ("CORX", "US", "N0", "Corrient Clearing Bank"),
    ("HDSN", "US", "N0", "Hudson Fictional Trust"),
    ("GTWY", "US", "M0", "Gateway Imaginary Bank"),
    # Creditor agents, LATAM
    ("BMXF", "MX", "M0", "Banco Mexicano Ficticio"),
    ("NRTE", "MX", "M0", "Banco del Norte Simulado"),
    ("PCFC", "MX", "G0", "Banco Pacifico Imaginario"),
    ("BRSL", "BR", "S0", "Banco Brasileiro Sintetico"),
    ("PAUL", "BR", "S0", "Banco Paulista Ficticio"),
    ("RIOB", "BR", "R0", "Banco Carioca Simulado"),
    ("COLF", "CO", "B0", "Banco Colombiano Ficticio"),
    ("ANDE", "CO", "M0", "Banco Andino Simulado"),
    ("CHLE", "CL", "S0", "Banco Austral Ficticio"),
    ("VLPO", "CL", "V0", "Banco Valparaiso Simulado"),
    ("PERU", "PE", "L0", "Banco Limeno Ficticio"),
    ("INCA", "PE", "L0", "Banco Inca Simulado"),
    ("TICO", "CR", "S0", "Banco Tico Ficticio"),
    ("GUAP", "CR", "S0", "Banco Caribe Simulado"),
    ("PLTA", "AR", "B0", "Banco del Plata Ficticio"),
    ("CRDB", "AR", "C0", "Banco Cordobes Simulado"),
    ("PNMA", "PA", "P0", "Banco Istmo Ficticio"),
    ("DOMR", "DO", "S0", "Banco Quisqueya Simulado"),
    # Creditor agents, Europe
    ("IBRF", "ES", "M0", "Banco Iberico Ficticio"),
    ("CATB", "ES", "B0", "Banco Catalan Simulado"),
    ("THMS", "GB", "L0", "Thames Fictional Bank"),
    ("MRCN", "GB", "L0", "Mercian Imaginary Bank"),
    ("RHNB", "DE", "F0", "Rheinische Fiktivbank"),
    ("ELBE", "DE", "H0", "Elbe Simulationsbank"),
    ("TEJO", "PT", "L0", "Banco Tejo Ficticio"),
    ("SEIN", "FR", "P0", "Banque Seine Fictive"),
]

OUR_BANK_CODE = "FSYN"
CORRESPONDENT_CODES = ("CORX", "HDSN", "GTWY")


def _bic(code: str, ctry: str, loc: str) -> str:
    return f"{code}{ctry}{loc}"


class Directory:
    def __init__(self) -> None:
        self.banks: list[dict] = [
            {"bic": _bic(c, ctry, loc), "name": name, "country": ctry}
            for c, ctry, loc, name in _BANKS
        ]
        self._by_bic = {b["bic"]: b for b in self.banks}

    def __contains__(self, bic: str) -> bool:
        return bic in self._by_bic

    def lookup(self, bic: str) -> dict | None:
        return self._by_bic.get(bic)

    def find_by_name(self, name: str) -> dict | None:
        for b in self.banks:
            if b["name"] == name:
                return b
        return None

    def agent(self, bic: str) -> Agent:
        b = self._by_bic[bic]
        return Agent(bic=b["bic"], name=b["name"])

    @property
    def our_bank(self) -> Agent:
        return self.agent(next(b["bic"] for b in self.banks if b["bic"].startswith(OUR_BANK_CODE)))

    def correspondents(self) -> list[Agent]:
        return [self.agent(b["bic"]) for b in self.banks if b["bic"][:4] in CORRESPONDENT_CODES]

    def creditor_agents(self) -> list[Agent]:
        return [self.agent(b["bic"]) for b in self.banks if b["country"] != "US"]

    def unlisted_bic(self, rng: random.Random, country: str) -> str:
        """A well-formed test BIC for `country` that is NOT in the directory."""
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        while True:
            code = "".join(rng.choice(letters) for _ in range(4))
            bic = _bic(code, country, rng.choice(letters) + "0")
            if bic not in self:
                return bic
