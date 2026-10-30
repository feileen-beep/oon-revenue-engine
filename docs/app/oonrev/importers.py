"""Turn exports from a billing system (CollaborateMD, Kipu, a spreadsheet) into
the engine's standard input files.

Every export names its columns differently, so each file comes with a column
mapping: {standard field: column in the export}. `guess_mapping` proposes one
from the header row, and the person confirms or corrects it. Level of care is
mapped by value, because exports carry it as text, a revenue code or a CPT code.

Exports that list one row per charge line are rolled up to one row per claim.
"""

from __future__ import annotations

import csv
import io
import re
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path

from .reference import LEVELS_OF_CARE

# field, label, required, header aliases (compared after lowercasing and removing non-letters)
CLAIM_FIELDS = [
    ("claim_id", "Claim number sent to the payer", True,
     ["claimid", "claimnumber", "claimno", "claim", "patientcontrolnumber", "pcn", "patientaccountnumber",
      "accountnumber", "encounterid", "chargeid"]),
    ("payer", "Payer / insurance", True,
     ["payer", "payername", "insurance", "insurancename", "primaryinsurance", "carrier", "plan", "insurancecompany"]),
    ("service_from", "Service date (from)", True,
     ["servicefrom", "dosfrom", "fromdate", "fromdos", "dateofservice", "dos", "servicedate", "startdate",
      "servicestartdate"]),
    ("service_to", "Service date (to)", False,
     ["serviceto", "dosto", "todate", "todos", "throughdate", "enddate", "serviceenddate"]),
    ("billed", "Billed charges", True,
     ["billed", "charges", "charge", "chargeamount", "totalcharges", "billedamount", "amount", "totalcharge", "fee"]),
    ("level_of_care", "Level of care (or revenue / CPT code)", True,
     ["levelofcare", "loc", "program", "servicetype", "revenuecode", "revcode", "cpt", "cptcode", "procedurecode",
      "procedure", "hcpcs", "code"]),
    ("member_id", "Member / subscriber ID", False,
     ["memberid", "subscriberid", "insuredid", "policynumber", "policyid", "insuranceid", "member"]),
    ("patient_id", "Patient ID / MRN", False,
     ["patientid", "mrn", "patientnumber", "chartnumber", "patientaccount", "patient"]),
    ("site", "Location / facility", False,
     ["site", "location", "facility", "servicelocation", "facilityname", "placeofservice", "office"]),
    ("units", "Units / days", False, ["units", "days", "qty", "quantity", "unit"]),
    ("sca_id", "Single case agreement ID", False, ["scaid", "sca", "singlecaseagreement", "agreementid"]),
]

PAYMENT_FIELDS = [
    ("claim_id", "Claim number", True, CLAIM_FIELDS[0][3]),
    ("date", "Payment date", True, ["date", "paymentdate", "paiddate", "depositdate", "postdate", "posteddate", "received"]),
    ("amount", "Amount", True, ["amount", "payment", "paymentamount", "paid", "paidamount", "total"]),
    ("patient_id", "Patient ID", False, CLAIM_FIELDS[7][3]),
]

SCA_FIELDS = [
    ("sca_id", "Agreement ID", False, ["scaid", "sca", "agreementid", "id", "agreement"]),
    ("payer", "Payer", True, CLAIM_FIELDS[1][3]),
    ("patient_id", "Patient ID / MRN", True, CLAIM_FIELDS[7][3]),
    ("level_of_care", "Level of care", True, ["levelofcare", "loc", "program"]),
    ("per_diem", "Rate per day", True, ["perdiem", "rate", "dailyrate", "rateperday", "agreedrate", "amount"]),
    ("effective_from", "Effective from", False, ["effectivefrom", "startdate", "effective", "from"]),
]

FIELDS = {"claims": CLAIM_FIELDS, "payments": PAYMENT_FIELDS, "sca": SCA_FIELDS}


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def read_csv(text: str) -> tuple[list[str], list[dict]]:
    text = text.lstrip("﻿")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = [r for r in reader if any((v or "").strip() for v in r.values())]
    return [h for h in (reader.fieldnames or []) if h is not None], rows


def guess_mapping(headers: list[str], kind: str = "claims") -> dict[str, str]:
    """Propose {field: header}. Exact alias matches win, then aliases contained in the header."""
    keyed = {h: _key(h) for h in headers}
    used: set[str] = set()
    out: dict[str, str] = {}
    for field, _, _, aliases in FIELDS[kind]:
        pick = next((h for alias in [field.replace("_", "")] + aliases for h, k in keyed.items()
                     if k == alias and h not in used), None)
        if pick is None:
            pick = next((h for alias in aliases if len(alias) > 3 for h, k in keyed.items()
                         if alias in k and h not in used), None)
        if pick:
            out[field] = pick
            used.add(pick)
    return out


DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y%m%d", "%d-%b-%Y", "%b %d, %Y", "%Y/%m/%d"]


def parse_date(s: str) -> date:
    s = (s or "").strip()
    s = re.split(r"[ T](?=\d{1,2}:)", s)[0]  # drop a time part
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognized date {s!r}")


def parse_money(s: str) -> float:
    s = (s or "").strip()
    if not s:
        return 0.0
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    num = re.sub(r"[^0-9.]", "", s)
    if not num:
        raise ValueError(f"unrecognized amount {s!r}")
    return -float(num) if neg else float(num)


_LOC_PATTERNS = [
    ("TMS", r"\btms\b|transcranial|^9086[789]$"),
    ("IOP", r"\biop\b|intensive\s*out|^0?90[56]$|^s9480$|^h0015$"),
    ("PHP", r"\bphp\b|partial|day\s*treat|^0?91[23]$|^h0035$|^s0201$"),
    ("RTC", r"\brtc\b|residential|^0?100[0-9]$|^h001[789]$|^h2036$|detox|inpatient"),
    ("OP", r"\bop\b|outpatient|therapy|psychotherapy|^908\d\d$|^9083\d$|^99\d\d\d$|^h0004$"),
]


def guess_loc(value: str) -> str:
    v = (value or "").strip().lower()
    for loc, pat in _LOC_PATTERNS:
        if re.search(pat, v):
            return loc
    return ""


def distinct_values(rows: list[dict], column: str, limit: int = 60) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for r in rows:
        v = (r.get(column) or "").strip()
        counts[v] = counts.get(v, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]


def _get(row, mapping, field):
    col = mapping.get(field)
    return (row.get(col) or "").strip() if col else ""


def normalize_claims(text: str, mapping: dict, loc_map: dict | None = None, default_site: str = "All sites"):
    """Return (standard claim rows, list of problems)."""
    _, rows = read_csv(text)
    loc_map = loc_map or {}
    claims: "OrderedDict[str, dict]" = OrderedDict()
    problems: list[str] = []
    skipped_loc = 0
    for i, r in enumerate(rows, start=2):
        cid = _get(r, mapping, "claim_id")
        if not cid:
            problems.append(f"row {i}: no claim number")
            continue
        raw_loc = _get(r, mapping, "level_of_care")
        loc = loc_map.get(raw_loc) or guess_loc(raw_loc)
        if loc not in LEVELS_OF_CARE:
            skipped_loc += 1
            continue
        try:
            d_from = parse_date(_get(r, mapping, "service_from"))
            d_to = parse_date(_get(r, mapping, "service_to")) if _get(r, mapping, "service_to") else d_from
            billed = parse_money(_get(r, mapping, "billed"))
        except ValueError as e:
            problems.append(f"row {i}: {e}")
            continue
        units_raw = _get(r, mapping, "units")
        units = int(float(units_raw)) if re.fullmatch(r"\d+(\.\d+)?", units_raw or "") else 1
        member = _get(r, mapping, "member_id")
        c = claims.get(cid)
        if c is None:
            claims[cid] = {
                "claim_id": cid, "patient_id": _get(r, mapping, "patient_id") or member or cid,
                "member_id": member, "payer": _get(r, mapping, "payer") or "Unknown payer",
                "payer_id": "", "level_of_care": loc, "site": _get(r, mapping, "site") or default_site,
                "service_from": d_from, "service_to": d_to, "units": units, "billed": billed,
                "service_code": raw_loc, "submit_date": d_to, "sca_id": _get(r, mapping, "sca_id"),
                "sca_per_diem": "",
            }
        else:  # another charge line on the same claim
            c["service_from"] = min(c["service_from"], d_from)
            c["service_to"] = max(c["service_to"], d_to)
            c["units"] += units
            c["billed"] += billed
    if skipped_loc:
        problems.append(f"{skipped_loc} rows skipped: level of care not mapped")
    out = []
    for c in claims.values():
        c = dict(c)
        for k in ("service_from", "service_to", "submit_date"):
            c[k] = c[k].isoformat()
        c["billed"] = f"{c['billed']:.2f}"
        out.append(c)
    return out, problems


def normalize_payments(text: str, mapping: dict):
    _, rows = read_csv(text)
    out, problems = [], []
    for i, r in enumerate(rows, start=2):
        try:
            out.append({"payment_id": f"PP{i:06d}", "claim_id": _get(r, mapping, "claim_id"),
                        "patient_id": _get(r, mapping, "patient_id"),
                        "date": parse_date(_get(r, mapping, "date")).isoformat(),
                        "amount": f"{parse_money(_get(r, mapping, 'amount')):.2f}", "note": "imported"})
        except ValueError as e:
            problems.append(f"row {i}: {e}")
    return out, problems


def normalize_sca(text: str, mapping: dict, loc_map: dict | None = None):
    _, rows = read_csv(text)
    loc_map = loc_map or {}
    out, problems = [], []
    for i, r in enumerate(rows, start=2):
        raw = _get(r, mapping, "level_of_care")
        loc = loc_map.get(raw) or guess_loc(raw)
        try:
            eff = _get(r, mapping, "effective_from")
            out.append({"sca_id": _get(r, mapping, "sca_id") or f"SCA-{i:04d}",
                        "payer": _get(r, mapping, "payer"), "patient_id": _get(r, mapping, "patient_id"),
                        "level_of_care": loc, "per_diem": f"{parse_money(_get(r, mapping, 'per_diem')):.2f}",
                        "effective_from": parse_date(eff).isoformat() if eff else ""})
        except ValueError as e:
            problems.append(f"row {i}: {e}")
    return out, problems


def write_rows(path: Path, rows: list[dict]):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)


def data_range(claim_rows: list[dict]) -> tuple[str, str]:
    dates = sorted(r["service_to"] for r in claim_rows)
    return (dates[0], dates[-1]) if dates else ("", "")
