"""Command line: python -m oonrev <generate|close> ..."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from . import dashboard, pipeline, synth

PACKAGE = Path(__file__).parent


def build_site(docs: Path) -> list[str]:
    """Copy the package source next to docs/run.html, which loads it into Pyodide."""
    import json
    import shutil
    target = docs / "app" / "oonrev"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    files = sorted(p.name for p in PACKAGE.iterdir() if p.suffix in (".py", ".html"))
    for name in files:
        shutil.copy(PACKAGE / name, target / name)
    (docs / "app" / "manifest.json").write_text(json.dumps({"files": files}, indent=1) + "\n")
    # GitHub Pages runs Jekyll by default, which drops files starting with "_" (__init__.py).
    (docs / ".nojekyll").write_text("")
    # A single-file copy of the run page that works when opened straight from disk.
    blob = json.dumps({n: (PACKAGE / n).read_text() for n in files}).replace("</", "<\\/")
    page = (docs / "run.html").read_text()
    marker = '<script src="https://cdn.jsdelivr.net/pyodide/'
    assert marker in page
    page = page.replace(marker, f"<script>window.OONREV_FILES = {blob};</script>\n{marker}", 1)
    (docs / "run_offline.html").write_text(page)
    return files


def main(argv=None):
    p = argparse.ArgumentParser(prog="oonrev", description="Out-of-network revenue and collections engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="create synthetic claims, 835 remits and patient payments")
    g.add_argument("--out", default="data")
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--as-of", default=synth.AS_OF.isoformat())

    c = sub.add_parser("close", help="run a month-end close and write the outputs")
    c.add_argument("--data", default="data")
    c.add_argument("--as-of", default=synth.AS_OF.isoformat(), help="month-end date, YYYY-MM-DD")
    c.add_argument("--out", default="out")
    c.add_argument("--dashboard", default="docs/index.html")
    c.add_argument("--no-claude", action="store_true", help="use the template memo even if an API key is set")
    c.add_argument("--backtest-months", type=int, default=15)

    s = sub.add_parser("site", help="copy the package into docs/app for the in-browser page (docs/run.html)")
    s.add_argument("--docs", default="docs")

    a = p.parse_args(argv)
    if a.cmd == "site":
        files = build_site(Path(a.docs))
        print(f"Copied {len(files)} files to {a.docs}/app/oonrev/")
    elif a.cmd == "generate":
        stats = synth.generate(Path(a.out), seed=a.seed, as_of=date.fromisoformat(a.as_of))
        print(f"{stats['claims']:,} claims, {stats['remits']:,} remits, {stats['patient_payments']:,} patient payments, "
              f"${stats['billed']:,.0f} billed -> {a.out}/")
    else:
        report = pipeline.run(Path(a.data), date.fromisoformat(a.as_of), Path(a.out),
                              use_claude=not a.no_claude, backtest_months=a.backtest_months)
        dashboard.write(report, Path(a.dashboard))
        f = report["facts"]
        print(f"{report['month']}: net revenue ${f['net_revenue']:,.0f} on ${f['gross_charges']:,.0f} charges "
              f"({f['accrual_rate']:.1%}), true-up ${f['true_up']:,.0f}, net AR ${f['net_ar']:,.0f}")
        print(f"Memo: {report['memo_note']}")
        print(f"Outputs in {a.out}/, dashboard at {a.dashboard}")


if __name__ == "__main__":
    main()
