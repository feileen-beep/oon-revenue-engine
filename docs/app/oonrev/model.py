"""Collection-rate model: how much of each open claim we expect to collect.

Every number here comes from claim history; nothing is typed in by hand.

**Rates by payer x level of care x age.** For each age bucket, look at mature
claims (old enough that their outcome is known) that were still unpaid at the
start of that bucket, and measure what share of billed charges they went on to
collect. The 0-30 bucket is the accrual rate for new services. Older buckets
fall, because a claim that has sat unpaid for 120 days is worth less than a new
one. Denied claims and payments sent to the patient get their own recovery
rates.

**Recency weighting.** Recent service months count more (6-month half-life),
so the table follows payer behavior as it changes.

**Credibility blending.** A payer x level-of-care cell with few claims is pulled
toward the level-of-care average, which is pulled toward the overall average:
estimate = Z * own + (1 - Z) * parent, with Z = n / (n + k).

**Recent-experience adjustment.** Mature claims are at least a year old, so on
their own they cannot see a payer that changed behavior last quarter. For
claims from the last year, the model compares cash collected so far with what
the mature payment pattern says should have come in by the same age. The ratio,
credibility-weighted, scales that segment's rates. This is how a jump in
MultiPlan repricing or an authorization denial spike shows up in the accrual
within a month or two, instead of a year later in the true-up.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .ledger import WRITE_OFF_AGE_DAYS, Claim
from .reference import BUCKET_LABELS, age_bucket, bucket_start

ALL = "*"
HORIZON_STEP = 10     # days between ages in the cash-timing table
HORIZON_STEPS = 37


@dataclass
class ModelConfig:
    maturity_days: int = WRITE_OFF_AGE_DAYS   # follow-up window for a claim's outcome to be known
    half_life_months: float = 6.0
    credibility_k: float = 25.0
    experience_k: float = 40.0
    experience_window_days: int = 150
    experience_min_age: int = 45
    experience_clip: tuple[float, float] = (0.6, 1.4)
    cash_horizon_days: int = 30


@dataclass
class Cell:
    rate: float          # expected share of the base still to be collected
    rate_30: float       # share expected within the next 30 days
    n: int
    z: float             # credibility of this cell's own experience
    raw: float | None = None


@dataclass
class RateTables:
    as_of: date
    pending: dict[tuple, Cell] = field(default_factory=dict)        # (payer, loc, bucket)
    denied: dict[str, Cell] = field(default_factory=dict)           # payer
    paid_to_patient: dict[str, Cell] = field(default_factory=dict)  # payer
    experience: dict[tuple, dict] = field(default_factory=dict)     # (payer, loc) -> detail
    development: dict[str, list[float]] = field(default_factory=dict)  # payer -> share collected by age
    # (payer, "pending" | "denied", age step) -> share of billed expected in the next 30 days
    horizon: dict[tuple, float] = field(default_factory=dict)

    def pending_cash_rate(self, payer, loc, age: int) -> float:
        """Share of billed expected within the cash horizon for a claim pending at this age."""
        rate = self._interp(payer, "pending", age)
        return rate * self.experience.get((payer, loc), {}).get("factor", 1.0)

    def _interp(self, payer, kind, age: int) -> float:
        i, frac = divmod(min(age, (HORIZON_STEPS - 1) * HORIZON_STEP), HORIZON_STEP)
        lo = self.horizon.get((payer, kind, i), self.horizon.get((ALL, kind, i), 0.0))
        hi = self.horizon.get((payer, kind, i + 1), self.horizon.get((ALL, kind, i + 1), lo))
        return lo + (hi - lo) * frac / HORIZON_STEP

    def pending_rate(self, payer, loc, bucket, horizon=False) -> float:
        cell = self.pending.get((payer, loc, bucket)) or self.pending.get((ALL, loc, bucket)) \
            or self.pending.get((ALL, ALL, bucket))
        if cell is None:
            return 0.0
        adj = self.experience.get((payer, loc), {}).get("factor", 1.0)
        return (cell.rate_30 if horizon else cell.rate) * adj

    def accrual_rate(self, payer, loc) -> float:
        return self.pending_rate(payer, loc, BUCKET_LABELS[0])

    def _simple(self, table, payer, horizon):
        cell = table.get(payer) or table.get(ALL)
        if cell is None:
            return 0.0
        return cell.rate_30 if horizon else cell.rate

    def denied_cash_rate(self, payer, days_since_denial: int) -> float:
        return self._interp(payer, "denied", days_since_denial)

    def denied_rate(self, payer, horizon=False) -> float:
        return self._simple(self.denied, payer, horizon)

    def p2p_rate(self, payer, horizon=False) -> float:
        return self._simple(self.paid_to_patient, payer, horizon)


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


class _Acc:
    """Weighted ratio accumulator: sum(w * value) / sum(w * base)."""

    __slots__ = ("num", "num30", "den", "n")

    def __init__(self):
        self.num = self.num30 = self.den = 0.0
        self.n = 0

    def add(self, base, value, value30, w):
        self.num += w * value
        self.num30 += w * value30
        self.den += w * base
        self.n += 1

    def ratio(self):
        if self.den <= 0:
            return None, None
        return self.num / self.den, self.num30 / self.den


def _blend(own: _Acc | None, parent: Cell | None, k: float) -> Cell:
    r, r30 = own.ratio() if own else (None, None)
    n = own.n if own else 0
    if parent is None:
        return Cell(r or 0.0, r30 or 0.0, n, 1.0 if n else 0.0, r)
    if r is None:
        return Cell(parent.rate, parent.rate_30, 0, 0.0, None)
    z = n / (n + k)
    return Cell(z * r + (1 - z) * parent.rate, z * r30 + (1 - z) * parent.rate_30, n, z, r)


def fit(claims: list[Claim], as_of: date, cfg: ModelConfig | None = None) -> RateTables:
    cfg = cfg or ModelConfig()
    tables = RateTables(as_of=as_of)
    mature_cutoff = as_of - timedelta(days=cfg.maturity_days)
    mature = [c for c in claims if c.service_to <= mature_cutoff]
    if not mature:
        raise ValueError(f"no mature claims as of {as_of}: need claims at least {cfg.maturity_days} days old")
    latest = max(c.service_month for c in mature)
    h = cfg.cash_horizon_days

    def weight(c: Claim) -> float:
        return 0.5 ** (_months_between(c.service_month, latest) / cfg.half_life_months)

    pend: dict[tuple, _Acc] = defaultdict(_Acc)
    den: dict[str, _Acc] = defaultdict(_Acc)
    p2p: dict[str, _Acc] = defaultdict(_Acc)
    dev_num: dict[str, list[float]] = defaultdict(lambda: [0.0] * 13)
    hz: dict[tuple, _Acc] = defaultdict(_Acc)

    for c in mature:
        w = weight(c)
        end = c.service_to + timedelta(days=cfg.maturity_days)
        first = c.first_remit()
        total = c.collected(None, end)
        for label in BUCKET_LABELS:
            s = c.service_to + timedelta(days=bucket_start(label))
            if first is not None and first.date <= s:
                break  # no longer pending at this age
            value = c.collected(s, end)
            value30 = c.collected(s, s + timedelta(days=h))
            for key in ((c.payer, c.loc, label), (ALL, c.loc, label), (ALL, ALL, label)):
                pend[key].add(c.billed, value, value30, w)
        for step in range(HORIZON_STEPS):
            s = c.service_to + timedelta(days=step * HORIZON_STEP)
            if first is not None and first.date <= s:
                break
            value30 = c.collected(s, s + timedelta(days=h))
            for key in ((c.payer, "pending", step), (ALL, "pending", step)):
                hz[key].add(c.billed, value30, value30, w)
        denial = next((e for e in c.events if e.kind == "denied"), None)
        if denial:
            paid_after = next((e.date for e in c.events if e.kind == "paid" and e.date > denial.date), None)
            for step in range(HORIZON_STEPS):
                s = denial.date + timedelta(days=step * HORIZON_STEP)
                if s > end or (paid_after and paid_after <= s):
                    break
                value30 = c.collected(s, s + timedelta(days=h))
                for key in ((c.payer, "denied", step), (ALL, "denied", step)):
                    hz[key].add(c.billed, value30, value30, w)
        if denial and denial.date <= end:
            value = c.collected(denial.date, end)
            value30 = c.collected(denial.date, denial.date + timedelta(days=h))
            for key in (c.payer, ALL):
                den[key].add(c.billed, value, value30, w)
        p2p_event = next((e for e in c.events if e.kind == "paid_to_patient"), None)
        if p2p_event and p2p_event.date <= end:
            got = sum(e.cash for e in c.events if e.kind == "patient_payment" and e.date <= end)
            got30 = sum(e.cash for e in c.events if e.kind == "patient_payment"
                        and e.date <= p2p_event.date + timedelta(days=h))
            for key in (c.payer, ALL):
                p2p[key].add(p2p_event.p2p_amount, got, got30, w)
        # Payment pattern: share of eventual collections received by each 30-day age.
        if total > 0:
            for i in range(13):
                share = c.collected(None, c.service_to + timedelta(days=30 * i)) / total
                dev_num[c.payer][i] += w * share
                dev_num[ALL][i] += w * share

    k = cfg.credibility_k
    dev_weight = defaultdict(float)
    for c in mature:
        if c.collected(None, c.service_to + timedelta(days=cfg.maturity_days)) > 0:
            dev_weight[c.payer] += weight(c)
            dev_weight[ALL] += weight(c)
    tables.development = {key: [v / dev_weight[key] for v in vals] for key, vals in dev_num.items()}

    for label in BUCKET_LABELS:
        top = _blend(pend.get((ALL, ALL, label)), None, k)
        tables.pending[(ALL, ALL, label)] = top
        locs = {key[1] for key in pend if key[0] == ALL and key[1] != ALL}
        for loc in locs:
            loc_cell = _blend(pend.get((ALL, loc, label)), top, k)
            tables.pending[(ALL, loc, label)] = loc_cell
            for key in pend:
                if key[1] == loc and key[2] == label and key[0] != ALL:
                    tables.pending[key] = _blend(pend[key], loc_cell, k)
    for key, acc in hz.items():
        if key[0] == ALL:
            tables.horizon[key] = acc.ratio()[0] or 0.0
    for (payer, kind, step), acc in hz.items():
        if payer != ALL:
            z = acc.n / (acc.n + k)
            parent = tables.horizon[(ALL, kind, step)]
            tables.horizon[(payer, kind, step)] = z * (acc.ratio()[0] or 0.0) + (1 - z) * parent
    for table, acc in ((tables.denied, den), (tables.paid_to_patient, p2p)):
        top = _blend(acc.get(ALL), None, k)
        table[ALL] = top
        for payer, a in acc.items():
            if payer != ALL:
                table[payer] = _blend(a, top, k)

    _experience(tables, claims, as_of, cfg)
    return tables


def _expected_share_by_age(tables: RateTables, payer: str, age: int) -> float:
    curve = tables.development.get(payer) or tables.development.get(ALL)
    if not curve:
        return 1.0
    i, frac = divmod(min(age, 360), 30)
    lo = curve[i]
    hi = curve[min(i + 1, 12)]
    return lo + (hi - lo) * frac / 30


def _experience(tables: RateTables, claims: list[Claim], as_of: date, cfg: ModelConfig):
    """Scale rates by how recent claims are paying versus the mature pattern."""
    window_start = as_of - timedelta(days=cfg.experience_window_days)
    actual = defaultdict(float)
    expected = defaultdict(float)
    count = defaultdict(int)
    first_bucket = BUCKET_LABELS[0]
    for c in claims:
        if not (window_start < c.service_to <= as_of):
            continue
        age = (as_of - c.service_to).days
        if age < cfg.experience_min_age:
            continue
        cell = tables.pending.get((c.payer, c.loc, first_bucket)) or tables.pending[(ALL, c.loc, first_bucket)]
        expected[(c.payer, c.loc)] += c.billed * cell.rate * _expected_share_by_age(tables, c.payer, age)
        actual[(c.payer, c.loc)] += c.collected(None, as_of)
        count[(c.payer, c.loc)] += 1
    lo, hi = cfg.experience_clip
    for key, exp in expected.items():
        if exp <= 0:
            continue
        ratio = actual[key] / exp
        z = count[key] / (count[key] + cfg.experience_k)
        factor = min(hi, max(lo, 1 + z * (ratio - 1)))
        tables.experience[key] = {"ratio": ratio, "z": z, "factor": factor, "n": count[key],
                                  "actual": actual[key], "expected": exp}


def remaining_expected(c: Claim, tables: RateTables, as_of: date, horizon: bool = False) -> float:
    """Cash still expected on a claim (or within the cash horizon, if horizon)."""
    st = c.state(as_of)
    if st.finalized:
        return 0.0
    if st.status == "pending":
        if horizon:
            return c.billed * tables.pending_cash_rate(c.payer, c.loc, st.age_days)
        return c.billed * tables.pending_rate(c.payer, c.loc, age_bucket(st.age_days))
    if st.status == "denied":
        if horizon:
            return c.billed * tables.denied_cash_rate(c.payer, (as_of - st.denial_date).days)
        return c.billed * tables.denied_rate(c.payer)
    if st.status == "paid_to_patient":
        return st.p2p_outstanding * tables.p2p_rate(c.payer, horizon)
    return 0.0
