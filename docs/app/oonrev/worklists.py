"""Work queues for the revenue cycle team, built from the same close data."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import timedelta

from .close import CloseResult
from .reference import CARC

APPEAL_WINDOW_DAYS = 180
SCA_TOLERANCE = 0.98         # flag SCA claims allowed below 98% of the contract rate
BENCHMARK_FLOOR = 0.70       # flag other claims allowed below 70% of the payer's median
MIN_VARIANCE = 500.0


def _paid_events(res: CloseResult, start, end):
    for v in res.now.values():
        for e in v.claim.events:
            if e.kind == "paid" and start < e.date <= end and e.allowed:
                yield v, e


def allowed_benchmarks(res: CloseResult) -> dict[tuple, float]:
    """Median allowed amount per unit, by payer x level of care, trailing 12 months.

    Excludes SCA and MultiPlan-repriced claims so the benchmark reflects what
    the payer allows on its own out-of-network schedule.
    """
    per_unit = defaultdict(list)
    for v, e in _paid_events(res, res.as_of - timedelta(days=365), res.as_of):
        c = v.claim
        if c.sca_id or e.contract_class == "MULTIPLAN":
            continue
        per_unit[(c.payer, c.loc)].append(e.allowed / c.units)
    return {k: statistics.median(vals) for k, vals in per_unit.items() if len(vals) >= 10}


def underpayments(res: CloseResult, lookback_days: int = 90) -> list[dict]:
    bench = allowed_benchmarks(res)
    rows = []
    for v, e in _paid_events(res, res.as_of - timedelta(days=lookback_days), res.as_of):
        c = v.claim
        if c.sca_id:
            expected = c.sca_per_diem * c.units
            if e.allowed >= expected * SCA_TOLERANCE:
                continue
            reason = f"Allowed below single case agreement {c.sca_id} rate"
            action = "Send SCA and EOB to payer; request reprocessing at contract rate"
        else:
            b = bench.get((c.payer, c.loc))
            if not b:
                continue
            expected = b * c.units
            if e.allowed >= expected * BENCHMARK_FLOOR:
                continue
            if e.contract_class == "MULTIPLAN":
                reason = "Repriced through MultiPlan well below this payer's usual allowed"
                action = "Check for a signed MultiPlan agreement; if none, dispute the repricing"
            else:
                reason = "Allowed far below this payer's usual allowed amount"
                action = "Request reconsideration with payment history for this payer"
        variance = expected - e.allowed
        if variance < MIN_VARIANCE:
            continue
        pay_ratio = e.cash / e.allowed if e.allowed else 0.0
        rows.append({
            "claim_id": c.claim_id, "payer": c.payer, "loc": c.loc, "site": c.site,
            "service_from": c.service_from.isoformat(), "service_to": c.service_to.isoformat(),
            "paid_date": e.date.isoformat(), "billed": c.billed, "allowed": e.allowed, "paid": e.cash,
            "expected_allowed": expected, "allowed_variance": variance,
            "est_recovery": variance * pay_ratio, "repricer": e.contract_class if e.contract_class == "MULTIPLAN" else "",
            "reason": reason, "action": action,
        })
    return sorted(rows, key=lambda r: -r["est_recovery"])


def open_denials(res: CloseResult) -> list[dict]:
    rows = []
    for v in res.now.values():
        if v.status != "denied":
            continue
        c = v.claim
        st = c.state(res.as_of)
        deadline = st.denial_date + timedelta(days=APPEAL_WINDOW_DAYS)
        rows.append({
            "claim_id": c.claim_id, "payer": c.payer, "loc": c.loc, "site": c.site,
            "service_to": c.service_to.isoformat(), "denied_on": st.denial_date.isoformat(),
            "carc": st.denial_carc, "reason": CARC.get(st.denial_carc, st.denial_carc),
            "billed": c.billed, "expected_recovery": v.remaining,
            "appeal_deadline": deadline.isoformat(), "days_left": (deadline - res.as_of).days,
        })
    return sorted(rows, key=lambda r: (r["days_left"] < 0, r["days_left"], -r["billed"]))


def paid_to_patient(res: CloseResult) -> list[dict]:
    rows = []
    for v in res.now.values():
        if v.status != "paid_to_patient":
            continue
        c = v.claim
        st = c.state(res.as_of)
        remit = next(e for e in c.events if e.kind == "paid_to_patient")
        rows.append({
            "claim_id": c.claim_id, "patient_id": c.patient_id, "payer": c.payer, "loc": c.loc,
            "service_to": c.service_to.isoformat(), "payer_paid_patient_on": remit.date.isoformat(),
            "amount_sent_to_patient": remit.p2p_amount, "still_owed": st.p2p_outstanding,
            "days_outstanding": (res.as_of - remit.date).days, "expected_recovery": v.remaining,
        })
    return sorted(rows, key=lambda r: -r["still_owed"])


def denial_trend(res: CloseResult, months: int = 6) -> list[dict]:
    """Denial rate by payer and month of first remit, with the leading CARC."""
    start = (res.as_of.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    stats = defaultdict(lambda: {"remits": 0, "denied": 0, "denied_billed": 0.0, "carcs": defaultdict(float)})
    for v in res.now.values():
        first = v.claim.first_remit()
        if not first or first.date < start or first.date > res.as_of:
            continue
        s = stats[(v.claim.payer, first.date.strftime("%Y-%m"))]
        s["remits"] += 1
        if first.kind == "denied":
            s["denied"] += 1
            s["denied_billed"] += v.claim.billed
            s["carcs"][first.carc] += v.claim.billed
    rows = []
    for (payer, month), s in sorted(stats.items()):
        top = max(s["carcs"].items(), key=lambda kv: kv[1])[0] if s["carcs"] else ""
        rows.append({"payer": payer, "month": month, "remits": s["remits"], "denied": s["denied"],
                     "denial_rate": s["denied"] / s["remits"] if s["remits"] else 0.0,
                     "denied_billed": s["denied_billed"], "top_carc": top,
                     "top_reason": CARC.get(top, top)})
    return rows


def unmatched_remits(ledger) -> list[dict]:
    return [{"remit_claim_number": r.claim_id, "payer": r.payer, "payment_date": r.payment_date.isoformat(),
             "member_id": r.member_id, "billed": float(r.billed), "paid": float(r.paid), "file": r.source_file}
            for r in ledger.unmatched]
