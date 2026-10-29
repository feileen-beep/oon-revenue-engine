"""Run a month-end close end to end and write every output."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from .close import (CloseResult, aging, month_end, prior_month_end, rate_table_rows, run_close,
                    true_up_drivers, write_journal_csv)
from .ledger import Ledger, load
from .memo import build_facts, write_memo
from .model import ModelConfig, fit
from .reference import BUCKET_LABELS, LOC_NAMES
from .worklists import denial_trend, open_denials, paid_to_patient, underpayments, unmatched_remits


def backtest(ledger: Ledger, start: date, end: date, final: CloseResult, cfg: ModelConfig) -> list[dict]:
    """Accrual rate booked at each month's close vs. what those services look like now."""
    rows = []
    claims = list(ledger.claims.values())
    m = month_end(start)
    prior_tables = fit(claims, prior_month_end(m), cfg)
    while m <= end:
        res = run_close(ledger, m, cfg, prior_tables=prior_tables)
        prior_tables = res.tables
        new_ids = [cid for cid in res.now if cid not in res.before]
        billed = sum(res.now[c].claim.billed for c in new_ids)
        booked = sum(res.now[c].expected for c in new_ids)
        current = sum(final.now[c].expected for c in new_ids)
        collected = sum(final.now[c].collected for c in new_ids)
        open_share = sum(final.now[c].remaining for c in new_ids)
        rows.append({"month": m.strftime("%Y-%m"), "charges": billed, "booked_rate": booked / billed,
                     "current_rate": current / billed, "collected_rate": collected / billed,
                     "still_expected": open_share, "net_revenue": res.net_revenue,
                     "true_up": res.total("true_up"), "cash": res.total("cash"),
                     "expected_cash": res.total("expected_cash"), "cash_on_prior": res.total("cash_on_prior")})
        m = month_end(date(m.year + (m.month == 12), m.month % 12 + 1, 1))
    return rows


def _csv(path: Path, rows: list[dict]):
    if not rows:
        path.write_text("")
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})


def run(data_dir: Path, as_of: date, out_dir: Path, use_claude: bool = True,
        backtest_months: int = 15) -> dict:
    cfg = ModelConfig()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = load(data_dir)
    res = run_close(ledger, as_of, cfg)
    prior = run_close(ledger, res.prior, cfg)

    under = underpayments(res)
    denials = open_denials(res)
    p2p = paid_to_patient(res)
    trend = denial_trend(res)
    facts = build_facts(res, under, denials, trend, prior)
    memo, memo_note = write_memo(facts, use_claude)

    months_back = as_of.year * 12 + as_of.month - 1 - (backtest_months - 1)
    bt_start = date(months_back // 12, months_back % 12 + 1, 1)
    bt = backtest(ledger, bt_start, as_of, res, cfg)

    tag = as_of.strftime("%Y-%m")
    write_journal_csv(res, out_dir / f"journal_entry_{tag}.csv")
    _csv(out_dir / f"underpayments_{tag}.csv", under)
    _csv(out_dir / f"open_denials_{tag}.csv", denials)
    _csv(out_dir / f"paid_to_patient_{tag}.csv", p2p)
    _csv(out_dir / f"unmatched_remits_{tag}.csv", unmatched_remits(ledger))
    _csv(out_dir / f"match_exceptions_{tag}.csv", ledger.match_log)
    rates = rate_table_rows(res)
    _csv(out_dir / f"rate_table_{tag}.csv",
         [{k: v for k, v in r.items() if k != "by_age"} | {f"rate_{b}": r["by_age"][b] for b in BUCKET_LABELS}
          for r in rates])
    (out_dir / f"memo_{tag}.md").write_text(memo)

    ag = aging(res)
    payer_rows = []
    for payer, v in sorted(res.by_payer.items()):
        payer_rows.append({"payer": payer, **v})
    report = {
        "as_of": as_of.isoformat(),
        "month": as_of.strftime("%B %Y"),
        "facts": facts,
        "memo": memo,
        "memo_note": memo_note,
        "rollforward": res.rollforward(),
        "by_payer": payer_rows,
        "lines": [{"loc": k[0], "loc_name": LOC_NAMES[k[0]], "site": k[1], **v} for k, v in sorted(res.lines.items())],
        "aging": ag,
        "buckets": BUCKET_LABELS,
        "rates": rates,
        "experience": [{"payer": k[0], "loc": k[1], **v} for k, v in sorted(res.tables.experience.items())],
        "true_up_drivers": true_up_drivers(res),
        "underpayments": under,
        "denials": denials,
        "denial_trend": trend,
        "paid_to_patient": p2p,
        "unmatched": unmatched_remits(ledger),
        "match_exceptions": ledger.match_log,
        "journal_entry": res.journal_entry(),
        "backtest": bt,
        "counts": {"claims": len(ledger.claims), "remits": len(ledger.remits),
                   "remit_files": len({r.source_file for r in ledger.remits})},
    }
    (out_dir / f"report_{tag}.json").write_text(json.dumps(report, indent=1, default=str))
    return report
