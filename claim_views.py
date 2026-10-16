# Read-only queries for pages. Nothing here touches private_attributes.
import claims

# NFR-USE-001: plain language and the next action, never an internal state name.
PLAIN = {
    "PENDING": ("Claim received", "Start verification to answer a few questions about your item."),
    "VERIFYING": ("Verification in progress", "Answer the question below."),
    "VERIFIED": ("Ownership verified", "Arrange to collect the item, then both of you confirm the handover."),
    "REJECTED": ("Claim not accepted", "This claim is closed."),
    "EXPIRED": ("Claim timed out", "This claim was inactive for too long and is closed."),
    "CLOSED": ("Item returned", "The handover is complete."),
    "CLOSED_ITEM_RESOLVED": ("Item no longer available", "This item was closed, so the claim was closed too."),
    "MANUAL_REVIEW": ("Under review", "An administrator will decide this claim. Check back later."),
    "DISPUTED": ("Under review", "An administrator will decide this claim. Check back later."),
}


def get_claim(conn, claim_id):
    return claims._get_claim(conn, claim_id)


def current_question(conn, claim_id, user):
    # The unanswered question for the claimant, or None.
    claims._own_claim(conn, claim_id, user)
    return conn.execute(
        "SELECT challenges.id, challenges.question_text FROM challenges"
        " LEFT JOIN challenge_responses ON challenge_responses.challenge_id = challenges.id"
        " WHERE challenges.claim_id=? AND challenge_responses.id IS NULL",
        (claim_id,),
    ).fetchone()


def claimant_view(conn, claim_id, user):
    # Everything the claimant may see: no budget, no progress, no scores (UI-010, FR-089, NFR-USE-005).
    claim = claims._own_claim(conn, claim_id, user)
    title, next_step = PLAIN[claim["state"]]
    question = current_question(conn, claim_id, user) if claim["state"] == "VERIFYING" else None
    return {
        "id": claim["id"],
        "item_id": claim["item_id"],
        "state": claim["state"],
        "title": title,
        "next_step": next_step,
        "withdrawn": claim["closure_cause"] == "CLAIMANT_WITHDRAWN",
        "question": dict(question) if question else None,
        "claimant_confirmed": bool(claim["claimant_confirmed"]),
        "finder_confirmed": claim["finder_confirmed_by"] is not None,
    }


def my_claims(conn, user):
    rows = conn.execute(
        "SELECT claims.id, claims.item_id, claims.state, items.public_description, items.photo_path"
        " FROM claims JOIN items ON items.id = claims.item_id WHERE claims.claimant_id=? ORDER BY claims.id DESC",
        (user["id"],),
    ).fetchall()
    return [dict(row, title=PLAIN[row["state"]][0]) for row in rows]


def my_open_claim(conn, item_id, user):
    return conn.execute("SELECT id FROM claims WHERE item_id=? AND claimant_id=? ORDER BY id DESC",
                        (item_id, user["id"])).fetchone()


def claims_for_staff(conn, item_id=None):
    # For the finder of an item and for admins: states, scores and per-question similarity. Never answers.
    sql = ("SELECT claims.*, users.username AS claimant, items.public_description,"
           " verification_records.score, budgets.consumed_total AS claim_spent"
           " FROM claims JOIN users ON users.id = claims.claimant_id JOIN items ON items.id = claims.item_id"
           " LEFT JOIN verification_records ON verification_records.claim_id = claims.id"
           " LEFT JOIN budgets ON budgets.claim_id = claims.id")
    params = ()
    if item_id is not None:
        sql += " WHERE claims.item_id=?"
        params = (item_id,)
    rows = conn.execute(sql + " ORDER BY claims.id DESC", params).fetchall()
    result = []
    for row in rows:
        questions = conn.execute(
            "SELECT challenges.question_text, challenges.disclosure_cost, challenge_responses.similarity,"
            " challenge_responses.matched FROM challenges"
            " LEFT JOIN challenge_responses ON challenge_responses.challenge_id = challenges.id"
            " WHERE challenges.claim_id=? ORDER BY challenges.id",
            (row["id"],),
        ).fetchall()
        result.append(dict(row, questions=[dict(q) for q in questions]))
    return result


def item_budget(conn, item_id):
    return conn.execute("SELECT * FROM budgets WHERE item_id=?", (item_id,)).fetchone()
