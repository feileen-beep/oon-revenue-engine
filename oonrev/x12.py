"""Read and write X12 835 (health care claim payment / advice) files.

The parser covers the segments a provider needs to post payments and analyze
remits: BPR (payment), TRN (trace / check number), N1 (payer and payee),
CLP (claim payment), CAS (adjustments, claim or service level), NM1*QC
(patient), REF*CE (class of contract, used for repricing such as MultiPlan),
DTM*232/233 (statement period), SVC (service line) and AMT*B6 (allowed).
Delimiters are read from the ISA header, so files from any payer work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

# Claim status codes (CLP02).
STATUS_PRIMARY = "1"
STATUS_SECONDARY = "2"
STATUS_TERTIARY = "3"
STATUS_DENIED = "4"
STATUS_REVERSAL = "22"


@dataclass
class Adjustment:
    group: str   # CO contractual, PR patient responsibility, OA other, PI payer initiated
    reason: str  # CARC
    amount: Decimal


@dataclass
class ServiceLine:
    qualifier: str
    code: str
    billed: Decimal
    paid: Decimal
    units: Decimal
    allowed: Decimal | None = None
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class ClaimPayment:
    source_file: str
    payer: str
    payer_id: str
    trace_number: str
    payment_date: date
    claim_id: str
    status: str
    billed: Decimal
    paid: Decimal
    patient_responsibility: Decimal
    payer_claim_control: str
    member_id: str = ""
    patient_last: str = ""
    service_from: date | None = None
    service_to: date | None = None
    contract_class: str = ""
    adjustments: list[Adjustment] = field(default_factory=list)
    lines: list[ServiceLine] = field(default_factory=list)

    def all_adjustments(self) -> list[Adjustment]:
        out = list(self.adjustments)
        for line in self.lines:
            out.extend(line.adjustments)
        return out

    @property
    def allowed(self) -> Decimal | None:
        vals = [ln.allowed for ln in self.lines if ln.allowed is not None]
        return sum(vals, Decimal("0")) if vals else None

    def adjustment_total(self) -> Decimal:
        return sum((a.amount for a in self.all_adjustments()), Decimal("0"))

    def is_balanced(self) -> bool:
        """Billed charges should equal paid plus every adjustment."""
        return abs(self.billed - self.paid - self.adjustment_total()) < Decimal("0.01")


def _d(text: str) -> Decimal:
    return Decimal(text) if text else Decimal("0")


def _date8(text: str) -> date:
    return datetime.strptime(text, "%Y%m%d").date()


def _el(seg: list[str], idx: int) -> str:
    return seg[idx] if idx < len(seg) else ""


def split_segments(raw: str) -> tuple[list[list[str]], str]:
    """Split an X12 interchange into segments, using the ISA delimiters."""
    raw = raw.lstrip("﻿").lstrip()
    if not raw.startswith("ISA"):
        raise ValueError("not an X12 file: missing ISA header")
    element_sep = raw[3]
    component_sep = raw[104]
    segment_term = raw[105]
    segments = []
    for chunk in raw.split(segment_term):
        chunk = chunk.strip("\r\n ")
        if chunk:
            segments.append(chunk.split(element_sep))
    return segments, component_sep


def parse_835(raw: str, source_file: str = "") -> list[ClaimPayment]:
    segments, comp = split_segments(raw)
    out: list[ClaimPayment] = []
    payer = payer_id = trace = ""
    pay_date: date | None = None
    claim: ClaimPayment | None = None
    line: ServiceLine | None = None
    n1_role = ""

    for seg in segments:
        tag = seg[0]
        if tag == "ST":
            payer = payer_id = trace = ""
            pay_date, claim, line = None, None, None
        elif tag == "BPR":
            pay_date = _date8(_el(seg, 16)) if _el(seg, 16) else None
        elif tag == "TRN":
            trace = _el(seg, 2)
        elif tag == "DTM" and _el(seg, 1) == "405" and pay_date is None:
            pay_date = _date8(_el(seg, 2))
        elif tag == "N1":
            n1_role = _el(seg, 1)
            if n1_role == "PR":
                payer = _el(seg, 2)
        elif tag == "REF" and claim is None and n1_role == "PR" and _el(seg, 1) == "2U":
            payer_id = _el(seg, 2)
        elif tag == "CLP":
            line = None
            claim = ClaimPayment(
                source_file=source_file,
                payer=payer,
                payer_id=payer_id,
                trace_number=trace,
                payment_date=pay_date,
                claim_id=_el(seg, 1),
                status=_el(seg, 2),
                billed=_d(_el(seg, 3)),
                paid=_d(_el(seg, 4)),
                patient_responsibility=_d(_el(seg, 5)),
                payer_claim_control=_el(seg, 7),
            )
            out.append(claim)
        elif claim is None:
            continue
        elif tag == "CAS":
            group = _el(seg, 1)
            # Up to six reason / amount / quantity triplets per CAS segment.
            for i in range(2, len(seg), 3):
                reason, amount = _el(seg, i), _el(seg, i + 1)
                if reason:
                    adj = Adjustment(group, reason, _d(amount))
                    (line.adjustments if line else claim.adjustments).append(adj)
        elif tag == "NM1" and _el(seg, 1) == "QC":
            claim.patient_last = _el(seg, 3)
            claim.member_id = _el(seg, 9)
        elif tag == "REF" and _el(seg, 1) == "CE" and line is None:
            claim.contract_class = _el(seg, 2)
        elif tag == "DTM" and line is None and _el(seg, 1) in ("232", "233"):
            d = _date8(_el(seg, 2))
            if _el(seg, 1) == "232":
                claim.service_from = d
            else:
                claim.service_to = d
        elif tag == "SVC":
            qual, _, code = _el(seg, 1).partition(comp)
            line = ServiceLine(
                qualifier=qual,
                code=code,
                billed=_d(_el(seg, 2)),
                paid=_d(_el(seg, 3)),
                units=_d(_el(seg, 5)) if _el(seg, 5) else Decimal("1"),
            )
            claim.lines.append(line)
        elif tag == "AMT" and line is not None and _el(seg, 1) == "B6":
            line.allowed = _d(_el(seg, 2))
        elif tag == "SE":
            claim, line = None, None
    return out


def read_835_dir(folder: Path) -> list[ClaimPayment]:
    out = []
    for path in sorted(Path(folder).glob("*.835")):
        out.extend(parse_835(path.read_text(), path.name))
    return out


# ---------------------------------------------------------------- writing


def _money(x) -> str:
    return f"{Decimal(x).quantize(Decimal('0.01'))}"


def build_835(
    *,
    payer: str,
    payer_id: str,
    payee: str,
    payee_npi: str,
    payment_date: date,
    trace_number: str,
    control_number: int,
    claims: list[dict],
) -> str:
    """Build one 835 interchange holding one payment (BPR) and its claims.

    Each claim dict has: claim_id, status, billed, paid, patient_resp,
    payer_ctl, member_id, patient_last, service_from, service_to, svc_qual,
    svc_code, units, allowed, adjustments [(group, reason, amount)], and an
    optional contract_class.
    """
    d8 = payment_date.strftime("%Y%m%d")
    total = sum((Decimal(str(c["paid"])) for c in claims), Decimal("0"))
    isa = (
        f"ISA*00*          *00*          *ZZ*{payer_id:<15}*ZZ*{'DRCPROVIDER':<15}*"
        f"{payment_date.strftime('%y%m%d')}*1200*^*00501*{control_number:09d}*0*P*:"
    )
    assert len(isa) == 105, len(isa)
    segs = [
        isa,
        f"GS*HP*{payer_id}*DRCPROVIDER*{d8}*1200*{control_number}*X*005010X221A1",
        f"ST*835*{control_number % 10000:04d}",
    ]
    if total > 0:
        segs.append(
            f"BPR*I*{_money(total)}*C*ACH*CCP*01*999999999*DA*123456789*{payer_id}**"
            f"01*111000025*DA*987654321*{d8}"
        )
    else:
        segs.append(f"BPR*H*0*C*NON************{d8}")
    segs += [
        f"TRN*1*{trace_number}*1{payer_id}",
        f"DTM*405*{d8}",
        f"N1*PR*{payer}",
        f"REF*2U*{payer_id}",
        f"N1*PE*{payee}*XX*{payee_npi}",
        "LX*1",
    ]
    for c in claims:
        segs.append(
            f"CLP*{c['claim_id']}*{c['status']}*{_money(c['billed'])}*{_money(c['paid'])}*"
            f"{_money(c['patient_resp'])}*12*{c['payer_ctl']}"
        )
        segs.append(f"NM1*QC*1*{c['patient_last']}*****MI*{c['member_id']}")
        if c.get("contract_class"):
            segs.append(f"REF*CE*{c['contract_class']}")
        segs.append(f"DTM*232*{c['service_from'].strftime('%Y%m%d')}")
        segs.append(f"DTM*233*{c['service_to'].strftime('%Y%m%d')}")
        segs.append(
            f"SVC*{c['svc_qual']}:{c['svc_code']}*{_money(c['billed'])}*{_money(c['paid'])}**{c['units']}"
        )
        by_group: dict[str, list] = {}
        for group, reason, amount in c["adjustments"]:
            by_group.setdefault(group, []).append((reason, amount))
        for group, items in by_group.items():
            body = "*".join(f"{r}*{_money(a)}*" for r, a in items).rstrip("*")
            segs.append(f"CAS*{group}*{body}")
        if c.get("allowed") is not None:
            segs.append(f"AMT*B6*{_money(c['allowed'])}")
    seg_count = len(segs) - 2 + 1  # ST through SE inclusive
    segs.append(f"SE*{seg_count}*{control_number % 10000:04d}")
    segs.append(f"GE*1*{control_number}")
    segs.append(f"IEA*1*{control_number:09d}")
    return "~\n".join(segs) + "~\n"
