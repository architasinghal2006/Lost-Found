import re

import pytest

import claim_views
import claims
import transitions
from conftest import TRUTH, answer_all, budget, register


def right(question):
    return TRUTH[question]


def wrong(question):
    return "no idea"


def test_user_can_claim_item_they_did_not_register(conn, world):
    # FR-050
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    assert claim_views.get_claim(conn, claim_id)["state"] == "PENDING"
    assert budget(conn, "claim_id", claim_id)["allowance"] == claims.CLAIM_BUDGET


def test_finder_and_admin_cannot_claim(conn, world):
    for user in (world["finder"], world["admin"]):
        with pytest.raises(claims.ClaimError):
            claims.open_claim(conn, world["item"], user)


def test_one_open_claim_per_claimant_per_item(conn, world):
    # FR-052
    claims.open_claim(conn, world["item"], world["owner"])
    with pytest.raises(claims.ClaimError):
        claims.open_claim(conn, world["item"], world["owner"])


def test_rejected_claimant_cannot_retry(conn, world):
    claim_id = claims.open_claim(conn, world["item"], world["fraud"])
    assert answer_all(conn, claim_id, world["fraud"], wrong) == "REJECTED"
    with pytest.raises(claims.ClaimError):
        claims.open_claim(conn, world["item"], world["fraud"])


def test_genuine_owner_is_verified(conn, world):
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    assert answer_all(conn, claim_id, world["owner"], right) == "VERIFIED"
    record = conn.execute("SELECT * FROM verification_records WHERE claim_id=?", (claim_id,)).fetchone()
    assert (record["score"], record["threshold_applied"], record["outcome"]) == (1.0, 0.75, "VERIFIED")


def test_questions_go_highest_affordable_weight_first(conn, world):
    # OI-13: weights 5,4,3,2,1 with a budget of 10 -> 5, 4, then 1 (3 and 2 no longer fit)
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    answer_all(conn, claim_id, world["owner"], right)
    costs = [r[0] for r in conn.execute("SELECT disclosure_cost FROM challenges WHERE claim_id=? ORDER BY id", (claim_id,))]
    assert costs == [5, 4, 1]


def test_challenges_come_from_private_attributes_only(conn, world):
    # FR-070, FR-071
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    answer_all(conn, claim_id, world["owner"], right)
    questions = {r[0] for r in conn.execute("SELECT question_text FROM challenges")}
    assert questions <= set(TRUTH)


def test_budgets_debited_at_issue_even_without_answer(conn, world):
    # FR-073, FR-082, FR-083
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    claims.start_verification(conn, claim_id, world["owner"])
    assert budget(conn, "claim_id", claim_id)["consumed_total"] == 5
    assert budget(conn, "item_id", world["item"])["consumed_total"] == 5
    assert claim_views.get_claim(conn, claim_id)["state"] == "VERIFYING"


def test_starting_twice_does_not_issue_a_second_question(conn, world):
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    claims.start_verification(conn, claim_id, world["owner"])
    claims.start_verification(conn, claim_id, world["owner"])
    assert budget(conn, "item_id", world["item"])["consumed_total"] == 5


def test_over_budget_debit_is_refused(conn, world):
    # FR-074, NFR-SEC-005: hard limit enforced in SQL
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    with pytest.raises(claims.ClaimError):
        claims._debit(conn, "claim_id", claim_id, claims.CLAIM_BUDGET + 1)


def test_fraudster_is_rejected(conn, world):
    claim_id = claims.open_claim(conn, world["item"], world["fraud"])
    assert answer_all(conn, claim_id, world["fraud"], wrong) == "REJECTED"


@pytest.mark.parametrize("known, expected", [
    ({"What colour is the strap?", "What is engraved on the lid?"}, "VERIFIED"),   # 9/10
    ({"What colour is the strap?"}, "DISPUTED"),                                   # 5/10
    ({"What is engraved on the lid?", "What colour is the cap?"}, "DISPUTED"),     # 5/10
    ({"What is engraved on the lid?"}, "REJECTED"),                                # 4/10
])
def test_verdict_bands_on_real_claims(conn, world, known, expected):
    # FR-078, FR-079, FR-064 (indeterminate -> admin)
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    state = answer_all(conn, claim_id, world["owner"], lambda q: TRUTH[q] if q in known else "?")
    assert state == expected


def test_near_miss_answers_still_match(conn, world):
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    typos = {"What colour is the strap?": " RED ", "What is engraved on the lid?": "ab  2024", "What colour is the cap?": "Blue"}
    assert answer_all(conn, claim_id, world["owner"], typos.get) == "VERIFIED"


def test_empty_answers_count_as_wrong(conn, world):
    # FR-081
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    assert answer_all(conn, claim_id, world["owner"], lambda q: "") == "REJECTED"


def test_logs_scores_but_never_answers(conn, world):
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    answer_all(conn, claim_id, world["owner"], right)
    details = " ".join(r[0] for r in conn.execute("SELECT detail FROM item_events"))
    assert "similarity" in details
    for answer in TRUTH.values():
        assert not re.search(r"\b" + re.escape(answer.lower()) + r"\b", details.lower())


def test_asked_attribute_is_locked_but_others_editable(conn, world):
    # FR-024
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    claims.start_verification(conn, claim_id, world["owner"])
    status = {row["weight"]: row for row in claims.attribute_lock_status(conn, world["item"])}
    assert status[5]["locked"] and not status[3]["locked"]
    with pytest.raises(claims.ClaimError):
        claims.replace_attribute(conn, world["item"], status[5]["id"], world["finder"], "New?", "new", 5)
    claims.replace_attribute(conn, world["item"], status[3]["id"], world["finder"], "Which brand?", "Nalgene", 3)


def test_only_finder_can_replace_attributes(conn, world):
    attribute_id = claims.attribute_lock_status(conn, world["item"])[0]["id"]
    with pytest.raises(claims.ClaimError):
        claims.replace_attribute(conn, world["item"], attribute_id, world["owner"], "Q?", "a", 2)


def test_no_refund_on_rejection_and_item_budget_carries_over(conn, world):
    # FR-086, FR-087
    first = claims.open_claim(conn, world["item"], world["fraud"])
    answer_all(conn, first, world["fraud"], wrong)
    assert budget(conn, "claim_id", first)["consumed_total"] == 10
    assert budget(conn, "item_id", world["item"])["consumed_total"] == 10
    second = claims.open_claim(conn, world["item"], world["owner"])
    claims.start_verification(conn, second, world["owner"])
    assert budget(conn, "item_id", world["item"])["consumed_total"] == 15


def test_item_budget_exhaustion_halts_claiming(conn, world):
    # FR-085: two full claims use the item's 20 points; the next claimant is refused
    for name in ("fraud", "fraud2"):
        claim_id = claims.open_claim(conn, world["item"], world[name])
        answer_all(conn, claim_id, world[name], wrong)
    assert budget(conn, "item_id", world["item"])["consumed_total"] == 20
    with pytest.raises(claims.ClaimError):
        claims.open_claim(conn, world["item"], world["fraud3"])


def test_exhaustion_mid_claim_routes_to_manual_review(conn, world):
    # T7: the claimant could afford another question but the item cannot
    item = register(conn, world["finder"], caption="red bag",
                    attributes=[("Q1?", "a1", 4), ("Q2?", "a2", 4), ("Q3?", "a3", 3)])
    for name in ("fraud", "fraud2"):
        claim_id = claims.open_claim(conn, item, world[name])
        answer_all(conn, claim_id, world[name], wrong)
    last = claims.open_claim(conn, item, world["owner"])
    assert answer_all(conn, last, world["owner"], lambda q: {"Q1?": "a1", "Q2?": "a2"}.get(q)) == "MANUAL_REVIEW"
    assert budget(conn, "item_id", item)["consumed_total"] == 20


def test_admin_decision_needs_justification_and_admin_role(conn, world):
    # FR-065, FR-057
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    answer_all(conn, claim_id, world["owner"], lambda q: TRUTH[q] if q == "What colour is the strap?" else "?")
    assert claim_views.get_claim(conn, claim_id)["state"] == "DISPUTED"
    with pytest.raises(claims.ClaimError):
        claims.admin_decide(conn, claim_id, world["admin"], True, "  ")
    with pytest.raises(transitions.NotAuthorised):
        claims.admin_decide(conn, claim_id, world["finder"], True, "looks fine")
    claims.admin_decide(conn, claim_id, world["admin"], True, "Owner showed a matching receipt")
    assert claim_views.get_claim(conn, claim_id)["state"] == "VERIFIED"


def test_admin_cannot_decide_a_claim_that_is_not_waiting(conn, world):
    # FR-055, FR-056
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    claims.start_verification(conn, claim_id, world["owner"])
    with pytest.raises(transitions.IllegalTransition):
        claims.admin_decide(conn, claim_id, world["admin"], True, "why not")
    assert claim_views.get_claim(conn, claim_id)["state"] == "VERIFYING"


def test_claimant_withdrawal_is_rejected_with_its_own_cause(conn, world):
    # T11, OI-03
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    claims.start_verification(conn, claim_id, world["owner"])
    claims.withdraw_claim(conn, claim_id, world["owner"])
    claim = claim_views.get_claim(conn, claim_id)
    assert (claim["state"], claim["closure_cause"]) == ("REJECTED", "CLAIMANT_WITHDRAWN")
    assert budget(conn, "item_id", world["item"])["consumed_total"] == 5


def test_other_users_cannot_act_on_someone_elses_claim(conn, world):
    # NFR-SEC-004
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    with pytest.raises(claims.ClaimError):
        claims.start_verification(conn, claim_id, world["fraud"])
    with pytest.raises(claims.ClaimError):
        claims.withdraw_claim(conn, claim_id, world["fraud"])
    with pytest.raises(claims.ClaimError):
        claim_views.current_question(conn, claim_id, world["fraud"])


def test_claimant_view_has_no_budget_or_progress(conn, world):
    # FR-089, UI-010
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    claims.start_verification(conn, claim_id, world["owner"])
    view = claim_views.claimant_view(conn, claim_id, world["owner"])
    text = repr(view).lower()
    for word in ("budget", "remaining", "consumed", "allowance", "cost", "weight", "score"):
        assert word not in text
