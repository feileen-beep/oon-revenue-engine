from datetime import date

from oonrev.close import run_close
from oonrev.ledger import Claim, Event
from oonrev.memo import build_facts, template_memo, unverified_numbers
from oonrev.worklists import denial_trend, open_denials, underpayments

from conftest import AS_OF


def test_every_remit_balances_and_matches(ledger):
    assert all(r.is_balanced() for r in ledger.remits)
    assert not ledger.unmatched
    assert ledger.match_log, "generator plants a few mangled claim numbers"
    assert {m["method"] for m in ledger.match_log} <= {"normalized claim number", "member + dates + amount"}


def test_claim_state_lifecycle():
    c = Claim("C1", "P1", "M1", "Aetna", "RTC", "Phoenix", date(2026, 1, 1), date(2026, 1, 7), 7, 10000.0,
              date(2026, 1, 10))
    c.events = [Event(date(2026, 2, 1), "denied", carc="197"),
                Event(date(2026, 5, 1), "paid", cash=2500.0)]
    assert c.state(date(2026, 1, 31)).status == "pending"
    s = c.state(date(2026, 3, 1))
    assert (s.status, s.denial_carc, s.denial_date) == ("denied", "197", date(2026, 2, 1))
    s = c.state(date(2026, 6, 1))
    assert s.status == "paid" and s.finalized and s.collected == 2500.0
    c2 = Claim("C2", "P1", "M1", "Aetna", "RTC", "Phoenix", date(2025, 1, 1), date(2025, 1, 7), 7, 10000.0,
               date(2025, 1, 10))
    assert c2.state(date(2026, 3, 1)).status == "written_off"


def test_balances_roll_forward_exactly(close):
    for name, parts in close.rollforward().items():
        moved = sum(v for k, v in parts.items() if k != "closing")
        assert abs(moved - parts["closing"]) < 0.01, name


def test_journal_entry_balances(close):
    rows = close.journal_entry()
    debits = sum(float(r["Debits"] or 0) for r in rows)
    credits = sum(float(r["Credits"] or 0) for r in rows)
    assert abs(debits - credits) < 0.01
    assert {r["Class"] for r in rows} <= {"RTC", "PHP", "IOP", "OP", "TMS"}
    assert abs(debits - credits) < 0.01 and debits > 0


def test_net_revenue_equals_change_in_expected_value(close):
    now = sum(v.expected for v in close.now.values())
    before = sum(v.expected for v in close.before.values())
    assert abs((now - before) - close.net_revenue) < 0.01


def test_model_detects_the_two_storylines(close):
    exp = close.tables.experience
    assert exp[("Aetna", "PHP")]["factor"] < 0.9      # MultiPlan repricing from May 2026
    assert exp[("Cigna", "RTC")]["factor"] < 0.95     # authorization denials from June 2026
    cigna = [r for r in denial_trend(close) if r["payer"] == "Cigna"][-1]
    assert cigna["top_carc"] == "197"


def test_worklists(close):
    under = underpayments(close)
    assert under and all(r["allowed"] < r["expected_allowed"] for r in under)
    assert any(r["repricer"] == "MULTIPLAN" for r in under)
    assert any("single case agreement" in r["reason"] for r in under)
    assert all(r["days_left"] == (date.fromisoformat(r["appeal_deadline"]) - AS_OF).days for r in open_denials(close))


def test_memo_numbers_are_traceable(close, ledger):
    prior = run_close(ledger, close.prior)
    facts = build_facts(close, underpayments(close), open_denials(close), denial_trend(close), prior)
    memo = template_memo(facts)
    assert unverified_numbers(memo, facts) == []
    assert unverified_numbers(memo + "\nRevenue rose 41.7% to $9.99M.", facts) == ["41.7%", "$9.99M"]
