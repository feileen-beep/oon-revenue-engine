from datetime import date

from oonrev import importers

EXPORT = """Claim ID,Patient Account,Insurance ID,Primary Insurance,From DOS,To DOS,Rev Code/CPT,Facility,Units,Total Charges
C100,P1,M1,Aetna,01/01/2026,01/07/2026,1002,Scottsdale,7,"$24,500.00"
C100,P1,M1,Aetna,01/01/2026,01/07/2026,1002,Scottsdale,0,$100.00
C101,P2,M2,Cigna,01/03/2026,,90837,Phoenix,1,$350.00
C102,P3,M3,Cigna,01/03/2026,,99999X,Phoenix,1,$350.00
"""


def test_guess_mapping_for_a_collaboratemd_style_export():
    headers, _ = importers.read_csv(EXPORT)
    m = importers.guess_mapping(headers, "claims")
    assert m["claim_id"] == "Claim ID" and m["payer"] == "Primary Insurance"
    assert m["service_from"] == "From DOS" and m["service_to"] == "To DOS"
    assert m["billed"] == "Total Charges" and m["level_of_care"] == "Rev Code/CPT"
    assert m["patient_id"] == "Patient Account" and m["member_id"] == "Insurance ID"


def test_normalize_rolls_up_lines_and_parses_formats():
    headers, _ = importers.read_csv(EXPORT)
    rows, problems = importers.normalize_claims(EXPORT, importers.guess_mapping(headers))
    by = {r["claim_id"]: r for r in rows}
    assert by["C100"]["billed"] == "24600.00" and by["C100"]["level_of_care"] == "RTC"
    assert by["C101"]["service_to"] == "2026-01-03" and by["C101"]["level_of_care"] == "OP"
    assert "C102" not in by and problems == ["1 rows skipped: level of care not mapped"]


def test_parsers():
    assert importers.parse_date("3/5/2026 12:00 AM") == date(2026, 3, 5)
    assert importers.parse_money("($1,234.50)") == -1234.5
    assert [importers.guess_loc(v) for v in ["Partial Hospitalization", "0906", "H0018", "90868", "IOP"]] == \
        ["PHP", "IOP", "RTC", "TMS", "IOP"]
