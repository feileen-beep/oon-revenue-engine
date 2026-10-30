"""Load claims, remits and patient payments, match them, and track each claim.

Matching runs in three passes, from strongest to weakest key:

1. the claim number on the remit (CLP01) equals the claim number we billed
2. the claim number matches once formatting is normalized (payers often drop
   prefixes or leading zeros)
3. member ID + service dates + billed amount

Anything left over goes to an exceptions list for the revenue cycle team.
"""

from __future__ import annotations

import csv
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .reference import DENIAL_CARCS, PAID_TO_PATIENT_CARC
from .x12 import STATUS_DENIED, STATUS_REVERSAL, ClaimPayment, read_835_dir

WRITE_OFF_AGE_DAYS = 365


@dataclass
class Event:
    date: date
    kind: str            # paid | denied | paid_to_patient | reversal | patient_payment
    cash: float = 0.0    # cash received by the provider
    p2p_amount: float = 0.0
    carc: str = ""
    allowed: float | None = None
    contract_class: str = ""
    source: str = ""


@dataclass
class Claim:
    claim_id: str
    patient_id: str
    member_id: str
    payer: str
    loc: str
    site: str
    service_from: date
    service_to: date
    units: int
    billed: float
    submit_date: date
    sca_id: str = ""
    sca_per_diem: float = 0.0
    events: list[Event] = field(default_factory=list)

    @property
    def service_month(self) -> date:
        return self.service_to.replace(day=1)

    def collected(self, start: date | None, end: date) -> float:
        """Cash received after `start` (exclusive) and on or before `end`."""
        dates, cum = self._cash_index()
        total = cum[bisect_right(dates, end)]
        return total - cum[bisect_right(dates, start)] if start is not None else total

    def _cash_index(self):
        # Cumulative cash by date, rebuilt only if events were added since the last call.
        cached = self.__dict__.get("_cash_cache")
        if cached is None or cached[0] != len(self.events):
            evs = sorted((e for e in self.events if e.cash), key=lambda e: e.date)
            cum = [0.0]
            for e in evs:
                cum.append(cum[-1] + e.cash)
            cached = (len(self.events), [e.date for e in evs], cum)
            self.__dict__["_cash_cache"] = cached
        return cached[1], cached[2]

    def first_remit(self) -> Event | None:
        for e in self.events:
            if e.kind in ("paid", "denied", "paid_to_patient"):
                return e
        return None

    def state(self, as_of: date) -> "ClaimState":
        status = "pending"
        collected = 0.0
        p2p_due = 0.0
        carc = ""
        denial_date = None
        final_date = None
        last_paid: Event | None = None
        for e in self.events:
            if e.date > as_of:
                break
            collected += e.cash
            if e.kind == "paid":
                status, last_paid = "paid", e
                final_date = e.date
            elif e.kind == "denied" and status != "paid":
                status, carc = "denied", e.carc
                denial_date = denial_date or e.date
            elif e.kind == "paid_to_patient":
                status, p2p_due = "paid_to_patient", p2p_due + e.p2p_amount
            elif e.kind == "patient_payment":
                p2p_due -= e.cash
                if status == "paid_to_patient" and p2p_due <= 0.01:
                    status, final_date = "patient_collected", e.date
            elif e.kind == "reversal":
                status, final_date = "pending", None
        finalized = status in ("paid", "patient_collected")
        age = (as_of - self.service_to).days
        if not finalized and age > WRITE_OFF_AGE_DAYS:
            finalized = True
            final_date = self.service_to + timedelta(days=WRITE_OFF_AGE_DAYS + 1)
            status = "written_off"
        return ClaimState(status, collected, max(p2p_due, 0.0), carc, finalized, final_date, age, last_paid,
                          denial_date)


@dataclass
class ClaimState:
    status: str
    collected: float
    p2p_outstanding: float
    denial_carc: str
    finalized: bool
    final_date: date | None
    age_days: int
    last_paid: Event | None
    denial_date: date | None = None


@dataclass
class Ledger:
    claims: dict[str, Claim]
    remits: list[ClaimPayment]
    match_log: list[dict]
    unmatched: list[ClaimPayment]
    sca_contracts: list[dict]

    def as_of(self, when: date) -> list[Claim]:
        return [c for c in self.claims.values() if c.service_to <= when]


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _norm_id(s: str) -> str:
    digits = re.sub(r"\D", "", s or "")
    return digits.lstrip("0")


def load_claims(path: Path) -> dict[str, Claim]:
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out[r["claim_id"]] = Claim(
                claim_id=r["claim_id"],
                patient_id=r["patient_id"],
                member_id=r["member_id"],
                payer=r["payer"],
                loc=r["level_of_care"],
                site=r["site"],
                service_from=_date(r["service_from"]),
                service_to=_date(r["service_to"]),
                units=int(r["units"]),
                billed=float(r["billed"]),
                submit_date=_date(r["submit_date"]),
                sca_id=r.get("sca_id", ""),
                sca_per_diem=float(r["sca_per_diem"]) if r.get("sca_per_diem") else 0.0,
            )
    return out


def classify_remit(rp: ClaimPayment) -> Event:
    adjustments = rp.all_adjustments()
    allowed = float(rp.allowed) if rp.allowed is not None else None
    base = dict(date=rp.payment_date, allowed=allowed, contract_class=rp.contract_class,
                source=f"{rp.source_file}#{rp.payer_claim_control}")
    if rp.status == STATUS_REVERSAL:
        return Event(kind="reversal", cash=float(rp.paid), **base)
    if rp.paid > 0:
        return Event(kind="paid", cash=float(rp.paid), **base)
    p2p = sum(float(a.amount) for a in adjustments if a.reason == PAID_TO_PATIENT_CARC)
    if p2p > 0:
        return Event(kind="paid_to_patient", p2p_amount=p2p, **base)
    denial = [a for a in adjustments if a.reason in DENIAL_CARCS]
    carc = max(denial, key=lambda a: a.amount).reason if denial else ""
    if rp.status == STATUS_DENIED or carc:
        return Event(kind="denied", carc=carc, **base)
    # Processed with nothing paid and no denial code: usually all patient responsibility.
    return Event(kind="paid", cash=0.0, **base)


def match_remits(claims: dict[str, Claim], remits: list[ClaimPayment]):
    by_norm = {_norm_id(cid): c for cid, c in claims.items()}
    by_detail = {(c.member_id, c.service_from, c.service_to, round(c.billed, 2)): c for c in claims.values()}
    log, unmatched = [], []
    for rp in remits:
        claim, method = claims.get(rp.claim_id), "claim number"
        if claim is None:
            claim, method = by_norm.get(_norm_id(rp.claim_id)), "normalized claim number"
        if claim is None:
            key = (rp.member_id, rp.service_from, rp.service_to, round(float(rp.billed), 2))
            claim, method = by_detail.get(key), "member + dates + amount"
        if claim is None:
            unmatched.append(rp)
            continue
        if method != "claim number":
            log.append({"remit_claim_number": rp.claim_id, "matched_to": claim.claim_id,
                        "method": method, "payer": rp.payer, "payment_date": rp.payment_date.isoformat(),
                        "paid": float(rp.paid)})
        claim.events.append(classify_remit(rp))
    return log, unmatched


def load_patient_payments(path: Path, claims: dict[str, Claim]):
    if not Path(path).exists():
        return
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            c = claims.get(r["claim_id"])
            if c:
                c.events.append(Event(date=_date(r["date"]), kind="patient_payment",
                                      cash=float(r["amount"]), source=r["payment_id"]))


def load(data_dir: Path) -> Ledger:
    data_dir = Path(data_dir)
    claims = load_claims(data_dir / "claims.csv")
    remits = read_835_dir(data_dir / "era")
    log, unmatched = match_remits(claims, remits)
    load_patient_payments(data_dir / "patient_payments.csv", claims)
    for c in claims.values():
        c.events.sort(key=lambda e: (e.date, e.kind != "denied"))
    sca = []
    if (data_dir / "sca_contracts.csv").exists():
        with open(data_dir / "sca_contracts.csv", newline="") as f:
            sca = list(csv.DictReader(f))
        link_sca(claims, sca)
    return Ledger(claims, remits, log, unmatched, sca)


def link_sca(claims: dict[str, Claim], sca: list[dict]):
    """Fill in each claim's SCA rate from the agreement list.

    A claim is covered when it names the agreement, or when the agreement is for
    the same patient and level of care and was in effect on the service date.
    """
    by_id = {r["sca_id"]: r for r in sca if r.get("sca_id")}
    by_patient: dict[tuple, list] = {}
    for r in sca:
        by_patient.setdefault((r.get("patient_id", ""), r.get("level_of_care", "")), []).append(r)
    for c in claims.values():
        if c.sca_per_diem:
            continue
        match = by_id.get(c.sca_id) if c.sca_id else None
        if match is None:
            for r in by_patient.get((c.patient_id, c.loc), []):
                start = r.get("effective_from")
                if not start or date.fromisoformat(start) <= c.service_to:
                    match = r
        if match and match.get("per_diem"):
            c.sca_id = match["sca_id"]
            c.sca_per_diem = float(match["per_diem"])
