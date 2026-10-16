from datetime import timedelta

import db
import events
import scoring
import transitions
from transitions import NotAuthorised

# Volatile allowances (SRS OI-12). Budgets are only ever debited, never refunded or reset.
CLAIM_BUDGET = 10
ITEM_BUDGET = 20
CLAIM_TIMEOUT_DAYS = 7  # OI-14: a VERIFYING claim with no activity for this long expires (T5)
CLOSURE_REASONS = ("HANDED_OVER", "WITHDRAWN", "EXPIRED")
OPEN_STATES = ("PENDING", "VERIFYING", "VERIFIED")
ADMIN_REVIEW_STATES = ("MANUAL_REVIEW", "DISPUTED")


class ClaimError(Exception):
    pass


# ---------------------------------------------------------------------------
# D2: private attributes. This module is the only code that reads this table.
# ---------------------------------------------------------------------------

def insert_private_attributes(conn, item_id, attributes):
    # Runs inside the caller's transaction (item registration).
    for label, answer, weight in attributes:
        conn.execute("INSERT INTO private_attributes (item_id, label, answer, weight) VALUES (?, ?, ?, ?)",
                     (item_id, label, answer, weight))


def attribute_lock_status(conn, item_id):
    # For the finder's page: id, weight and lock flag only. Never the label or the answer.
    return conn.execute(
        "SELECT id, weight, EXISTS (SELECT 1 FROM challenges WHERE attribute_id = private_attributes.id) AS locked"
        " FROM private_attributes WHERE item_id=? ORDER BY id",
        (item_id,),
    ).fetchall()


def replace_attribute(conn, item_id, attribute_id, user, label, answer, weight):
    with db.transaction(conn):
        item = conn.execute("SELECT finder_id, status FROM items WHERE id=?", (item_id,)).fetchone()
        if item is None or item["finder_id"] != user["id"]:
            raise ClaimError("Only the finder can change private details.")
        if item["status"] != "active":
            raise ClaimError("This item is closed and can no longer be changed.")
        # FR-024: guarded update, refused once any question has been asked from this attribute.
        cur = conn.execute(
            "UPDATE private_attributes SET label=?, answer=?, weight=?"
            " WHERE id=? AND item_id=? AND NOT EXISTS (SELECT 1 FROM challenges WHERE attribute_id=?)",
            (label, answer, weight, attribute_id, item_id, attribute_id),
        )
        if cur.rowcount != 1:
            raise ClaimError("This detail is locked because a question has already been asked from it.")
        events.log_event(conn, item_id, "attribute_replaced", user["username"], "staff", f"private detail #{attribute_id}")


def _pick_attribute(conn, claim, claim_left, item_left):
    # Chooses the next attribute using only id and weight (no label, no answer).
    rows = conn.execute(
        "SELECT id, weight FROM private_attributes WHERE item_id=?"
        " AND id NOT IN (SELECT attribute_id FROM challenges WHERE claim_id=?)"
        " ORDER BY weight DESC, id",
        (claim["item_id"], claim["id"]),
    ).fetchall()
    claimant_can = [r for r in rows if r["weight"] <= claim_left]
    if not claimant_can:
        return None, "claimant_done"
    both_can = [r for r in claimant_can if r["weight"] <= item_left]
    if not both_can:
        return None, "item_exhausted"
    return both_can[0], "ok"


def _issue_metered(conn, claim, attribute):
    # The metered read: debit both budgets, read the label, record the challenge. One transaction.
    cost = attribute["weight"]
    _debit(conn, "claim_id", claim["id"], cost)
    _debit(conn, "item_id", claim["item_id"], cost)
    label = conn.execute("SELECT label FROM private_attributes WHERE id=?", (attribute["id"],)).fetchone()["label"]
    conn.execute(
        "INSERT INTO challenges (claim_id, attribute_id, question_text, disclosure_cost, issued_at) VALUES (?, ?, ?, ?, ?)",
        (claim["id"], attribute["id"], label, cost, db.now()),
    )
    events.log_event(conn, claim["item_id"], "challenge_issued", "verification", "staff",
                     f"private detail #{attribute['id']} asked, cost {cost}", claim["id"])


def _answer_for_issued_challenge(conn, challenge_id):
    # Only reachable for a challenge that exists, i.e. one that has already been paid for.
    return conn.execute(
        "SELECT private_attributes.answer FROM challenges"
        " JOIN private_attributes ON private_attributes.id = challenges.attribute_id WHERE challenges.id=?",
        (challenge_id,),
    ).fetchone()["answer"]


def _claiming_halted(conn, item_id):
    # FR-085: the item budget cannot pay for even its cheapest question.
    cheapest = conn.execute("SELECT MIN(weight) FROM private_attributes WHERE item_id=?", (item_id,)).fetchone()[0]
    return cheapest is None or _left(conn, "item_id", item_id) < cheapest


# ---------------------------------------------------------------------------
# Budgets (D4)
# ---------------------------------------------------------------------------

def create_item_budget(conn, item_id):
    conn.execute("INSERT INTO budgets (item_id, allowance) VALUES (?, ?)", (item_id, ITEM_BUDGET))


def _left(conn, column, owner_id):
    assert column in ("claim_id", "item_id")
    row = conn.execute(f"SELECT allowance - consumed_total AS left, frozen FROM budgets WHERE {column}=?",
                       (owner_id,)).fetchone()
    return 0 if row["frozen"] else row["left"]


def _debit(conn, column, owner_id, cost):
    # Hard limit in SQL: the update only happens if the budget is unfrozen and can pay (NFR-SEC-005).
    assert column in ("claim_id", "item_id")
    cur = conn.execute(
        f"UPDATE budgets SET consumed_total = consumed_total + ?"
        f" WHERE {column}=? AND frozen=0 AND consumed_total + ? <= allowance",
        (cost, owner_id, cost),
    )
    if cur.rowcount != 1:
        raise ClaimError("This question cannot be issued.")


def _freeze_budgets(conn, item_id):
    conn.execute(
        "UPDATE budgets SET frozen=1, frozen_value=consumed_total"
        " WHERE frozen=0 AND (item_id=? OR claim_id IN (SELECT id FROM claims WHERE item_id=?))",
        (item_id, item_id),
    )


# ---------------------------------------------------------------------------
# Claim lifecycle
# ---------------------------------------------------------------------------

def _get_claim(conn, claim_id):
    claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    if claim is None:
        raise ClaimError("No such claim.")
    return claim


def _own_claim(conn, claim_id, user):
    claim = _get_claim(conn, claim_id)
    if claim["claimant_id"] != user["id"]:
        raise ClaimError("No such claim.")  # same message as missing: don't reveal other claims
    return claim


def _require_active_item(conn, item_id):
    if conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()["status"] != "active":
        raise ClaimError("This item is closed.")


def _move(conn, claim, trigger, actor_code, actor_name, cause=None):
    # Applies one row of the transition table. The WHERE state=? guard makes a stale move fail.
    new_state = transitions.next_state(claim["state"], trigger, actor_code)
    cur = conn.execute("UPDATE claims SET state=?, closure_cause=COALESCE(?, closure_cause) WHERE id=? AND state=?",
                       (new_state, cause, claim["id"], claim["state"]))
    if cur.rowcount != 1:
        raise ClaimError("This claim changed in the meantime. Please reload the page.")
    events.log_event(conn, claim["item_id"], trigger, actor_name, "claimant",
                     f"claim #{claim['id']}: {claim['state']} -> {new_state}", claim["id"])
    return new_state


def open_claim(conn, item_id, user):
    with db.transaction(conn):
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if item is None or item["status"] != "active":
            raise ClaimError("This item is not open for claims.")
        if user["role"] == "admin":
            raise ClaimError("Administrators cannot claim items.")
        if item["finder_id"] == user["id"]:
            raise ClaimError("You cannot claim an item you registered.")
        earlier = conn.execute(
            "SELECT 1 FROM claims WHERE item_id=? AND claimant_id=? AND state != 'CLOSED_ITEM_RESOLVED'",
            (item_id, user["id"]),
        ).fetchone()
        if earlier:
            raise ClaimError("You have already claimed this item.")
        if _claiming_halted(conn, item_id):
            raise ClaimError("Claims on this item are paused while an administrator reviews it.")
        state = transitions.next_state(None, "claim_filed", "C")  # T1
        cur = conn.execute("INSERT INTO claims (item_id, claimant_id, state, created_at) VALUES (?, ?, ?, ?)",
                           (item_id, user["id"], state, db.now()))
        claim_id = cur.lastrowid
        conn.execute("INSERT INTO budgets (claim_id, allowance) VALUES (?, ?)", (claim_id, CLAIM_BUDGET))
        events.log_event(conn, item_id, "claim_filed", user["username"], "claimant", f"claim #{claim_id} opened", claim_id)
    return claim_id


def _has_open_challenge(conn, claim_id):
    return conn.execute(
        "SELECT 1 FROM challenges LEFT JOIN challenge_responses ON challenge_responses.challenge_id = challenges.id"
        " WHERE challenges.claim_id=? AND challenge_responses.id IS NULL",
        (claim_id,),
    ).fetchone() is not None


def _advance(conn, claim):
    # After the start or an answer: ask the next question, or decide.
    attribute, status = _pick_attribute(conn, claim, _left(conn, "claim_id", claim["id"]),
                                        _left(conn, "item_id", claim["item_id"]))
    if status == "ok":
        _issue_metered(conn, claim, attribute)
        if claim["state"] == "PENDING":
            _move(conn, claim, "challenge_sent", "V", "verification")  # T2
    elif claim["state"] == "PENDING":
        raise ClaimError("Claims on this item are paused while an administrator reviews it.")
    elif status == "item_exhausted":
        _record_verdict(conn, claim, "budget_exhausted")  # T7
    else:
        _record_verdict(conn, claim, None)


def _record_verdict(conn, claim, forced_trigger):
    rows = conn.execute(
        "SELECT challenges.disclosure_cost, challenge_responses.matched FROM challenges"
        " LEFT JOIN challenge_responses ON challenge_responses.challenge_id = challenges.id"
        " WHERE challenges.claim_id=?",
        (claim["id"],),
    ).fetchall()
    value = scoring.score([(r["disclosure_cost"], r["matched"] == 1) for r in rows])
    trigger = forced_trigger or scoring.verdict_trigger(value)
    new_state = _move(conn, claim, trigger, "V", "verification")
    conn.execute(
        "INSERT INTO verification_records (claim_id, score, threshold_applied, outcome, created_at) VALUES (?, ?, ?, ?, ?)",
        (claim["id"], value, scoring.PASS_THRESHOLD, new_state, db.now()),
    )
    events.log_event(conn, claim["item_id"], "verification_scored", "verification", "staff",
                     f"claim #{claim['id']}: score {value:.2f} -> {new_state}", claim["id"])


def start_verification(conn, claim_id, user):
    with db.transaction(conn):
        claim = _own_claim(conn, claim_id, user)
        _require_active_item(conn, claim["item_id"])
        if claim["state"] != "PENDING" or _has_open_challenge(conn, claim_id):
            return  # already started: asking again changes nothing
        _advance(conn, claim)


def answer_question(conn, claim_id, challenge_id, user, text):
    with db.transaction(conn):
        claim = _own_claim(conn, claim_id, user)
        _require_active_item(conn, claim["item_id"])
        if claim["state"] != "VERIFYING":
            raise ClaimError("This claim is not waiting for an answer.")
        challenge = conn.execute(
            "SELECT challenges.id FROM challenges"
            " LEFT JOIN challenge_responses ON challenge_responses.challenge_id = challenges.id"
            " WHERE challenges.id=? AND challenges.claim_id=? AND challenge_responses.id IS NULL",
            (challenge_id, claim_id),
        ).fetchone()
        if challenge is None:
            raise ClaimError("That question has already been answered.")
        value = scoring.similarity(text, _answer_for_issued_challenge(conn, challenge_id))
        matched = scoring.is_match(value)
        conn.execute(
            "INSERT INTO challenge_responses (challenge_id, response_text, similarity, matched, answered_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (challenge_id, scoring.normalise(text), value, int(matched), db.now()),
        )
        # Only the score leaves this function, never the correct answer.
        events.log_event(conn, claim["item_id"], "challenge_answered", user["username"], "staff",
                         f"question #{challenge_id}: similarity {value:.2f}, {'match' if matched else 'no match'}",
                         claim_id)
        _advance(conn, claim)


def withdraw_claim(conn, claim_id, user):
    # T11, resolved under OI-03: REJECTED with its own cause, no refund.
    with db.transaction(conn):
        claim = _own_claim(conn, claim_id, user)
        _move(conn, claim, "claimant_withdraws", "C", user["username"], cause="CLAIMANT_WITHDRAWN")


def admin_decide(conn, claim_id, admin, approve, justification):
    # T8 / T9 from MANUAL_REVIEW, T13 from DISPUTED. FR-065: a justification is required.
    if admin["role"] != "admin":
        raise NotAuthorised("Only an administrator can decide a claim.")
    if not (justification or "").strip():
        raise ClaimError("A justification is required.")
    with db.transaction(conn):
        claim = _get_claim(conn, claim_id)
        if claim["state"] == "MANUAL_REVIEW":
            trigger = "admin_approve" if approve else "admin_reject"
        else:
            trigger = "admin_resolve_approve" if approve else "admin_resolve_reject"
        _move(conn, claim, trigger, "A", admin["username"])
        conn.execute("UPDATE claims SET decision_justification=? WHERE id=?", (justification.strip(), claim_id))
        events.log_event(conn, claim["item_id"], "admin_justification", admin["username"], "staff",
                         justification.strip(), claim_id)


def expire_stale_claims(conn, now):
    cutoff = (now - timedelta(days=CLAIM_TIMEOUT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    stale = conn.execute(
        "SELECT id FROM claims WHERE state='VERIFYING'"
        " AND (SELECT MAX(issued_at) FROM challenges WHERE claim_id = claims.id) < ?",
        (cutoff,),
    ).fetchall()
    for row in stale:
        with db.transaction(conn):
            _move(conn, _get_claim(conn, row["id"]), "timeout", "S", "scheduler")  # T5, no refund


# ---------------------------------------------------------------------------
# Handover, closure, reopen
# ---------------------------------------------------------------------------

def confirm_handover(conn, claim_id, user):
    # FR-104: claimant and finder both confirm. An admin may stand in for the finder, never the claimant.
    with db.transaction(conn):
        claim = _get_claim(conn, claim_id)
        item = conn.execute("SELECT * FROM items WHERE id=?", (claim["item_id"],)).fetchone()
        if user["id"] == claim["claimant_id"]:
            cur = conn.execute("UPDATE claims SET claimant_confirmed=1 WHERE id=? AND claimant_confirmed=0", (claim_id,))
            side, actor_code = "claimant", "C"
        elif user["id"] == item["finder_id"] or user["role"] == "admin":
            cur = conn.execute("UPDATE claims SET finder_confirmed_by=? WHERE id=? AND finder_confirmed_by IS NULL",
                               (user["id"], claim_id))
            side, actor_code = "finder", "F"
            if user["role"] == "admin":
                side = "finder (by an administrator)"
        else:
            raise NotAuthorised("Only the finder, an administrator or the claimant can confirm handover.")
        if claim["state"] != "VERIFIED":
            raise ClaimError("Handover is only possible once the claim is verified.")
        if cur.rowcount == 1:
            events.log_event(conn, item["id"], "handover_confirmed", user["username"], "claimant",
                             f"{side} confirmed handover", claim_id)
        claim = _get_claim(conn, claim_id)
        if claim["claimant_confirmed"] and claim["finder_confirmed_by"]:
            return close_item_in_tx(conn, item["id"], "HANDED_OVER", user["username"], claim_id, actor_code)
    return False


def close_item_in_tx(conn, item_id, reason, actor_name, winning_claim_id=None, winner_actor="F"):
    # The single closure routine for all three paths. Must run inside a transaction (FR-101).
    if reason not in CLOSURE_REASONS:
        raise ClaimError("A valid reason code is required to close an item.")
    waiting = conn.execute("SELECT 1 FROM claims WHERE item_id=? AND state IN (?, ?)",
                           (item_id, *ADMIN_REVIEW_STATES)).fetchone()
    if waiting:
        raise ClaimError("A claim on this item is waiting for an administrator's decision. Decide it first.")
    # Guarded update: a second close finds status != 'active', changes nothing and logs nothing (FR-098).
    cur = conn.execute("UPDATE items SET status='resolved', resolved_at=?, closure_reason=? WHERE id=? AND status='active'",
                       (db.now(), reason, item_id))
    if cur.rowcount == 0:
        return False
    open_claims = conn.execute("SELECT * FROM claims WHERE item_id=? AND state IN (?, ?, ?)",
                               (item_id, *OPEN_STATES)).fetchall()
    for claim in open_claims:
        if claim["id"] == winning_claim_id:
            _move(conn, claim, "handoff_done", winner_actor, actor_name)            # T6
        else:
            _move(conn, claim, "item_resolved_elsewhere", "S", "system")            # T10
    _freeze_budgets(conn, item_id)
    events.log_event(conn, item_id, "item_closed", actor_name, "public", f"closed with reason {reason}")
    return True


def reopen_item(conn, item_id, admin, justification):
    # FR-113 admin only; FR-116 the budget comes back at its frozen value, never a fresh allowance.
    if admin["role"] != "admin":
        raise NotAuthorised("Only an administrator can reopen an item.")
    if not (justification or "").strip():
        raise ClaimError("A justification is required to reopen an item.")
    with db.transaction(conn):
        cur = conn.execute("UPDATE items SET status='active', resolved_at=NULL, closure_reason=NULL"
                           " WHERE id=? AND status='resolved'", (item_id,))
        if cur.rowcount == 0:
            return False
        conn.execute("UPDATE budgets SET consumed_total=frozen_value, frozen=0 WHERE item_id=?", (item_id,))
        events.log_event(conn, item_id, "item_reopened", admin["username"], "public", "reopened by an administrator")
        events.log_event(conn, item_id, "admin_justification", admin["username"], "staff", justification.strip())
    return True
