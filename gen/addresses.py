"""Lane C: outbound payments whose creditor address is fully unstructured.

Swift is retiring free-text-only addresses in cross-border payments: a CBPR+
hybrid address needs Town Name and Country as separate elements and at most
2 address lines; a structured address has none. Each case is a pacs.008 whose
creditor PstlAdr holds only AdrLine elements, the way most bank customer files
store beneficiaries today. There is no pacs.002: this is fixed before sending.

Three sets:
- address:          24 cases, generated in the formats the country rules know
- address-holdout:  13 hand-written cases in formats the rules were NOT written
                    for (abbreviations, reordered lines, missing country line,
                    countries with no rules yet, Costa Rica's directions-style
                    addresses). Report-only; never used to tune the rules.
- demo:             2 cases added to the main build for the approval screen

Labels: action "repair" with the true address in expected_value, or
"escalate" when a person must decide (country sources disagree, no town).

    python -m gen.addresses --seed 42 --out cases_address
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path

from lxml import etree

from .base import build_payment, to_xml
from .customers import make_beneficiary
from .directory import Directory
from .model import Label
from .validate import validate

# country -> a creditor agent bank code in that country
BANK = {"MX": "BMXF", "BR": "BRSL", "CO": "COLF", "AR": "PLTA", "GB": "THMS", "DE": "RHNB",
        "CL": "CHLE", "PE": "PERU", "CR": "TICO"}

# (id, agent country, address lines, true address or None, action, why)
Spec = tuple[str, str, list[str], dict | None, str, str]


def _a(**kw) -> dict:
    return kw


ADDRESS_EVAL: list[Spec] = [
    # Mexico: street number / Col. district / C.P. postcode town, state / country
    ("MX-1", "MX", ["Av. Paseo de la Reforma 222", "Col. Juarez", "C.P. 06600 Ciudad de Mexico, CDMX", "MEXICO"],
     _a(strt_nm="Av. Paseo de la Reforma", bldg_nb="222", dstrct_nm="Juarez", pst_cd="06600",
        twn_nm="Ciudad de Mexico", ctry_sub_dvsn="CDMX", ctry="MX"), "repair", "MX format"),
    ("MX-2", "MX", ["Calle Padre Mier 1450", "Col. Centro", "C.P. 64000 Monterrey, Nuevo Leon", "MEXICO"],
     _a(strt_nm="Calle Padre Mier", bldg_nb="1450", dstrct_nm="Centro", pst_cd="64000",
        twn_nm="Monterrey", ctry_sub_dvsn="Nuevo Leon", ctry="MX"), "repair", "MX format"),
    ("MX-3", "MX", ["Av. Chapultepec 480", "Col. Americana", "C.P. 44160 Guadalajara, Jalisco", "MEXICO"],
     _a(strt_nm="Av. Chapultepec", bldg_nb="480", dstrct_nm="Americana", pst_cd="44160",
        twn_nm="Guadalajara", ctry_sub_dvsn="Jalisco", ctry="MX"), "repair", "MX format"),
    # Brazil: street, number - bairro / town - UF / CEP / country
    ("BR-1", "BR", ["Rua Augusta, 1500 - Consolacao", "Sao Paulo - SP", "CEP 01304-001", "BRASIL"],
     _a(strt_nm="Rua Augusta", bldg_nb="1500", dstrct_nm="Consolacao", twn_nm="Sao Paulo",
        ctry_sub_dvsn="SP", pst_cd="01304-001", ctry="BR"), "repair", "BR format"),
    ("BR-2", "BR", ["Av. Atlantica, 1702 - Copacabana", "Rio de Janeiro - RJ", "CEP 22021-001", "BRASIL"],
     _a(strt_nm="Av. Atlantica", bldg_nb="1702", dstrct_nm="Copacabana", twn_nm="Rio de Janeiro",
        ctry_sub_dvsn="RJ", pst_cd="22021-001", ctry="BR"), "repair", "BR format"),
    ("BR-3", "BR", ["Rua da Bahia, 1148 - Centro", "Belo Horizonte - MG", "CEP 30160-011", "BRASIL"],
     _a(strt_nm="Rua da Bahia", bldg_nb="1148", dstrct_nm="Centro", twn_nm="Belo Horizonte",
        ctry_sub_dvsn="MG", pst_cd="30160-011", ctry="BR"), "repair", "BR format"),
    # Colombia: via # placa / town, department / postcode / country
    ("CO-1", "CO", ["Calle 93 # 11-26", "Bogota, Cundinamarca", "110221", "COLOMBIA"],
     _a(strt_nm="Calle 93", bldg_nb="11-26", twn_nm="Bogota", ctry_sub_dvsn="Cundinamarca",
        pst_cd="110221", ctry="CO"), "repair", "CO format"),
    ("CO-2", "CO", ["Carrera 43A # 1-50", "Medellin, Antioquia", "050021", "COLOMBIA"],
     _a(strt_nm="Carrera 43A", bldg_nb="1-50", twn_nm="Medellin", ctry_sub_dvsn="Antioquia",
        pst_cd="050021", ctry="CO"), "repair", "CO format"),
    ("CO-3", "CO", ["Avenida 6N # 23-45", "Cali, Valle del Cauca", "760045", "COLOMBIA"],
     _a(strt_nm="Avenida 6N", bldg_nb="23-45", twn_nm="Cali", ctry_sub_dvsn="Valle del Cauca",
        pst_cd="760045", ctry="CO"), "repair", "CO format"),
    # Argentina: street number / CPA town / province / country
    ("AR-1", "AR", ["Av. Corrientes 1234", "C1043AAZ Buenos Aires", "CABA", "ARGENTINA"],
     _a(strt_nm="Av. Corrientes", bldg_nb="1234", pst_cd="C1043AAZ", twn_nm="Buenos Aires",
        ctry_sub_dvsn="CABA", ctry="AR"), "repair", "AR format"),
    ("AR-2", "AR", ["Bv. San Juan 890", "X5000ATS Cordoba", "Cordoba", "ARGENTINA"],
     _a(strt_nm="Bv. San Juan", bldg_nb="890", pst_cd="X5000ATS", twn_nm="Cordoba",
        ctry_sub_dvsn="Cordoba", ctry="AR"), "repair", "AR format"),
    ("AR-3", "AR", ["Calle Cordoba 1452", "S2000AWV Rosario", "Santa Fe", "ARGENTINA"],
     _a(strt_nm="Calle Cordoba", bldg_nb="1452", pst_cd="S2000AWV", twn_nm="Rosario",
        ctry_sub_dvsn="Santa Fe", ctry="AR"), "repair", "AR format"),
    # United Kingdom: number street / town postcode / country
    ("GB-1", "GB", ["221B Baker Street", "London NW1 6XE", "UNITED KINGDOM"],
     _a(bldg_nb="221B", strt_nm="Baker Street", twn_nm="London", pst_cd="NW1 6XE", ctry="GB"),
     "repair", "GB format"),
    ("GB-2", "GB", ["10 Deansgate", "Manchester M3 2BW", "UNITED KINGDOM"],
     _a(bldg_nb="10", strt_nm="Deansgate", twn_nm="Manchester", pst_cd="M3 2BW", ctry="GB"),
     "repair", "GB format"),
    ("GB-3", "GB", ["45 Princes Street", "Edinburgh EH2 2BY", "UNITED KINGDOM"],
     _a(bldg_nb="45", strt_nm="Princes Street", twn_nm="Edinburgh", pst_cd="EH2 2BY", ctry="GB"),
     "repair", "GB format"),
    # Germany: street number / PLZ town / country
    ("DE-1", "DE", ["Hauptstrasse 5", "10115 Berlin", "DEUTSCHLAND"],
     _a(strt_nm="Hauptstrasse", bldg_nb="5", pst_cd="10115", twn_nm="Berlin", ctry="DE"), "repair", "DE format"),
    ("DE-2", "DE", ["Koenigsallee 92", "40212 Duesseldorf", "DEUTSCHLAND"],
     _a(strt_nm="Koenigsallee", bldg_nb="92", pst_cd="40212", twn_nm="Duesseldorf", ctry="DE"), "repair", "DE format"),
    ("DE-3", "DE", ["Zeil 106", "60313 Frankfurt am Main", "DEUTSCHLAND"],
     _a(strt_nm="Zeil", bldg_nb="106", pst_cd="60313", twn_nm="Frankfurt am Main", ctry="DE"), "repair", "DE format"),
    # Hybrid: a line the rules can't place stays as an address line
    ("MX-HYB", "MX", ["Av. Insurgentes Sur 1602", "Torre Azul Piso 12", "Col. Credito Constructor",
                      "C.P. 03940 Ciudad de Mexico, CDMX", "MEXICO"],
     _a(strt_nm="Av. Insurgentes Sur", bldg_nb="1602", dstrct_nm="Credito Constructor", pst_cd="03940",
        twn_nm="Ciudad de Mexico", ctry_sub_dvsn="CDMX", ctry="MX", adr_lines=["Torre Azul Piso 12"]),
     "repair", "hybrid: building line kept as an address line"),
    ("BR-HYB", "BR", ["Rua Haddock Lobo, 595 - Cerqueira Cesar", "Conjunto 42", "Sao Paulo - SP",
                      "CEP 01414-001", "BRASIL"],
     _a(strt_nm="Rua Haddock Lobo", bldg_nb="595", dstrct_nm="Cerqueira Cesar", twn_nm="Sao Paulo",
        ctry_sub_dvsn="SP", pst_cd="01414-001", ctry="BR", adr_lines=["Conjunto 42"]),
     "repair", "hybrid: suite line kept as an address line"),
    ("DE-HYB", "DE", ["c/o Mueller Logistik GmbH", "Hafenstrasse 21", "20359 Hamburg", "DEUTSCHLAND"],
     _a(strt_nm="Hafenstrasse", bldg_nb="21", pst_cd="20359", twn_nm="Hamburg", ctry="DE",
        adr_lines=["c/o Mueller Logistik GmbH"]),
     "repair", "hybrid: care-of line kept as an address line"),
    # A person decides
    ("CONFLICT", "MX", ["Calle 93 # 11-26", "Bogota, Cundinamarca", "110221", "COLOMBIA"], None, "escalate",
     "Address says Colombia, the account and bank are Mexican: a wrong country breaks sanctions screening."),
    ("NO-TOWN", "GB", ["221B Baker Street", "UNITED KINGDOM"], None, "escalate",
     "No town anywhere in the address: CBPR+ needs Town Name and nothing may be invented."),
    ("PE-NORULES", "PE", ["Av. Larco 1301", "Miraflores", "Lima 15074", "PERU"],
     _a(strt_nm="Av. Larco", bldg_nb="1301", twn_nm="Lima", pst_cd="15074", ctry="PE"),
     "repair", "Peru has no rules yet: the rules send it to a person; a model may structure it."),
]

ADDRESS_HOLDOUT: list[Spec] = [
    ("MX-NOCOL", "MX", ["Calle Hamburgo 172", "Colonia Juarez", "CP 06600 Ciudad de Mexico, CDMX", "MEXICO"],
     _a(strt_nm="Calle Hamburgo", bldg_nb="172", dstrct_nm="Juarez", pst_cd="06600",
        twn_nm="Ciudad de Mexico", ctry_sub_dvsn="CDMX", ctry="MX"), "repair", "'Colonia' and 'CP' spelled differently"),
    ("MX-PCLAST", "MX", ["Av. Constitucion 300", "Monterrey, N.L. 64000", "MEXICO"],
     _a(strt_nm="Av. Constitucion", bldg_nb="300", twn_nm="Monterrey", ctry_sub_dvsn="N.L.",
        pst_cd="64000", ctry="MX"), "repair", "postcode after the town"),
    ("BR-NOHYPHEN", "BR", ["Av. Paulista, 1578 - Bela Vista", "Sao Paulo/SP", "CEP: 01310200", "BRAZIL"],
     _a(strt_nm="Av. Paulista", bldg_nb="1578", dstrct_nm="Bela Vista", twn_nm="Sao Paulo",
        ctry_sub_dvsn="SP", pst_cd="01310200", ctry="BR"), "repair", "CEP without hyphen, town/UF with a slash"),
    ("BR-LOJA", "BR", ["Rua Oscar Freire, 379 - Jardins", "Loja 12", "Sao Paulo - SP", "01426-001", "BRASIL"],
     _a(strt_nm="Rua Oscar Freire", bldg_nb="379", dstrct_nm="Jardins", twn_nm="Sao Paulo",
        ctry_sub_dvsn="SP", pst_cd="01426-001", ctry="BR", adr_lines=["Loja 12"]), "repair", "shop line, bare CEP"),
    ("CO-ABBR", "CO", ["Cra. 7 # 71-21", "Bogota D.C.", "110231", "COLOMBIA"],
     _a(strt_nm="Cra. 7", bldg_nb="71-21", twn_nm="Bogota D.C.", pst_cd="110231", ctry="CO"),
     "repair", "abbreviated 'Cra.', town without department"),
    ("AR-OLDPC", "AR", ["Calle Florida 537", "1005 Buenos Aires", "ARGENTINA"],
     _a(strt_nm="Calle Florida", bldg_nb="537", pst_cd="1005", twn_nm="Buenos Aires", ctry="AR"),
     "repair", "old 4-digit postcode"),
    ("GB-FLAT", "GB", ["Flat 3", "12 Abbey Road", "London NW8 9AY", "UK"],
     _a(bldg_nb="12", strt_nm="Abbey Road", twn_nm="London", pst_cd="NW8 9AY", ctry="GB", adr_lines=["Flat 3"]),
     "repair", "flat line first, 'UK'"),
    ("DE-UMLAUT", "DE", ["Königsallee 60", "40212 Düsseldorf", "GERMANY"],
     _a(strt_nm="Königsallee", bldg_nb="60", pst_cd="40212", twn_nm="Düsseldorf", ctry="DE"),
     "repair", "umlauts, English country name"),
    ("DE-NOCTRY", "DE", ["Bahnhofstrasse 12", "80335 Muenchen"],
     _a(strt_nm="Bahnhofstrasse", bldg_nb="12", pst_cd="80335", twn_nm="Muenchen", ctry="DE"),
     "repair", "no country line: country from the IBAN and the BIC"),
    ("CL-NORULES", "CL", ["Av. Apoquindo 3000, Of. 501", "Las Condes", "Santiago", "CHILE"],
     _a(strt_nm="Av. Apoquindo", bldg_nb="3000", twn_nm="Santiago", ctry="CL"),
     "repair", "Chile: no rules; office number and comuna"),
    ("PE-NORULES", "PE", ["Jr. de la Union 456", "Cercado de Lima", "Lima 15001", "PERU"],
     _a(strt_nm="Jr. de la Union", bldg_nb="456", twn_nm="Lima", pst_cd="15001", ctry="PE"),
     "repair", "Peru: no rules"),
    ("CR-SENAS", "CR", ["Del Parque Central de Heredia 300 metros sur", "Heredia, Heredia", "COSTA RICA"],
     _a(twn_nm="Heredia", ctry="CR"), "repair",
     "Costa Rica: directions instead of a street address; only town and country are knowable"),
    ("CONFLICT", "BR", ["Av. Corrientes 1234", "C1043AAZ Buenos Aires", "ARGENTINA"], None, "escalate",
     "Argentine address on a Brazilian account and bank: a person decides"),
]

DEMO_ADDRESSES: list[Spec] = [
    ("DEMO-12-ADDR", "BR", ["Rua Augusta, 2690 - Jardins", "Sao Paulo - SP", "CEP 01412-100", "BRASIL"],
     _a(strt_nm="Rua Augusta", bldg_nb="2690", dstrct_nm="Jardins", twn_nm="Sao Paulo",
        ctry_sub_dvsn="SP", pst_cd="01412-100", ctry="BR"), "repair", "BR rules structure it fully"),
    ("DEMO-13-ADDR", "CL", ["Av. Libertador Bernardo O'Higgins 1449, Of. 302", "Santiago Centro", "Santiago", "CHILE"],
     _a(strt_nm="Av. Libertador Bernardo O'Higgins", bldg_nb="1449", twn_nm="Santiago", ctry="CL"),
     "repair", "no Chile rules: the rules send it to a person; the model proposes, the checks verify"),
]


def _unstructure(pacs008: bytes, lines: list[str]) -> bytes:
    """Replace the creditor's PstlAdr with free-text address lines only."""
    root = etree.fromstring(pacs008)
    ns = {"n": etree.QName(root).namespace}
    cdtr = root.find(".//n:CdtTrfTxInf/n:Cdtr", ns)
    old = cdtr.find("n:PstlAdr", ns)
    adr = etree.Element(f"{{{ns['n']}}}PstlAdr")
    for l in lines:
        etree.SubElement(adr, f"{{{ns['n']}}}AdrLine").text = l
    if old is not None:
        cdtr.replace(old, adr)
    else:
        cdtr.insert(1, adr)
    return etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8")


def build_case(seed: int, case_id: str, set_: str, spec: Spec, d: Directory) -> dict[str, bytes]:
    _, ctry, lines, expected, action, why = spec
    rng = random.Random(f"address:{seed}:{case_id}")
    p = build_payment(rng, d)
    bank = next(b for b in d.banks if b["bic"][:4] == BANK[ctry])
    p.creditor_agent = d.agent(bank["bic"])
    p.creditor, p.creditor_account = make_beneficiary(rng, ctry, BANK[ctry])
    p8 = _unstructure(to_xml(p), lines)
    if errs := validate(p8, "pacs.008.001.08"):
        raise ValueError(f"{case_id}: pacs.008 invalid: {errs[:2]}")
    label = Label(case_id=case_id, set=set_, lane="C", code="ADDR", action=action,
                  allowed_fields=["Cdtr.PstlAdr"], expected_value=expected, rationale=why)
    return {"pacs008.xml": p8,
            "label.json": (json.dumps(label.model_dump(), indent=2, sort_keys=True) + "\n").encode()}


def build_addresses(seed: int, out: Path, holdout: bool = False) -> list[str]:
    specs, prefix, set_ = ((ADDRESS_HOLDOUT, "H-C-", "address-holdout") if holdout
                           else (ADDRESS_EVAL, "C-", "address"))
    d = Directory()
    if out.exists():
        for child in out.iterdir():
            if child.is_dir() and (child / "label.json").exists():
                shutil.rmtree(child)
    out.mkdir(parents=True, exist_ok=True)
    ids = []
    for spec in specs:
        cid = prefix + spec[0]
        (out / cid).mkdir()
        for name, data in build_case(seed, cid, set_, spec, d).items():
            (out / cid / name).write_bytes(data)
        ids.append(cid)
    return ids


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate Lane C address cases")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path("cases_address"))
    ap.add_argument("--holdout", action="store_true", help="write the hand-written held-out set")
    a = ap.parse_args()
    ids = build_addresses(a.seed, a.out, a.holdout)
    print(f"wrote {len(ids)} cases to {a.out}")


if __name__ == "__main__":
    main()
