# Out-of-network revenue and collections engine

A working model of how an out-of-network behavioral health provider can book revenue at the amount it
expects to collect, true it up every month, and give its revenue cycle team ranked work queues. It is
built around the finance problems of an out-of-network residential and outpatient provider, such as commercial payers that
allow a fraction of billed charges, MultiPlan repricing, single case agreements, denials, and checks
mailed to patients instead of the provider.

**Live demo:** [dashboard](docs/index.html) (published with GitHub Pages from `docs/`).

> All data in this repository is synthetic. Behavioral health and substance use treatment records are
> protected by HIPAA and 42 CFR Part 2, so the engine ships with a generator that invents realistic
> claims and remits instead.

## What it does

1. **Reads claims and ERA 835 remits.** A plain X12 835 parser (`oonrev/x12.py`) reads payments,
   claim-level and line-level adjustments (CARC groups CO, PR, OA), allowed amounts and repricing
   markers. Every remit is checked to balance: billed = paid + adjustments.
2. **Matches payments to claims** in three passes: exact claim number, normalized claim number
   (payers drop prefixes), then member ID + service dates + billed amount. Leftovers become exceptions.
3. **Tracks each claim's status**: pending, paid, denied, paid to patient, collected from patient, or
   written off at 365 days.
4. **Estimates collection rates** by payer × level of care × days unpaid, from claims old enough that
   their outcome is known. Recent months count more, and thin cells borrow from broader averages
   (credibility blending).
5. **Adjusts for recent experience.** History is a year old by definition, so the model compares cash
   on recent claims with what history says should have arrived by the same age. When a payer changes
   behavior, the rate moves within a month or two instead of waiting for the year-end true-up.
6. **Books the month** under ASC 606: gross charges, the implicit price concession on new services,
   the change in estimate on prior months, and write-offs of finalized claims, as a QuickBooks Online
   journal entry import with class = level of care and location = site. Gross AR, the allowance and
   net AR each roll forward to the penny.
7. **Builds work queues**: underpayments (below the SCA rate or far below the payer's usual allowed,
   with MultiPlan flagged), open denials by appeal deadline, and payments sent to patients.
8. **Writes the owner's summary.** The code computes every figure. Claude only drafts the prose, and
   every number in the draft is checked against the computed facts; if anything does not trace back,
   the engine uses a plain template instead.
9. **Backtests itself**: for each of the last 15 months, the rate booked at close against today's
   estimate and the cash collected so far.

## Run it

```bash
python -m oonrev generate                 # synthetic data -> data/
python -m oonrev close --as-of 2026-09-30 # outputs -> out/, dashboard -> docs/index.html
python -m pytest -q
```

Python 3.10+ and no required dependencies. For the Claude-written memo, `pip install anthropic` and set
`ANTHROPIC_API_KEY`; without a key the template memo is used. `--no-claude` forces the template.

### Outputs (one set per close, see `examples/2026-09/`)

| File | What it is |
| --- | --- |
| `journal_entry_YYYY-MM.csv` | QuickBooks Online journal entry import, balanced, by class and location |
| `memo_YYYY-MM.md` | One-page owner summary |
| `rate_table_YYYY-MM.csv` | Accrual and age-bucket rates by payer × level of care, with credibility and experience factor |
| `underpayments_YYYY-MM.csv` | Underpaid claims with expected allowed, estimated recovery and next step |
| `open_denials_YYYY-MM.csv` | Open denials with CARC, appeal deadline and expected recovery |
| `paid_to_patient_YYYY-MM.csv` | Insurance checks sent to members that need collecting |
| `match_exceptions_YYYY-MM.csv` | Remits matched on something other than an exact claim number |
| `report_YYYY-MM.json` | Everything above plus roll-forwards and backtest, for the dashboard |

### Using real data

`oonrev/ledger.py` expects `claims.csv` (columns as written by the generator; a CollaborateMD or Kipu
claims export maps onto them), a folder of `.835` files, optional `patient_payments.csv`, and optional
`sca_contracts.csv`. Run it inside the provider's own environment; patient data should never leave it.

## Accounting approach

For each claim, the expected value at a close date is cash collected so far plus cash still expected.
Revenue recognized to date equals that expected value, so revenue for a month is the change in the
total:

- **Current-period services:** gross charges less the implicit price concession (variable
  consideration estimated with the portfolio approach).
- **Prior-period services:** change in estimate from new remits, denials, appeals, payments sent to
  patients, and the refreshed rate table. `true_up_drivers` breaks it down by cause.

Patient AR is carried gross with a contra allowance (accounts 1200 / 1210 / 4000 / 4050 / 4060; names
are in `oonrev/close.py`).

**Simplifications to know about:** revenue follows the claim's service end date (weekly claims that
cross month-end are not split by day); unbilled services at month-end are not accrued; patient
responsibility (deductible and coinsurance) is outside the estimate, which covers insurance payments
and payments forwarded by patients; underpayment recoveries are worked but not accrued until received.

## Synthetic data

`oonrev/synth.py` generates 33 months of residential, PHP, IOP, outpatient and TMS claims across
Glendale, Scottsdale and Phoenix for six payers (about $6M of charges a month), with patients stepping
down from residential to PHP to IOP. It includes deductibles and coinsurance, MultiPlan repricing,
single case agreements (some loaded at the wrong rate), denials with real CARCs and appeal overturns,
payments mailed to patients, and a few remits with mangled claim numbers. Two storylines give the
memo something to explain: Aetna starts repricing most PHP claims through MultiPlan in May 2026, and
Cigna's missing-authorization denials jump from June 2026.

## Layout

```
oonrev/
  x12.py        835 reader and writer
  synth.py      synthetic data generator
  ledger.py     loading, matching, claim status
  model.py      collection-rate model
  close.py      accrual, true-up, journal entry, aging
  worklists.py  underpayments, denials, paid-to-patient
  memo.py       owner summary, Claude draft with number checking
  pipeline.py   end-to-end close and backtest
  dashboard.py  renders docs/index.html
tests/          parser, claim lifecycle, roll-forward and journal entry checks
```
