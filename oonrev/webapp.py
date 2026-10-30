"""Glue for the in-browser page (docs/run.html), which runs this package in Pyodide.

Uploaded files are written to /uploads by the page, so nothing leaves the
viewer's computer. Every function returns plain JSON-able data.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

from . import dashboard, importers, pipeline, synth
from .close import month_end, prior_month_end
from .model import ModelConfig

UPLOADS = Path("/uploads")
DATA = Path("/work/data")
OUT = Path("/work/out")
MIN_HISTORY_DAYS = ModelConfig().maturity_days + 31


def _is_x12(text: str) -> bool:
    return text.lstrip("﻿").lstrip().startswith("ISA")


def inspect(path: str) -> str:
    """Describe one uploaded file: an 835, or a CSV with suggested mappings."""
    text = Path(path).read_text(errors="replace")
    if _is_x12(text):
        return json.dumps({"kind": "835"})
    headers, rows = importers.read_csv(text)
    guesses = {k: importers.guess_mapping(headers, k) for k in importers.FIELDS}
    required = {k: [f for f, _, req, _ in importers.FIELDS[k] if req] for k in importers.FIELDS}
    score = {k: sum(f in guesses[k] for f in required[k]) / len(required[k]) for k in guesses}
    kind = max(score, key=score.get) if headers else "unknown"
    return json.dumps({"kind": kind, "headers": headers, "rows": len(rows), "guesses": guesses,
                       "preview": rows[:3]})


def fields() -> str:
    return json.dumps({k: [{"field": f, "label": lab, "required": req} for f, lab, req, _ in v]
                       for k, v in importers.FIELDS.items()})


def loc_values(path: str, column: str) -> str:
    _, rows = importers.read_csv(Path(path).read_text(errors="replace"))
    return json.dumps([{"value": v, "count": n, "guess": importers.guess_loc(v)}
                       for v, n in importers.distinct_values(rows, column)])


def prepare(spec_json: str) -> str:
    """Normalize the uploads into the engine's standard files.

    spec: {"claims": {path, mapping, loc_map, default_site}, "payments": [...], "sca": [...],
           "era": [paths]}
    """
    spec = json.loads(spec_json)
    if DATA.exists():
        shutil.rmtree(DATA)
    (DATA / "era").mkdir(parents=True)
    problems: list[str] = []

    c = spec["claims"]
    claims, p = importers.normalize_claims(Path(c["path"]).read_text(errors="replace"), c["mapping"],
                                           c.get("loc_map"), c.get("default_site") or "All sites")
    problems += [f"Claims: {x}" for x in p]
    importers.write_rows(DATA / "claims.csv", claims)

    payments = []
    for f in spec.get("payments", []):
        rows, p = importers.normalize_payments(Path(f["path"]).read_text(errors="replace"), f["mapping"])
        payments += rows
        problems += [f"Patient payments: {x}" for x in p]
    if payments:
        importers.write_rows(DATA / "patient_payments.csv", payments)

    scas = []
    for f in spec.get("sca", []):
        rows, p = importers.normalize_sca(Path(f["path"]).read_text(errors="replace"), f["mapping"],
                                          c.get("loc_map"))
        scas += rows
        problems += [f"SCAs: {x}" for x in p]
    if scas:
        importers.write_rows(DATA / "sca_contracts.csv", scas)

    for i, path in enumerate(spec.get("era", [])):
        shutil.copy(path, DATA / "era" / f"{i:05d}.835")
    if str(c["path"]).startswith(str(UPLOADS / "sample")):
        (DATA / "SAMPLE").write_text("built from the sample files")

    first, last = importers.data_range(claims)
    months = []
    if first:
        earliest = month_end(date.fromisoformat(first) + timedelta(days=MIN_HISTORY_DAYS))
        latest = date.fromisoformat(last)
        latest = latest if latest == month_end(latest) else prior_month_end(latest)
        m = latest
        while m >= earliest and len(months) < 24:
            months.append(m.isoformat())
            m = prior_month_end(m)
    return json.dumps({"claims": len(claims), "payments": len(payments), "sca": len(scas),
                       "era_files": len(spec.get("era", [])), "first": first, "last": last,
                       "months": months, "problems": problems[:50], "problem_count": len(problems)})


def run(as_of: str, backtest_months: int = 6) -> str:
    if OUT.exists():
        shutil.rmtree(OUT)
    report = pipeline.run(DATA, date.fromisoformat(as_of), OUT, use_claude=False,
                          backtest_months=backtest_months)
    report["synthetic"] = (DATA / "SAMPLE").exists()
    (OUT / "dashboard.html").write_text(_standalone(dashboard.render(report)))
    f = report["facts"]
    return json.dumps({
        "month": report["month"],
        "summary": {"net_revenue": f["net_revenue"], "gross_charges": f["gross_charges"],
                    "accrual_rate": f["accrual_rate"], "true_up": f["true_up"], "net_ar": f["net_ar"]},
        "matched": report["counts"], "unmatched": len(report["unmatched"]),
        "files": sorted(p.name for p in OUT.iterdir()),
    })


def _standalone(html: str) -> str:
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n</head>\n<body>\n"
            + html + "\n</body>\n</html>\n")


# ------------------------------------------------------------- sample files

def make_samples(seed: int = 7) -> str:
    """Write synthetic files shaped like billing-system exports into /uploads/sample."""
    tmp = Path("/work/synth")
    if tmp.exists():
        shutil.rmtree(tmp)
    synth.generate(tmp, seed=seed)
    dest = UPLOADS / "sample"
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "era").mkdir(parents=True)

    def us(d: str) -> str:
        y, m, dd = d.split("-")
        return f"{m}/{dd}/{y}"

    with open(tmp / "claims.csv", newline="") as f:
        claims = list(csv.DictReader(f))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Claim ID", "Patient Account", "Insurance ID", "Primary Insurance", "From DOS", "To DOS",
                "Rev Code/CPT", "Facility", "Units", "Total Charges"])
    for c in claims:
        w.writerow([c["claim_id"], c["patient_id"], c["member_id"], c["payer"], us(c["service_from"]),
                    us(c["service_to"]), c["service_code"].split(":")[1], c["site"], c["units"],
                    f"${float(c['billed']):,.2f}"])
    (dest / "collaboratemd_claims_export.csv").write_text(buf.getvalue())

    with open(tmp / "patient_payments.csv", newline="") as f:
        pays = list(csv.DictReader(f))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Claim #", "Patient Account", "Payment Date", "Payment Amount"])
    for p in pays:
        w.writerow([p["claim_id"], p["patient_id"], us(p["date"]), f"${float(p['amount']):,.2f}"])
    (dest / "patient_payments.csv").write_text(buf.getvalue())

    with open(tmp / "sca_contracts.csv", newline="") as f:
        scas = list(csv.DictReader(f))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Agreement", "Payer", "Patient Account", "Program", "Per Diem", "Start Date"])
    names = {"RTC": "Residential", "PHP": "Partial Hospitalization"}
    for s in scas:
        w.writerow([s["sca_id"], s["payer"], s["patient_id"], names.get(s["level_of_care"], s["level_of_care"]),
                    s["per_diem"], us(s["effective_from"])])
    (dest / "single_case_agreements.csv").write_text(buf.getvalue())

    era = []
    for p in sorted((tmp / "era").glob("*.835")):
        target = dest / "era" / p.name
        shutil.copy(p, target)
        era.append(str(target))
    return json.dumps({"csv": [str(dest / n) for n in ("collaboratemd_claims_export.csv",
                                                       "patient_payments.csv", "single_case_agreements.csv")],
                       "era": era})
