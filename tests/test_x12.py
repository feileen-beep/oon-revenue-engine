from datetime import date
from decimal import Decimal

from oonrev.x12 import build_835, parse_835

CLAIM = {
    "claim_id": "DRC1", "status": "1", "billed": "24500.00", "paid": "6000.00", "patient_resp": "2000.00",
    "payer_ctl": "X1", "member_id": "M1", "patient_last": "PT1", "service_from": date(2026, 1, 1),
    "service_to": date(2026, 1, 7), "svc_qual": "NU", "svc_code": "1002", "units": 7, "allowed": "8000.00",
    "adjustments": [("CO", "45", 16500), ("PR", "1", 1500), ("PR", "2", 500)], "contract_class": "MULTIPLAN",
}


def make(claims):
    return build_835(payer="Aetna", payer_id="60054", payee="DRC", payee_npi="1999999999",
                     payment_date=date(2026, 2, 6), trace_number="T1", control_number=7, claims=claims)


def test_round_trip():
    [c] = parse_835(make([CLAIM]))
    assert c.claim_id == "DRC1" and c.payer == "Aetna" and c.payer_id == "60054"
    assert c.payment_date == date(2026, 2, 6)
    assert c.paid == Decimal("6000.00") and c.allowed == Decimal("8000.00")
    assert c.contract_class == "MULTIPLAN"
    assert c.service_from == date(2026, 1, 1) and c.service_to == date(2026, 1, 7)
    assert [(a.group, a.reason) for a in c.all_adjustments()] == [("CO", "45"), ("PR", "1"), ("PR", "2")]
    assert c.is_balanced()


def test_reads_delimiters_from_isa_header():
    raw = make([CLAIM]).replace("*", "|").replace("~", "\\")
    assert parse_835(raw)[0].paid == Decimal("6000.00")


def test_zero_payment_file():
    denied = dict(CLAIM, status="4", paid="0.00", patient_resp="0.00", allowed=None,
                  adjustments=[("CO", "50", 24500)], contract_class="")
    [c] = parse_835(make([denied]))
    assert c.status == "4" and c.payment_date == date(2026, 2, 6) and c.is_balanced()
