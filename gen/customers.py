"""Fake debtor customers (our bank's clients) and fake beneficiaries.

The debtor customer master is what a debtor-agent analyst can see, so a
missing debtor address (RR02) is repairable by enrichment. Beneficiary data
is NOT in our master data, so a missing creditor address (RR03) is not.
"""

from __future__ import annotations

import random

from .model import Account, Address, Party

# 20 debtor customers: name, street, number, zip, city (all US, all invented)
_DEBTORS = [
    ("Bayshore Freight LLC", "Harbor View Rd", "4100", "33602", "Tampa"),
    ("Coral Grove Imports Inc", "Palmetto Ave", "215", "33130", "Miami"),
    ("Lakeland Citrus Partners", "Orange Blossom Way", "88", "33801", "Lakeland"),
    ("Gulfstream Medical Supply", "Seaside Blvd", "1200", "33701", "St Petersburg"),
    ("Pinecrest Tools Co", "Cypress Lane", "57", "32801", "Orlando"),
    ("Riverside Textiles Corp", "Mill St", "903", "28202", "Charlotte"),
    ("Magnolia Coffee Traders", "Bean Market St", "14", "70112", "New Orleans"),
    ("Lone Oak Machinery LLC", "Industrial Pkwy", "7700", "77002", "Houston"),
    ("Desert Bloom Agro Inc", "Mesa Dr", "3150", "85004", "Phoenix"),
    ("Harborline Logistics", "Pier Ave", "600", "90802", "Long Beach"),
    ("Summit Peak Outfitters", "Alpine Rd", "42", "80202", "Denver"),
    ("Blue Heron Seafood Co", "Wharf St", "19", "02210", "Boston"),
    ("Keystone Auto Parts", "Liberty Ave", "1550", "15222", "Pittsburgh"),
    ("Prairie Grain Exporters", "Elevator Rd", "300", "68102", "Omaha"),
    ("Silver Lake Software LLC", "Sunset Blvd", "2900", "90026", "Los Angeles"),
    ("Evergreen Timber Supply", "Fir St", "811", "98101", "Seattle"),
    ("Cobalt Pharma Distributors", "Research Pkwy", "45", "27709", "Durham"),
    ("Sandhill Ceramics Inc", "Kiln Rd", "66", "29201", "Columbia"),
    ("Ironbridge Steel Trading", "Foundry St", "1020", "60607", "Chicago"),
    ("Northstar Electronics Co", "Circuit Dr", "500", "55401", "Minneapolis"),
]

# Beneficiary name stems and streets per country (invented)
_BENEF = {
    "MX": (["Distribuidora", "Comercializadora", "Textiles", "Agroexportadora"],
           ["Av Reforma", "Calle Hidalgo", "Blvd Juarez"], ["Ciudad de Mexico", "Monterrey", "Guadalajara"], "{n:05d}"),
    "BR": (["Comercial", "Industria", "Exportadora", "Distribuidora"],
           ["Rua Augusta", "Av Paulista", "Rua das Flores"], ["Sao Paulo", "Rio de Janeiro", "Curitiba"], "{n:05d}-000"),
    "CO": (["Comercializadora", "Inversiones", "Textiles", "Cafes"],
           ["Calle 72", "Carrera 7", "Av El Dorado"], ["Bogota", "Medellin", "Cali"], "{n:06d}"),
    "CL": (["Comercial", "Exportadora", "Vinos", "Agricola"],
           ["Av Providencia", "Calle Huerfanos", "Av Apoquindo"], ["Santiago", "Valparaiso"], "{n:07d}"),
    "PE": (["Inversiones", "Textil", "Agroindustrias", "Minera"],
           ["Av Arequipa", "Jr de la Union", "Av Javier Prado"], ["Lima", "Arequipa"], "LIMA {n:02d}"),
    "CR": (["Inversiones", "Exportadora", "Cafetalera", "Comercial"],
           ["Avenida Central", "Calle 5", "Paseo Colon"], ["San Jose", "Limon", "Puntarenas"], "{n:05d}"),
    "AR": (["Distribuidora", "Comercial", "Bodegas", "Industrias"],
           ["Av Corrientes", "Calle Florida", "Av Santa Fe"], ["Buenos Aires", "Cordoba"], "C{n:04d}"),
    "PA": (["Inversiones", "Logistica", "Comercial"],
           ["Calle 50", "Av Balboa"], ["Panama City"], "{n:04d}"),
    "DO": (["Comercial", "Agroexportadora", "Distribuidora"],
           ["Av Winston Churchill", "Calle El Conde"], ["Santo Domingo"], "{n:05d}"),
    "ES": (["Comercial", "Distribuciones", "Exportaciones"],
           ["Calle Mayor", "Gran Via", "Paseo de Gracia"], ["Madrid", "Barcelona"], "{n:05d}"),
    "GB": (["Trading", "Supplies", "Imports"],
           ["High Street", "Kings Road", "Market Lane"], ["London", "Manchester"], "EC{n:d}A 1AA"),
    "DE": (["Handels", "Technik", "Logistik"],
           ["Hauptstrasse", "Bahnhofstrasse", "Kaiserstrasse"], ["Frankfurt", "Hamburg"], "{n:05d}"),
    "PT": (["Comercial", "Exportadora", "Distribuicao"],
           ["Rua Augusta", "Av da Liberdade"], ["Lisboa", "Porto"], "{n:04d}-001"),
    "FR": (["Negoce", "Import", "Distribution"],
           ["Rue de Rivoli", "Avenue Victor Hugo"], ["Paris", "Lyon"], "{n:05d}"),
}
_SUFFIX = {"MX": "SA de CV", "BR": "Ltda", "CO": "SAS", "CL": "SpA", "PE": "SAC", "CR": "SA",
           "AR": "SRL", "PA": "SA", "DO": "SRL", "ES": "SL", "GB": "Ltd", "DE": "GmbH",
           "PT": "Lda", "FR": "SARL"}
_TRADE = ["Aurora", "Delta", "Sol", "Horizonte", "Cumbre", "Atlas", "Brisa", "Faro", "Nova", "Puente"]

# IBAN BBAN shapes: (digits, trailing literal) — check digits computed below
_IBAN_BBAN = {"BR": 23, "CR": 18, "ES": 20, "DE": 18, "PT": 21, "FR": 23, "GB": 14, "DO": 24}


def debtor_customers() -> list[tuple[Party, Account]]:
    out = []
    for i, (name, st, nb, pc, city) in enumerate(_DEBTORS):
        party = Party(name=name, address=Address(strt_nm=st, bldg_nb=nb, pst_cd=pc, twn_nm=city, ctry="US"))
        out.append((party, Account(other_id=f"{4000100000 + i * 7919:010d}")))
    return out


def debtor_master() -> dict[str, Address]:
    """Our KYC master record: debtor name -> address. Used to repair RR02."""
    return {p.name: p.address for p, _ in debtor_customers()}


def iban_check_digits(country: str, bban: str) -> str:
    rearranged = bban + country + "00"
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return f"{98 - int(numeric) % 97:02d}"


def iban_is_valid(iban: str) -> bool:
    rearranged = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def make_iban(rng: random.Random, country: str, bank_code: str) -> str:
    digits = "".join(rng.choice("0123456789") for _ in range(_IBAN_BBAN[country]))
    if country == "GB":
        bban = bank_code + digits
    elif country == "BR":
        bban = digits + "C1"
    elif country == "CR":
        bban = "0" + digits[1:]
    elif country == "DO":
        bban = bank_code + digits[4:]
    else:
        bban = digits
    return f"{country}{iban_check_digits(country, bban)}{bban}"


def make_beneficiary(rng: random.Random, country: str, bank_code: str) -> tuple[Party, Account]:
    stems, streets, cities, pc_fmt = _BENEF[country]
    name = f"{rng.choice(stems)} {rng.choice(_TRADE)} {_SUFFIX[country]}"
    addr = Address(
        strt_nm=rng.choice(streets),
        bldg_nb=str(rng.randint(1, 2500)),
        pst_cd=pc_fmt.format(n=rng.randint(1, 99)),
        twn_nm=rng.choice(cities),
        ctry=country,
    )
    if country in _IBAN_BBAN:
        acct = Account(iban=make_iban(rng, country, bank_code))
    else:
        acct = Account(other_id="".join(rng.choice("0123456789") for _ in range(rng.choice([11, 12, 14, 18]))))
    return Party(name=name, address=addr), acct
