"""Month-end close: revenue at expected collection, true-up, and the journal entry.

For every claim, the expected ultimate collection at a close date T is

    E_T = cash collected so far + cash still expected (from the rate model)

and revenue recognized to date for that claim equals E_T. Revenue for a month
is therefore the change in E across all claims, which splits cleanly into:

* current-period services: gross charges less the implicit price concession
  (ASC 606 variable consideration, estimated with the portfolio approach)
* prior-period services: the change in estimate, or true-up, from new remits,
  denials, appeals and the refreshed rate table

The books carry patient AR at gross with a contra allowance:

    gross AR  = billed - collected - written off       (open claims)
    net AR    = sum of cash still expected
    allowance = gross AR - net AR

Each balance rolls forward exactly, and tests check that it does.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .ledger import Claim, Ledger
from .model import ModelConfig, RateTables, fit, remaining_expected
from .reference import BUCKET_LABELS, age_bucket

ACCOUNTS = {
    "ar_gross": "1200 Patient Accounts Receivable - Gross",
    "allowance": "1210 Allowance for Implicit Price Concessions",
    "revenue_gross": "4000 Patient Service Revenue - Gross Charges",
    "concession": "4050 Implicit Price Concessions",
    "true_up": "4060 Change in Estimate - Prior Period Services",
}


def month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def prior_month_end(d: date) -> date:
    return d.replace(day=1) - timedelta(days=1)


@dataclass
class ClaimView:
    claim: Claim
    status: str
    collected: float
    remaining: float
    remaining_30: float
    written_off: float
    finalized: bool
    age_days: int

    @property
    def expected(self) -> float:
        return self.collected + self.remaining


def snapshot(claims: list[Claim], tables: RateTables, as_of: date) -> dict[str, ClaimView]:
    out = {}
    for c in claims:
        if c.service_to > as_of:
            continue
        st = c.state(as_of)
        rem = remaining_expected(c, tables, as_of)
        rem30 = remaining_expected(c, tables, as_of, horizon=True)
        w = c.billed - st.collected if st.finalized else 0.0
        out[c.claim_id] = ClaimView(c, st.status, st.collected, rem, rem30, w, st.finalized, st.age_days)
    return out


@dataclass
class CloseResult:
    as_of: date
    prior: date
    tables: RateTables
    prior_tables: RateTables
    now: dict[str, ClaimView]
    before: dict[str, ClaimView]
    lines: dict[tuple, dict] = field(default_factory=dict)   # (loc, site) -> amounts
    by_payer: dict[str, dict] = field(default_factory=dict)

    # --------------------------------------------------------------- totals
    def total(self, key: str) -> float:
        return sum(v[key] for v in self.lines.values())

    @property
    def net_revenue(self) -> float:
        return self.total("charges") - self.total("concession") + self.total("true_up")

    def balances(self, which: str = "now") -> dict:
        views = self.now if which == "now" else self.before
        gross = sum(v.claim.billed - v.collected - v.written_off for v in views.values())
        net = sum(v.remaining for v in views.values())
        return {"gross_ar": gross, "net_ar": net, "allowance": gross - net}

    def rollforward(self) -> dict:
        b0, b1 = self.balances("before"), self.balances("now")
        return {
            "gross_ar": {"opening": b0["gross_ar"], "charges": self.total("charges"), "cash": -self.total("cash"),
                         "write_offs": -self.total("write_offs"), "closing": b1["gross_ar"]},
            "allowance": {"opening": b0["allowance"], "concession": self.total("concession"),
                          "true_up": -self.total("true_up"), "write_offs": -self.total("write_offs"),
                          "closing": b1["allowance"]},
            "net_ar": {"opening": b0["net_ar"], "net_revenue": self.net_revenue, "cash": -self.total("cash"),
                       "closing": b1["net_ar"]},
        }

    # --------------------------------------------------------- journal entry
    def journal_entry(self, journal_no: str | None = None) -> list[dict]:
        """Rows ready for QuickBooks Online's journal entry import."""
        jno = journal_no or f"REV-{self.as_of:%Y%m}"
        d = self.as_of.strftime("%m/%d/%Y")
        rows = []

        def add(account, amount, desc, loc, site):
            if abs(amount) < 0.005:
                return
            rows.append({
                "Journal No": jno, "Journal Date": d, "Account": ACCOUNTS[account],
                "Debits": f"{amount:.2f}" if amount > 0 else "",
                "Credits": f"{-amount:.2f}" if amount < 0 else "",
                "Description": desc, "Location": site, "Class": loc,
            })

        for (loc, site), v in sorted(self.lines.items()):
            month = self.as_of.strftime("%b %Y")
            add("ar_gross", v["charges"], f"Gross charges, {month} services", loc, site)
            add("revenue_gross", -v["charges"], f"Gross charges, {month} services", loc, site)
            add("concession", v["concession"], f"Implicit price concession, {month} services", loc, site)
            add("allowance", -v["concession"], f"Implicit price concession, {month} services", loc, site)
            add("allowance", v["true_up"], "Change in estimate, prior-period services", loc, site)
            add("true_up", -v["true_up"], "Change in estimate, prior-period services", loc, site)
            add("allowance", v["write_offs"], "Write off finalized claims to allowance", loc, site)
            add("ar_gross", -v["write_offs"], "Write off finalized claims to allowance", loc, site)
        return rows


def run_close(ledger: Ledger, as_of: date, cfg: ModelConfig | None = None,
              prior_tables: RateTables | None = None) -> CloseResult:
    cfg = cfg or ModelConfig()
    prior = prior_month_end(as_of)
    claims = list(ledger.claims.values())
    tables = fit(claims, as_of, cfg)
    prior_tables = prior_tables or fit(claims, prior, cfg)
    now = snapshot(claims, tables, as_of)
    before = snapshot(claims, prior_tables, prior)
    res = CloseResult(as_of, prior, tables, prior_tables, now, before)

    zero = lambda: {"charges": 0.0, "concession": 0.0, "true_up": 0.0, "write_offs": 0.0, "cash": 0.0,
                    "expected_cash": 0.0, "cash_on_prior": 0.0, "new_expected": 0.0}
    lines = defaultdict(zero)
    payers = defaultdict(zero)
    for cid, v in now.items():
        c = v.claim
        b = before.get(cid)
        cash = v.collected - (b.collected if b else 0.0)
        wo = v.written_off - (b.written_off if b else 0.0)
        for bucket in (lines[(c.loc, c.site)], payers[c.payer]):
            bucket["cash"] += cash
            bucket["write_offs"] += wo
            if b is None:
                bucket["charges"] += c.billed
                bucket["concession"] += c.billed - v.expected
                bucket["new_expected"] += v.expected
            else:
                bucket["true_up"] += v.expected - b.expected
                bucket["expected_cash"] += b.remaining_30
                bucket["cash_on_prior"] += cash
    res.lines = dict(lines)
    res.by_payer = dict(payers)
    return res


# ------------------------------------------------------------------ reports


def aging(res: CloseResult) -> dict[str, dict[str, dict]]:
    """Gross and net AR by payer and age bucket (days since service end)."""
    out: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {"gross": 0.0, "net": 0.0, "n": 0}))
    for v in res.now.values():
        if v.finalized:
            continue
        cell = out[v.claim.payer][age_bucket(v.age_days)]
        cell["gross"] += v.claim.billed - v.collected
        cell["net"] += v.remaining
        cell["n"] += 1
    return {p: dict(b) for p, b in out.items()}


def true_up_drivers(res: CloseResult) -> list[dict]:
    """Break the prior-period true-up into what caused it, by payer x level of care."""
    out = defaultdict(lambda: defaultdict(float))
    for cid, v in res.now.items():
        b = res.before.get(cid)
        if b is None:
            continue
        delta = v.expected - b.expected
        if abs(delta) < 0.005:
            continue
        if b.status == v.status and v.collected == b.collected:
            reason = "rate table refresh / aging"
        elif v.status == "denied" and b.status != "denied":
            reason = "new denials"
        elif v.status in ("paid", "patient_collected") and b.status != v.status:
            reason = "remits vs. estimate"
        elif v.status == "paid_to_patient" and b.status != "paid_to_patient":
            reason = "paid to patient"
        elif v.status == "written_off":
            reason = "write-off at 365 days"
        else:
            reason = "other activity"
        key = (v.claim.payer, v.claim.loc)
        out[key][reason] += delta
        out[key]["total"] += delta
    rows = [{"payer": p, "loc": l, **dict(d)} for (p, l), d in out.items()]
    return sorted(rows, key=lambda r: r["total"])


def rate_table_rows(res: CloseResult) -> list[dict]:
    rows = []
    for (payer, loc, bucket), cell in sorted(res.tables.pending.items()):
        if payer == "*" or bucket != BUCKET_LABELS[0]:
            continue
        exp = res.tables.experience.get((payer, loc), {})
        prior_rate = res.prior_tables.accrual_rate(payer, loc)
        rows.append({
            "payer": payer, "loc": loc, "historical_rate": cell.rate,
            "experience_factor": exp.get("factor", 1.0), "experience_n": exp.get("n", 0),
            "accrual_rate": res.tables.accrual_rate(payer, loc), "prior_accrual_rate": prior_rate,
            "n": cell.n, "credibility": cell.z,
            "by_age": {b: res.tables.pending_rate(payer, loc, b) for b in BUCKET_LABELS},
        })
    return rows


def write_journal_csv(res: CloseResult, path: Path) -> Path:
    rows = res.journal_entry()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return path
