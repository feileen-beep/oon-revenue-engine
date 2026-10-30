"""The owner's one-page summary: what changed this month and why.

The code computes every number. Claude only writes the prose, and every number
in Claude's draft is checked against the computed facts before the draft is
used. If a number cannot be traced back, or no API key is set, the memo falls
back to a plain template built from the same facts.
"""

from __future__ import annotations

import json
import os
import re

from .close import CloseResult, rate_table_rows, true_up_drivers
from .reference import LOC_NAMES


def _m(x: float) -> str:
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e6:
        return f"{sign}${x / 1e6:.2f}M"
    if x >= 1e3:
        return f"{sign}${x / 1e3:.0f}K"
    return f"{sign}${x:.0f}"


def _month(ym: str) -> str:
    from datetime import date
    y, m = map(int, ym.split("-"))
    return date(y, m, 1).strftime("%B %Y")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def build_facts(res: CloseResult, underpay: list[dict], denials: list[dict], trend: list[dict],
                prior: CloseResult | None = None) -> dict:
    charges = res.total("charges")
    accrual_rate = res.total("new_expected") / charges if charges else 0.0
    rf = res.rollforward()
    facts = {
        "month": res.as_of.strftime("%B %Y"),
        "gross_charges": charges,
        "net_revenue": res.net_revenue,
        "accrual_rate": accrual_rate,
        "true_up": res.total("true_up"),
        "cash_collected": res.total("cash"),
        "expected_cash_on_open_claims": res.total("expected_cash"),
        "actual_cash_on_open_claims": res.total("cash_on_prior"),
        "cash_vs_expected": res.total("cash_on_prior") - res.total("expected_cash"),
        "net_ar": rf["net_ar"]["closing"],
        "net_ar_change": rf["net_ar"]["closing"] - rf["net_ar"]["opening"],
        "underpayment_count": len(underpay),
        "underpayment_recovery": sum(r["est_recovery"] for r in underpay),
        "open_denials": len(denials),
        "open_denials_billed": sum(r["billed"] for r in denials),
        "denials_due_14_days": sum(1 for r in denials if 0 <= r["days_left"] <= 14),
        "denials_past_deadline": sum(1 for r in denials if r["days_left"] < 0),
    }
    if prior:
        pc = prior.total("charges")
        facts["prior_net_revenue"] = prior.net_revenue
        facts["prior_accrual_rate"] = prior.total("new_expected") / pc if pc else 0.0

    # Accrual-rate movers: weighted by this month's charges so small cells don't dominate.
    charges_by = {}
    for v in res.now.values():
        if v.claim.claim_id not in res.before:
            key = (v.claim.payer, v.claim.loc)
            charges_by[key] = charges_by.get(key, 0.0) + v.claim.billed
    movers = []
    for r in rate_table_rows(res):
        key = (r["payer"], r["loc"])
        delta = r["accrual_rate"] - r["prior_accrual_rate"]
        impact = delta * charges_by.get(key, 0.0)
        movers.append({"payer": r["payer"], "level_of_care": LOC_NAMES[r["loc"]],
                       "accrual_rate": r["accrual_rate"], "prior_accrual_rate": r["prior_accrual_rate"],
                       "historical_rate": r["historical_rate"],
                       "recent_experience_factor": r["experience_factor"],
                       "recent_payment_gap": abs(1 - r["experience_factor"]), "revenue_impact": impact})
    movers.sort(key=lambda m: abs(m["revenue_impact"]), reverse=True)
    facts["rate_movers"] = movers[:3]

    # Segments where recent payments have moved the accrual away from history.
    adjusted = []
    for r in rate_table_rows(res):
        key = (r["payer"], r["loc"])
        effect = (r["accrual_rate"] - r["historical_rate"]) * charges_by.get(key, 0.0)
        if abs(r["experience_factor"] - 1) >= 0.08 and r["experience_n"] >= 40:
            adjusted.append({"payer": r["payer"], "level_of_care": LOC_NAMES[r["loc"]],
                             "historical_rate": r["historical_rate"], "accrual_rate": r["accrual_rate"],
                             "recent_experience_factor": r["experience_factor"],
                             "recent_payment_gap": abs(1 - r["experience_factor"]), "revenue_effect": effect})
    adjusted.sort(key=lambda a: abs(a["revenue_effect"]), reverse=True)
    facts["experience_adjustments"] = adjusted[:3]

    drivers = true_up_drivers(res)
    facts["true_up_drivers"] = [
        {"payer": d["payer"], "level_of_care": LOC_NAMES[d["loc"]], "total": d["total"],
         **{k: v for k, v in d.items() if k not in ("payer", "loc", "total")}}
        for d in drivers[:3]
    ]

    # Denial-rate change by payer: latest month vs. the average of the three before it.
    by_payer: dict[str, list] = {}
    for row in trend:
        by_payer.setdefault(row["payer"], []).append(row)
    spikes = []
    for payer, rows in by_payer.items():
        rows.sort(key=lambda r: r["month"])
        if len(rows) < 4:
            continue
        latest, base = rows[-1], rows[-4:-1]
        base_rate = sum(r["denied"] for r in base) / max(1, sum(r["remits"] for r in base))
        spikes.append({"payer": payer, "month": latest["month"], "denial_rate": latest["denial_rate"],
                       "baseline_rate": base_rate, "top_reason": latest["top_reason"],
                       "denied_billed": latest["denied_billed"]})
    spikes.sort(key=lambda s: s["denial_rate"] - s["baseline_rate"], reverse=True)
    facts["denial_changes"] = spikes[:2]
    return facts


def template_memo(f: dict) -> str:
    lines = [f"# {f['month']} revenue and collections summary", ""]
    head = (f"Net patient revenue was {_m(f['net_revenue'])} on {_m(f['gross_charges'])} of gross charges, "
            f"an accrual rate of {_pct(f['accrual_rate'])}")
    if "prior_accrual_rate" in f:
        head += f" (prior month {_pct(f['prior_accrual_rate'])})"
    head += (f". Prior-period estimates moved by {_m(f['true_up'])}. Cash collected was {_m(f['cash_collected'])}, "
             f"and net AR closed at {_m(f['net_ar'])}, {'up' if f['net_ar_change'] >= 0 else 'down'} "
             f"{_m(abs(f['net_ar_change']))} for the month.")
    lines += [head, "", "## What changed"]
    for a in f.get("experience_adjustments", []):
        direction = "below" if a["recent_experience_factor"] < 1 else "above"
        lines.append(f"- {a['payer']} {a['level_of_care']} claims from recent months are paying "
                     f"{_pct(a['recent_payment_gap'])} {direction} the historical pattern. "
                     f"The accrual rate is {_pct(a['accrual_rate'])} instead of the historical "
                     f"{_pct(a['historical_rate'])}, a {_m(a['revenue_effect'])} effect on this month's revenue.")
    explained = {(a["payer"], a["level_of_care"]) for a in f.get("experience_adjustments", [])}
    for m in f["rate_movers"]:
        if abs(m["revenue_impact"]) < 1000 or (m["payer"], m["level_of_care"]) in explained:
            continue
        direction = "fell" if m["accrual_rate"] < m["prior_accrual_rate"] else "rose"
        why = ""
        if m["recent_experience_factor"] < 0.95:
            why = (f" Recent claims are paying {_pct(m['recent_payment_gap'])} below the historical "
                   f"pattern, so the model lowered the rate.")
        elif m["recent_experience_factor"] > 1.05:
            why = (f" Recent claims are paying {_pct(m['recent_payment_gap'])} above the historical "
                   f"pattern, so the model raised the rate.")
        lines.append(f"- {m['payer']} {m['level_of_care']} accrual rate {direction} from "
                     f"{_pct(m['prior_accrual_rate'])} to {_pct(m['accrual_rate'])}, "
                     f"a {_m(m['revenue_impact'])} effect on this month's revenue.{why}")
    for d in f["denial_changes"]:
        if d["denial_rate"] - d["baseline_rate"] < 0.03:
            continue
        lines.append(f"- {d['payer']} denied {_pct(d['denial_rate'])} of claims it processed in {_month(d['month'])}, "
                     f"up from {_pct(d['baseline_rate'])} over the prior three months. The leading reason was "
                     f"\"{d['top_reason']}\" ({_m(d['denied_billed'])} billed).")
    for d in f["true_up_drivers"]:
        parts = sorted(((k, v) for k, v in d.items() if k not in ("payer", "level_of_care", "total")),
                       key=lambda kv: kv[1])
        main = parts[0][0] if parts else "activity"
        lines.append(f"- {d['payer']} {d['level_of_care']} prior-period estimate changed by {_m(d['total'])}, "
                     f"mostly from {main}.")
    lines += ["", "## Cash", f"- Open claims at last month-end were expected to bring in "
              f"{_m(f['expected_cash_on_open_claims'])} this month; they brought in "
              f"{_m(f['actual_cash_on_open_claims'])} ({_m(f['cash_vs_expected'])} vs. expected)."]
    lines += ["", "## Actions",
              f"- Work {f['underpayment_count']} underpaid claims, about {_m(f['underpayment_recovery'])} recoverable.",
              f"- {f['open_denials']} open denials ({_m(f['open_denials_billed'])} billed); "
              f"{f['denials_due_14_days']} reach the appeal deadline within 14 days and "
              f"{f['denials_past_deadline']} are already past it."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- Claude draft

SYSTEM = """You write the monthly revenue summary for the owner of a behavioral health provider \
that bills commercial insurers out of network. The owner is busy and decides things from this page.

Write at most 250 words in Markdown: a title, one short opening paragraph with the headline numbers, \
then sections "What changed", "Cash" and "Actions" as short bullets. Explain causes in plain words \
(for example, MultiPlan repricing or missing authorizations). Use only the numbers given in the facts. \
Format dollars like $1.23M or $45K and rates like 23.4%. Do not compute new numbers, and do not \
round differently than the formatted values provided."""


def _formatted_facts(f: dict) -> dict:
    """Hand Claude pre-formatted values so it never needs to do arithmetic."""
    def fmt(k, v):
        if isinstance(v, dict):
            return {kk: fmt(kk, vv) for kk, vv in v.items()}
        if isinstance(v, list):
            return [fmt(k, x) for x in v]
        if isinstance(v, float):
            if "rate" in k or "factor" in k or "gap" in k:
                return _pct(v)
            return _m(v)
        return v
    return {k: fmt(k, v) for k, v in f.items()}


_NUM = re.compile(r"\$?-?\d[\d,]*\.?\d*\s*[MK%]?")


def _allowed_tokens(facts: dict) -> set[str]:
    text = json.dumps(_formatted_facts(facts))
    return {t.replace(" ", "").rstrip(".,") for t in _NUM.findall(text)}


def unverified_numbers(memo: str, facts: dict) -> list[str]:
    """Numbers in the memo that do not appear in the formatted facts."""
    allowed = _allowed_tokens(facts)
    bad = []
    for tok in _NUM.findall(memo):
        t = tok.replace(" ", "").rstrip(".,")
        plain = t.lstrip("$-").replace(",", "")
        if re.fullmatch(r"\d{1,2}", plain) or re.fullmatch(r"20\d\d", plain):
            continue  # day counts, list numbers, years
        if t in allowed or t.lstrip("-") in {a.lstrip("-") for a in allowed}:
            continue
        bad.append(t)
    return bad


def claude_memo(facts: dict, model: str = "claude-opus-5-5") -> tuple[str | None, str]:
    """Ask Claude for the narrative. Returns (memo or None, status note)."""
    try:
        import anthropic
    except ImportError:
        return None, "anthropic package not installed"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None, "no Anthropic credentials set"
    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=SYSTEM,
            messages=[{"role": "user", "content": "Facts for this month:\n" +
                       json.dumps(_formatted_facts(facts), indent=2)}],
        )
    except anthropic.APIStatusError as e:
        return None, f"API error {e.status_code}"
    except anthropic.APIConnectionError:
        return None, "could not reach the API"
    if response.stop_reason == "refusal":
        return None, "request declined"
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    bad = unverified_numbers(text, facts)
    if bad:
        return None, f"draft rejected: numbers not in the facts {bad[:5]}"
    return text + "\n", "written by Claude, every number verified against the computed facts"


def write_memo(facts: dict, use_claude: bool = True) -> tuple[str, str]:
    if use_claude:
        memo, note = claude_memo(facts)
        if memo:
            return memo, note
        return template_memo(facts), f"template memo ({note})"
    return template_memo(facts), "template memo"
