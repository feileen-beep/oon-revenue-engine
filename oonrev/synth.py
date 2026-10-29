"""Generate realistic synthetic claims, 835 remits and patient payments.

No real patient data is used anywhere in this project. Behavioral health and
substance use treatment records fall under HIPAA and 42 CFR Part 2, so the demo
runs on data this module invents. The generator models how out-of-network
commercial claims behave in practice:

* patients step down from residential (RTC) to PHP to IOP, billed weekly
* payers allow a share of billed charges, then apply deductible and coinsurance
* some claims are repriced through MultiPlan, some are paid under a single case
  agreement (SCA), some are denied (and some of those are overturned on appeal),
  and some payments are sent to the patient instead of the provider
* a few remits carry a mangled claim number, so matching has real exceptions

Two storylines are built in so the monthly memo has something to explain:
Aetna starts repricing most PHP claims through MultiPlan from May 2026, and
Cigna denies more claims for missing authorization from June 2026.
"""

from __future__ import annotations

import csv
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .reference import LOC_SERVICE_CODE
from .x12 import STATUS_DENIED, STATUS_PRIMARY, build_835

START = date(2024, 1, 1)
AS_OF = date(2026, 9, 30)

CHARGE_PER_UNIT = {"RTC": 3500, "PHP": 1800, "IOP": 900, "OP": 350, "TMS": 600}
SCA_PER_DIEM = {"RTC": 1850, "PHP": 950}

PAYERS = {
    # name: (payer_id, mix, allowed share of billed, deny base, MultiPlan share,
    #        paid-to-patient share, median days to pay, SCA share for RTC/PHP)
    "Aetna": ("60054", 0.22, 0.56, 0.08, 0.15, 0.03, 28, 0.12),
    "BCBS of Arizona": ("53589", 0.20, 0.49, 0.09, 0.08, 0.09, 35, 0.10),
    "Cigna": ("62308", 0.18, 0.53, 0.09, 0.20, 0.03, 32, 0.12),
    "UnitedHealthcare": ("87726", 0.20, 0.44, 0.10, 0.35, 0.03, 45, 0.05),
    "Anthem": ("47198", 0.10, 0.50, 0.08, 0.10, 0.13, 40, 0.05),
    "Other commercial": ("99999", 0.10, 0.46, 0.12, 0.12, 0.05, 50, 0.00),
}

LOC_DENY_FACTOR = {"RTC": 1.5, "PHP": 1.2, "IOP": 1.0, "OP": 0.6, "TMS": 1.3}

# CARC, weight, chance the denial is overturned on appeal.
DENIAL_MIX = [("50", 0.35, 0.30), ("197", 0.30, 0.25), ("16", 0.15, 0.70), ("27", 0.10, 0.02), ("96", 0.10, 0.05)]

SITE_FOR_LOC = {
    "RTC": ["Scottsdale", "Glendale"],
    "PHP": ["Phoenix", "Scottsdale"],
    "IOP": ["Phoenix", "Scottsdale", "Glendale"],
    "OP": ["Phoenix"],
    "TMS": ["Phoenix"],
}


@dataclass
class Patient:
    patient_id: str
    member_id: str
    payer: str
    coinsurance: float
    deductible_left: float


@dataclass
class Claim:
    claim_id: str
    patient: Patient
    loc: str
    site: str
    service_from: date
    service_to: date
    units: int
    billed: float
    submit_date: date
    sca_id: str = ""
    sca_per_diem: float = 0.0


class Generator:
    def __init__(self, seed: int = 7, start: date = START, as_of: date = AS_OF):
        self.rng = random.Random(seed)
        self.start = start
        self.as_of = as_of
        self.claims: list[Claim] = []
        self.remits: list[dict] = []      # one per claim adjudication, before batching
        self.patient_payments: list[dict] = []
        self.sca_contracts: list[dict] = []
        self._pid = 10000
        self._cid = 1000000
        self._ctl = 500000

    # -------------------------------------------------------------- helpers
    def _payer(self) -> str:
        names = list(PAYERS)
        return self.rng.choices(names, weights=[PAYERS[n][1] for n in names])[0]

    def _patient(self) -> Patient:
        self._pid += 1
        payer = self._payer()
        return Patient(
            patient_id=f"P{self._pid}",
            member_id=f"M{self.rng.randint(10**8, 10**9 - 1)}",
            payer=payer,
            coinsurance=self.rng.choice([0.3, 0.3, 0.4, 0.5]),
            deductible_left=self.rng.choice([0, 1500, 3000, 5000, 7500]),
        )

    def _new_claim(self, patient, loc, site, d_from, d_to, units, sca=None) -> Claim:
        self._cid += 1
        c = Claim(
            claim_id=f"DRC{self._cid}",
            patient=patient,
            loc=loc,
            site=site,
            service_from=d_from,
            service_to=d_to,
            units=units,
            billed=float(units * CHARGE_PER_UNIT[loc]),
            submit_date=d_to + timedelta(days=self.rng.randint(2, 9)),
        )
        if sca:
            c.sca_id, c.sca_per_diem = sca
        self.claims.append(c)
        return c

    def _weekly_claims(self, patient, loc, site, start, days, attend_per_week=7, sca=None):
        """Bill a stay in 7-day claims. Returns the day after the stay ends."""
        d = start
        end = start + timedelta(days=days)
        while d < end:
            week_end = min(d + timedelta(days=6), end - timedelta(days=1))
            span = (week_end - d).days + 1
            units = span if attend_per_week >= 7 else max(1, round(span * attend_per_week / 7))
            if week_end <= self.as_of:
                self._new_claim(patient, loc, site, d, week_end, units, sca)
            d = week_end + timedelta(days=1)
        return end

    def _sca_for(self, patient: Patient, loc: str, when: date):
        if loc not in SCA_PER_DIEM or self.rng.random() >= PAYERS[patient.payer][7]:
            return None
        sca_id = f"SCA-{len(self.sca_contracts) + 1:04d}"
        per_diem = SCA_PER_DIEM[loc] * self.rng.choice([0.9, 1.0, 1.0, 1.1])
        self.sca_contracts.append({
            "sca_id": sca_id,
            "payer": patient.payer,
            "patient_id": patient.patient_id,
            "level_of_care": loc,
            "per_diem": round(per_diem, 2),
            "effective_from": when.isoformat(),
        })
        return sca_id, round(per_diem, 2)

    # ------------------------------------------------------------- episodes
    def generate_census(self):
        rng = self.rng
        d = self.start
        while d <= self.as_of:
            # Residential admissions, then step-down to PHP and IOP.
            for _ in range(self._poisson(1.35)):
                p = self._patient()
                site = rng.choice(SITE_FOR_LOC["RTC"])
                nxt = self._weekly_claims(p, "RTC", site, d, rng.randint(18, 40),
                                          sca=self._sca_for(p, "RTC", d))
                if rng.random() < 0.7:
                    self._php_iop(p, nxt)
            for _ in range(self._poisson(0.45)):
                self._php_iop(self._patient(), d)
            for _ in range(self._poisson(0.3)):
                p = self._patient()
                self._weekly_claims(p, "IOP", rng.choice(SITE_FOR_LOC["IOP"]), d,
                                    rng.randint(21, 56), attend_per_week=rng.choice([3, 4, 5]))
            for _ in range(self._poisson(0.25)):
                p = self._patient()
                self._weekly_claims(p, "TMS", "Phoenix", d, rng.randint(42, 49), attend_per_week=5)
            d += timedelta(days=1)
        # Outpatient therapy: monthly claims for an ongoing panel.
        month = self.start
        panel = [self._patient() for _ in range(55)]
        while month <= self.as_of:
            nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            last = min(nxt - timedelta(days=1), self.as_of)
            for i, p in enumerate(panel):
                if rng.random() < 0.06:
                    panel[i] = p = self._patient()
                if last >= month:
                    self._new_claim(p, "OP", "Phoenix", month, last, rng.randint(2, 5))
            month = nxt

    def _php_iop(self, p: Patient, start: date):
        rng = self.rng
        site = rng.choice(SITE_FOR_LOC["PHP"])
        nxt = self._weekly_claims(p, "PHP", site, start, rng.randint(14, 30), attend_per_week=5,
                                  sca=self._sca_for(p, "PHP", start))
        if rng.random() < 0.7:
            self._weekly_claims(p, "IOP", site, nxt, rng.randint(21, 49),
                                attend_per_week=rng.choice([3, 4, 5]))

    def _poisson(self, lam: float) -> int:
        l, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= l:
                return k
            k += 1

    # ---------------------------------------------------------- adjudication
    def adjudicate(self):
        for c in sorted(self.claims, key=lambda c: c.service_from):
            self._adjudicate_one(c)

    def _lag(self, payer: str) -> int:
        median = PAYERS[payer][6]
        return max(7, min(200, int(self.rng.lognormvariate(math.log(median), 0.45))))

    def _priced(self, c: Claim, allow_multiplan=True, svc_month: date | None = None):
        """Return (allowed, paid, patient_resp, adjustments, contract_class)."""
        rng = self.rng
        payer = c.patient.payer
        _, _, share, _, mp, *_ = PAYERS[payer]
        if payer == "Aetna" and c.loc == "PHP" and svc_month and svc_month >= date(2026, 5, 1):
            mp = 0.80
        contract = ""
        if c.sca_id:
            allowed = c.sca_per_diem * c.units
            if rng.random() < 0.15:  # payer loaded the wrong rate
                allowed *= rng.uniform(0.6, 0.8)
            coins, use_deductible = 0.10, False
            contract = c.sca_id
        else:
            if allow_multiplan and rng.random() < mp:
                allowed = c.billed * rng.uniform(0.27, 0.37)
                contract = "MULTIPLAN"
            else:
                allowed = c.billed * rng.uniform(share - 0.08, share + 0.08)
                if rng.random() < 0.02:  # processing error
                    allowed *= 0.5
            coins, use_deductible = c.patient.coinsurance, True
        allowed = round(min(allowed, c.billed), 2)
        ded = 0.0
        if use_deductible and c.patient.deductible_left > 0:
            ded = round(min(allowed, c.patient.deductible_left), 2)
            c.patient.deductible_left -= ded
        coins_amt = round((allowed - ded) * coins, 2)
        paid = round(allowed - ded - coins_amt, 2)
        reduction = round(c.billed - allowed, 2)
        adj = []
        if reduction > 0:
            adj.append(("CO" if contract else "OA", "45", reduction))
        if ded > 0:
            adj.append(("PR", "1", ded))
        if coins_amt > 0:
            adj.append(("PR", "2", coins_amt))
        return allowed, paid, round(ded + coins_amt, 2), adj, contract

    def _adjudicate_one(self, c: Claim):
        rng = self.rng
        payer = c.patient.payer
        svc_month = c.service_from.replace(day=1)
        if rng.random() < 0.03:
            return  # payer never responds
        deny = PAYERS[payer][3] * LOC_DENY_FACTOR[c.loc]
        cigna_auth_spike = payer == "Cigna" and svc_month >= date(2026, 6, 1)
        if cigna_auth_spike:
            deny += 0.20
        remit_date = c.submit_date + timedelta(days=self._lag(payer))

        if rng.random() < deny:
            if cigna_auth_spike and rng.random() < 0.7:
                carc, overturn = "197", 0.25
            else:
                carc, _, overturn = rng.choices(DENIAL_MIX, weights=[w for _, w, _ in DENIAL_MIX])[0]
            group = "PR" if carc in ("27", "96") else "CO"
            self._remit(c, remit_date, STATUS_DENIED, 0.0, 0.0, None, [(group, carc, c.billed)], "")
            if rng.random() < overturn:
                later = remit_date + timedelta(days=rng.randint(60, 150))
                allowed, paid, pr, adj, contract = self._priced(c, allow_multiplan=False, svc_month=svc_month)
                self._remit(c, later, STATUS_PRIMARY, paid, pr, allowed, adj, contract)
            return

        allowed, paid, pr, adj, contract = self._priced(c, svc_month=svc_month)
        if rng.random() < PAYERS[payer][5] and paid > 0:
            # Check mailed to the member. The provider sees the claim with no payment.
            self._remit(c, remit_date, STATUS_PRIMARY, 0.0, pr, allowed, adj + [("OA", "100", paid)], contract)
            roll = rng.random()
            if roll < 0.45:
                self._patient_payment(c, remit_date + timedelta(days=rng.randint(15, 120)), paid)
            elif roll < 0.60:
                self._patient_payment(c, remit_date + timedelta(days=rng.randint(30, 150)), round(paid * 0.5, 2))
            return
        self._remit(c, remit_date, STATUS_PRIMARY, paid, pr, allowed, adj, contract)

    def _remit(self, c, when, status, paid, pr, allowed, adjustments, contract):
        if when > self.as_of:
            return
        self._ctl += 1
        claim_ref = c.claim_id
        if self.rng.random() < 0.006:
            claim_ref = c.claim_id.replace("DRC", "")  # payer dropped the prefix
        self.remits.append({
            "claim": c,
            "date": when,
            "claim_id": claim_ref,
            "status": status,
            "billed": c.billed,
            "paid": paid,
            "patient_resp": pr,
            "allowed": allowed,
            "adjustments": adjustments,
            "contract_class": contract,
            "payer_ctl": f"{PAYERS[c.patient.payer][0]}{self._ctl}",
        })

    def _patient_payment(self, c: Claim, when: date, amount: float):
        if when > self.as_of:
            return
        self.patient_payments.append({
            "payment_id": f"PP{len(self.patient_payments) + 1:06d}",
            "claim_id": c.claim_id,
            "patient_id": c.patient.patient_id,
            "date": when.isoformat(),
            "amount": f"{amount:.2f}",
            "note": "insurance check forwarded by patient",
        })

    # ---------------------------------------------------------------- output
    def write(self, out: Path):
        out = Path(out)
        era_dir = out / "era"
        era_dir.mkdir(parents=True, exist_ok=True)
        for old in era_dir.glob("*.835"):
            old.unlink()

        with open(out / "claims.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["claim_id", "patient_id", "member_id", "payer", "payer_id", "level_of_care",
                        "site", "service_from", "service_to", "units", "billed", "service_code",
                        "submit_date", "sca_id", "sca_per_diem"])
            for c in self.claims:
                qual, code = LOC_SERVICE_CODE[c.loc]
                w.writerow([c.claim_id, c.patient.patient_id, c.patient.member_id, c.patient.payer,
                            PAYERS[c.patient.payer][0], c.loc, c.site, c.service_from.isoformat(),
                            c.service_to.isoformat(), c.units, f"{c.billed:.2f}", f"{qual}:{code}",
                            c.submit_date.isoformat(), c.sca_id,
                            f"{c.sca_per_diem:.2f}" if c.sca_id else ""])

        with open(out / "patient_payments.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["payment_id", "claim_id", "patient_id", "date", "amount", "note"])
            w.writeheader()
            w.writerows(sorted(self.patient_payments, key=lambda r: r["date"]))

        with open(out / "sca_contracts.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["sca_id", "payer", "patient_id", "level_of_care",
                                              "per_diem", "effective_from"])
            w.writeheader()
            w.writerows(self.sca_contracts)

        # One payment per payer per week, paid on the Friday of that week.
        batches: dict[tuple, list] = defaultdict(list)
        for r in self.remits:
            friday = r["date"] + timedelta(days=(4 - r["date"].weekday()) % 7)
            if friday > self.as_of:
                continue  # paid after the data cut-off
            batches[(r["claim"].patient.payer, friday)].append(r)
        ctl = 1
        for (payer, pay_date), items in sorted(batches.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            payer_id = PAYERS[payer][0]
            claims = []
            for r in items:
                c = r["claim"]
                qual, code = LOC_SERVICE_CODE[c.loc]
                claims.append({
                    "claim_id": r["claim_id"], "status": r["status"], "billed": f"{r['billed']:.2f}",
                    "paid": f"{r['paid']:.2f}", "patient_resp": f"{r['patient_resp']:.2f}",
                    "payer_ctl": r["payer_ctl"], "member_id": c.patient.member_id,
                    "patient_last": f"PT{c.patient.patient_id[1:]}",
                    "service_from": c.service_from, "service_to": c.service_to,
                    "svc_qual": qual, "svc_code": code, "units": c.units,
                    "allowed": None if r["allowed"] is None else f"{r['allowed']:.2f}",
                    "adjustments": r["adjustments"], "contract_class": r["contract_class"],
                })
            text = build_835(
                payer=payer, payer_id=payer_id, payee="DEMO BEHAVIORAL HEALTH",
                payee_npi="1999999999", payment_date=pay_date,
                trace_number=f"{payer_id}{pay_date.strftime('%y%m%d')}", control_number=ctl,
                claims=claims,
            )
            slug = payer.split()[0].lower()
            (era_dir / f"{pay_date.isoformat()}_{slug}.835").write_text(text)
            ctl += 1


def generate(out: Path, seed: int = 7, as_of: date = AS_OF) -> dict:
    g = Generator(seed=seed, as_of=as_of)
    g.generate_census()
    g.adjudicate()
    g.write(out)
    return {
        "claims": len(g.claims),
        "remits": len(g.remits),
        "patient_payments": len(g.patient_payments),
        "billed": sum(c.billed for c in g.claims),
    }
