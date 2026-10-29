"""Render the close report into a single self-contained HTML page."""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("dashboard_template.html")
DASHBOARD_LIMIT = 200  # rows per worklist embedded in the page; the CSVs hold everything


def render(report: dict) -> str:
    data = dict(report)
    data["list_counts"] = {k: len(report[k]) for k in
                           ("underpayments", "denials", "paid_to_patient", "match_exceptions", "unmatched")}
    for key in ("underpayments", "denials", "paid_to_patient", "match_exceptions", "unmatched"):
        data[key] = data[key][:DASHBOARD_LIMIT]
    payload = json.dumps(data, default=str, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.read_text().replace("/*__DATA__*/", payload)


def write(report: dict, path: Path, standalone: bool = True) -> Path:
    """Write the page. Standalone adds the document skeleton for GitHub Pages."""
    html = render(report)
    if standalone:
        html = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
                "</head>\n<body>\n" + html + "\n</body>\n</html>\n")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html)
    return path
