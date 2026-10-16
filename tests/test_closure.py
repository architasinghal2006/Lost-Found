import pytest

import claim_views
import claims
import events
import items
import transitions
from conftest import TRUTH, answer_all, budget


def verified_claim(conn, world, user="owner"):
    claim_id = claims.open_claim(conn, world["item"], world[user])
    assert answer_all(conn, claim_id, world[user], TRUTH.get) == "VERIFIED"
    return claim_id


def item_row(conn, world):
    return conn.execute("SELECT * FROM items WHERE id=?", (world["item"],)).fetchone()


def test_one_confirmation_does_not_close(conn, world):
    # FR-104
    claim_id = verified_claim(conn, world)
    assert claims.confirm_handover(conn, claim_id, world["owner"]) is False
    assert item_row(conn, world)["status"] == "active"


def test_both_confirmations_close_item(conn, world):
    claim_id = verified_claim(conn, world)
    claims.confirm_handover(conn, claim_id, world["owner"])
    assert claims.confirm_handover(conn, claim_id, world["finder"]) is True
    row = item_row(conn, world)
    assert (row["status"], row["closure_reason"]) == ("resolved", "HANDED_OVER")
    assert row["resolved_at"]
    assert claim_views.get_claim(conn, claim_id)["state"] == "CLOSED"


def test_admin_can_confirm_for_finder_but_never_for_claimant(conn, world):
    claim_id = verified_claim(conn, world)
    claims.confirm_handover(conn, claim_id, world["admin"])
    claims.confirm_handover(conn, claim_id, world["admin"])
    assert item_row(conn, world)["status"] == "active"
    assert claims.confirm_handover(conn, claim_id, world["owner"]) is True


def test_strangers_cannot_confirm(conn, world):
    claim_id = verified_claim(conn, world)
    with pytest.raises(transitions.NotAuthorised):
        claims.confirm_handover(conn, claim_id, world["fraud"])


def test_handover_needs_a_verified_claim(conn, world):
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    with pytest.raises(claims.ClaimError):
        claims.confirm_handover(conn, claim_id, world["owner"])


def test_repeated_confirmation_logs_once(conn, world):
    # NFR-SEC-009
    claim_id = verified_claim(conn, world)
    claims.confirm_handover(conn, claim_id, world["owner"])
    claims.confirm_handover(conn, claim_id, world["owner"])
    count = conn.execute("SELECT COUNT(*) FROM item_events WHERE event_type='handover_confirmed'").fetchone()[0]
    assert count == 1


def test_closure_cascades_other_claims_and_freezes_budgets(conn, world):
    # FR-062, FR-097, INV-1
    winner = verified_claim(conn, world)
    pending = claims.open_claim(conn, world["item"], world["fraud"])
    claims.confirm_handover(conn, winner, world["owner"])
    claims.confirm_handover(conn, winner, world["finder"])
    assert claim_views.get_claim(conn, pending)["state"] == "CLOSED_ITEM_RESOLVED"
    item_budget = budget(conn, "item_id", world["item"])
    assert (item_budget["frozen"], item_budget["frozen_value"]) == (1, 10)
    assert budget(conn, "claim_id", winner)["frozen"] == 1
    with pytest.raises(claims.ClaimError):
        claims.start_verification(conn, pending, world["fraud"])


def test_closed_item_refuses_new_claims_and_updates(conn, world):
    # FR-021
    items.withdraw_item(conn, world["item"], world["finder"], "WITHDRAWN")
    with pytest.raises(claims.ClaimError):
        claims.open_claim(conn, world["item"], world["owner"])
    attribute_id = claims.attribute_lock_status(conn, world["item"])[0]["id"]
    with pytest.raises(claims.ClaimError):
        claims.replace_attribute(conn, world["item"], attribute_id, world["finder"], "Q?", "a", 2)


def test_closure_refused_while_admin_decision_pending(conn, world):
    # D-9: INV-1 cannot hold otherwise, since T10 does not cover DISPUTED/MANUAL_REVIEW
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    answer_all(conn, claim_id, world["owner"], lambda q: TRUTH[q] if q == "What colour is the strap?" else "?")
    with pytest.raises(items.ItemError, match="administrator"):
        items.withdraw_item(conn, world["item"], world["finder"], "WITHDRAWN")
    assert item_row(conn, world)["status"] == "active"


def test_closure_is_atomic(conn, world, monkeypatch):
    # FR-101: a failure in the budget-freeze step rolls back the status change and cascade
    pending = claims.open_claim(conn, world["item"], world["owner"])

    def broken(*args):
        raise RuntimeError("disk full")

    monkeypatch.setattr(claims, "_freeze_budgets", broken)
    with pytest.raises(RuntimeError):
        items.withdraw_item(conn, world["item"], world["finder"], "WITHDRAWN")
    assert item_row(conn, world)["status"] == "active"
    assert claim_views.get_claim(conn, pending)["state"] == "PENDING"


def test_event_write_failure_rolls_back_closure(conn, world, monkeypatch):
    # FR-133
    original = events.log_event

    def failing(conn_, item_id, event_type, *args, **kwargs):
        if event_type == "item_closed":
            raise RuntimeError("log failed")
        return original(conn_, item_id, event_type, *args, **kwargs)

    monkeypatch.setattr(events, "log_event", failing)
    with pytest.raises(RuntimeError):
        items.withdraw_item(conn, world["item"], world["finder"], "WITHDRAWN")
    assert item_row(conn, world)["status"] == "active"


def test_reopen_is_admin_only_with_justification(conn, world):
    # FR-113
    items.withdraw_item(conn, world["item"], world["finder"], "WITHDRAWN")
    with pytest.raises(transitions.NotAuthorised):
        claims.reopen_item(conn, world["item"], world["finder"], "mistake")
    with pytest.raises(claims.ClaimError):
        claims.reopen_item(conn, world["item"], world["admin"], "")
    assert claims.reopen_item(conn, world["item"], world["admin"], "Closed by mistake") is True
    assert claims.reopen_item(conn, world["item"], world["admin"], "again") is False
    assert item_row(conn, world)["status"] == "active"


def test_reopen_restores_frozen_budget_not_a_fresh_one(conn, world):
    # FR-116, INV-6
    claim_id = claims.open_claim(conn, world["item"], world["fraud"])
    answer_all(conn, claim_id, world["fraud"], lambda q: "?")
    items.withdraw_item(conn, world["item"], world["finder"], "WITHDRAWN")
    claims.reopen_item(conn, world["item"], world["admin"], "Closed by mistake")
    item_budget = budget(conn, "item_id", world["item"])
    assert (item_budget["consumed_total"], item_budget["frozen"]) == (10, 0)
    second = claims.open_claim(conn, world["item"], world["owner"])
    claims.start_verification(conn, second, world["owner"])
    assert budget(conn, "item_id", world["item"])["consumed_total"] == 15


def test_item_consumption_equals_sum_of_issued_costs(conn, world):
    # INV-2
    for name, answer in [("fraud", lambda q: "?"), ("owner", TRUTH.get)]:
        claim_id = claims.open_claim(conn, world["item"], world[name])
        answer_all(conn, claim_id, world[name], answer)
    issued = conn.execute("SELECT SUM(disclosure_cost) FROM challenges").fetchone()[0]
    assert budget(conn, "item_id", world["item"])["consumed_total"] == issued == 20
