import inspect
import re
from pathlib import Path

import pytest

import claims
import events
import search
from conftest import TRUTH, answer_all, budget, register

ROOT = Path(__file__).resolve().parent.parent
CODE = {p.name: p.read_text(encoding="utf-8") for p in ROOT.glob("*.py")}
TEMPLATES = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / "templates").glob("*.html")}


def files_mentioning(pattern, sources=CODE):
    return {name for name, text in sources.items() if re.search(pattern, text)}


def columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


# Rule 1: two disjoint stores
def test_findability_and_proof_stores_share_no_fields(conn):
    shared = (columns(conn, "items") & columns(conn, "private_attributes")) - {"id"}
    assert shared == set()
    assert not {"label", "answer", "weight"} & columns(conn, "items")
    assert not {"embedding", "public_description", "category", "zone"} & columns(conn, "private_attributes")


def test_private_attributes_do_not_influence_search(conn, world):
    other = register(conn, world["finder"], attributes=[("Totally different?", "zzz", 1)])
    vectors = dict(conn.execute("SELECT id, embedding FROM items").fetchall())
    assert vectors[world["item"]] == vectors[other]
    assert "private_attributes" not in inspect.getsource(search)


# Rule 2: single reader
def test_only_claims_module_touches_private_attributes():
    sql_use = r"(FROM|JOIN|INTO|UPDATE|TABLE IF NOT EXISTS)\s+private_attributes"
    assert files_mentioning(sql_use) == {"claims.py", "db.py"}
    assert re.findall(sql_use, CODE["db.py"]) == ["TABLE IF NOT EXISTS"]  # db.py only creates it
    assert files_mentioning(r"private_attributes", TEMPLATES) == set()


# Rule 3: metered read
def test_label_and_answer_are_read_in_exactly_one_place_each():
    source = CODE["claims.py"]
    assert source.count("SELECT label FROM private_attributes") == 1
    assert "SELECT label FROM private_attributes" in inspect.getsource(claims._issue_metered)
    assert source.count("private_attributes.answer") == 1
    assert "private_attributes.answer" in inspect.getsource(claims._answer_for_issued_challenge)


def test_issue_failure_rolls_back_both_debits(conn, world, monkeypatch):
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    original = events.log_event

    def failing(conn_, item_id, event_type, *args, **kwargs):
        if event_type == "challenge_issued":
            raise RuntimeError("crash after the read")
        return original(conn_, item_id, event_type, *args, **kwargs)

    monkeypatch.setattr(events, "log_event", failing)
    with pytest.raises(RuntimeError):
        claims.start_verification(conn, claim_id, world["owner"])
    assert budget(conn, "claim_id", claim_id)["consumed_total"] == 0
    assert budget(conn, "item_id", world["item"])["consumed_total"] == 0
    assert conn.execute("SELECT COUNT(*) FROM challenges").fetchone()[0] == 0


def test_every_issued_question_is_paid_for(conn, world):
    # INV-2
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    answer_all(conn, claim_id, world["owner"], TRUTH.get)
    paid = conn.execute("SELECT SUM(disclosure_cost) FROM challenges").fetchone()[0]
    assert budget(conn, "claim_id", claim_id)["consumed_total"] == paid
    assert budget(conn, "item_id", world["item"])["consumed_total"] == paid


# Rule 4: one-way crossing (page-level checks are in test_web.py)
def test_responses_are_never_read_back_for_display():
    assert files_mentioning(r"response_text") == {"claims.py", "db.py"}
    assert "SELECT" not in [line for line in CODE["claims.py"].splitlines() if "response_text" in line][0]
    assert files_mentioning(r"\.answer\b|answer_for", TEMPLATES) == set()


# Rule 5: soft delete only
def test_no_hard_deletes_in_application_code():
    deletes = re.findall(r"DELETE FROM (\w+)", "\n".join(text for name, text in CODE.items() if name != "db.py"))
    assert set(deletes) <= {"sessions"}


# Rule 6: guarded updates
def test_every_item_status_change_is_guarded():
    status_updates = re.findall(r'"UPDATE items SET status=[^)]*', "\n".join(CODE.values()))
    assert status_updates, "expected guarded status updates"
    for update in status_updates:
        assert re.search(r"AND status='(active|resolved)'", update), update


def test_only_one_routine_resolves_items():
    # FR-090: every closure path funnels through close_item_in_tx
    assert sum(text.count("SET status='resolved'") for text in CODE.values()) == 1
    assert "SET status='resolved'" in inspect.getsource(claims.close_item_in_tx)
    assert claims.CLOSURE_REASONS == ("HANDED_OVER", "WITHDRAWN", "EXPIRED")


def test_writes_use_begin_immediate():
    assert "BEGIN IMMEDIATE" in CODE["db.py"]
    assert files_mentioning(r"\.commit\(\)") == set()


# Rule 7: append-only events
def test_no_code_updates_or_deletes_events():
    assert files_mentioning(r"UPDATE item_events|DELETE FROM item_events") == set()


# FR-047 / NFR-MNT-001: retrieval is advisory and replaceable
def test_claims_never_use_retrieval_scores():
    for name in ("claims.py", "claim_views.py", "scoring.py", "transitions.py"):
        assert not re.search(r"^import (search|embedder)|^from (search|embedder)", CODE[name], re.M), name
    assert not re.search(r"^import (claims|claim_views)", CODE["search.py"], re.M)
