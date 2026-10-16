import pytest

import embedder
import search
from helpers import make_user, png_bytes, raw_item


@pytest.fixture
def stub():
    return embedder.StubEmbedder()


@pytest.fixture
def corpus(conn):
    finder = make_user(conn, "finder")
    ids = {
        "bottle_lib": raw_item(conn, finder["id"], "black metal water bottle", "bottle", "library", "2026-10-01"),
        "bottle_gym": raw_item(conn, finder["id"], "black metal water bottle", "bottle", "gym", "2026-09-01"),
        "umbrella": raw_item(conn, finder["id"], "blue folding umbrella", "umbrella", "cafeteria", "2026-10-02"),
    }
    return ids


def test_query_and_image_share_vector_space(stub):
    # FR-031
    assert stub.embed_text("a bottle").shape == stub.embed_image(png_bytes("bottle")).shape == (embedder.DIM,)


def test_stub_is_deterministic(stub):
    assert (stub.embed_text("blue bird") == stub.embed_text("blue bird")).all()
    assert (stub.embed_image(b"abc") == stub.embed_image(b"abc")).all()


def test_scores_every_active_item(conn, stub, corpus):
    # FR-032
    results, _ = search.search(conn, stub, "water bottle")
    assert {r["item_id"] for r in results} == set(corpus.values())


def test_semantic_match_ranks_first(conn, stub, corpus):
    results, _ = search.search(conn, stub, "blue umbrella", rerank=False)
    assert results[0]["item_id"] == corpus["umbrella"]


def test_resolved_items_are_excluded(conn, stub, corpus):
    # FR-028
    conn.execute("UPDATE items SET status='resolved', resolved_at='t', closure_reason='WITHDRAWN' WHERE id=?",
                 (corpus["umbrella"],))
    results, _ = search.search(conn, stub, "blue umbrella")
    assert corpus["umbrella"] not in {r["item_id"] for r in results}


def test_rerank_off_is_pure_baseline(conn, stub, corpus):
    # FR-045
    results, _ = search.search(conn, stub, "black bottle", rerank=False, category="bottle", zone="gym")
    assert results and all(r["final_score"] == r["baseline_score"] for r in results)


def test_rerank_separates_near_identical_items(conn, stub, corpus):
    # FR-041: same photo embedding, metadata decides the order
    off, _ = search.search(conn, stub, "black bottle", rerank=False)
    on, _ = search.search(conn, stub, "black bottle", rerank=True, category="bottle", zone="gym", date_lost="2026-08-30")
    assert off[0]["item_id"] == corpus["bottle_lib"]  # tie broken by id
    assert on[0]["item_id"] == corpus["bottle_gym"]
    assert on[0]["final_score"] > on[0]["baseline_score"]


@pytest.mark.parametrize("kwargs, expected", [
    ({"category": "bottle"}, search.CATEGORY_BONUS),
    ({"zone": "library"}, search.ZONE_BONUS),
    ({"date_lost": "2026-09-28"}, search.DATE_BONUS),
])
def test_each_metadata_dimension_contributes(kwargs, expected):
    # FR-042
    item = {"category": "bottle", "zone": "library", "date_found": "2026-10-01"}
    assert search.metadata_bonus(item, **{"category": None, "zone": None, "date_lost": None, **kwargs}) == expected


def test_date_outside_window_gives_no_bonus():
    item = {"category": "x", "zone": "y", "date_found": "2026-10-01"}
    assert search.metadata_bonus(item, None, None, "2026-10-05") == 0  # found before it was lost
    assert search.metadata_bonus(item, None, None, "2026-08-01") == 0  # too long ago


def test_identical_inputs_give_identical_rankings(conn, stub, corpus):
    # FR-044
    first, _ = search.search(conn, stub, "black bottle", category="bottle")
    second, _ = search.search(conn, stub, "black bottle", category="bottle")
    assert first == second


def test_unrelated_query_is_flagged_low_confidence(conn, stub, corpus):
    # FR-039
    _, low = search.search(conn, stub, "quantum giraffe saxophone")
    assert low
    _, low = search.search(conn, stub, "black metal water bottle")
    assert not low
