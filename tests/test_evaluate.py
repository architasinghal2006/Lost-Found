import numpy as np
import pytest

import embedder
import evaluate
from helpers import png_bytes


def test_rank_metrics():
    ranks = [1, 2, 5, 11]
    assert evaluate.recall_at(ranks, 1) == 0.25
    assert evaluate.recall_at(ranks, 5) == 0.75
    assert evaluate.recall_at(ranks, 10) == 0.75
    assert evaluate.mrr(ranks) == pytest.approx((1 + 0.5 + 0.2 + 1 / 11) / 4)


def test_rank_of_missing_item_is_beyond_the_list():
    assert evaluate.rank_of(["a", "b"], "b") == 2
    assert evaluate.rank_of(["a", "b"], "z") == 3


def test_token_overlap():
    assert evaluate.overlap("silver keyring, bird!", "Keyring with bird") == pytest.approx(2 / 4)
    assert evaluate.overlap("abc", "xyz") == 0


def candidate(item_id, caption, description, category="bottle", zone="gym", found="2026-10-01"):
    return {"id": item_id, "embedding": embedder.StubEmbedder().embed_image(png_bytes(caption)),
            "description": description, "category": category, "zone": zone, "date_found": found}


def test_rq1_reports_all_three_conditions_per_query():
    candidates = [candidate("1", "black bottle", "dark flask", zone="library"),
                  candidate("2", "black bottle", "dark flask", zone="gym"),
                  candidate("3", "blue umbrella", "folding brolly", "umbrella")]
    queries = [{"query_text": "black bottle", "correct_item_id": "2", "category": "bottle", "zone": "gym", "date_lost": ""}]
    rows = evaluate.run_rq1(queries, candidates, embedder.StubEmbedder())
    row = rows[0]
    assert row["rank_semantic"] == 2 and row["rank_rerank"] == 1  # FR-046: both ranks recorded
    assert row["change"] == "better"
    assert "rank_lexical" in row


def test_rq2_simulation_runs_through_real_claim_rules():
    results = evaluate.simulate_claims(trials=5, seed=1)
    assert set(results) == set(evaluate.KNOWLEDGE_LEVELS)
    perfect = results[1.0]
    assert all(r["state"] == "VERIFIED" for r in perfect)
    assert all(r["spent"] <= 10 for group in results.values() for r in group)


def test_sequential_claimants_never_exceed_item_allowance():
    # INV-2 and NFR-PRIV-004
    report = evaluate.sequential_claimants(claimants=8, seed=1)
    assert report["item_consumed"] <= 20
    assert report["item_consumed"] == report["sum_of_costs"]
    assert "refused" in [r["outcome"] for r in report["claims"]]


def test_roc_threshold_selection():
    owners = [1.0, 0.9, 0.8, 0.4]
    opportunists = [0.0, 0.0, 0.1, 0.5]
    table = evaluate.roc_table(owners, opportunists)
    chosen = evaluate.choose_threshold(table, max_fpr=0.25)
    assert chosen["threshold"] == pytest.approx(0.15)
    assert chosen["tpr"] == 1.0


def test_synthetic_latency_fit_is_linear():
    fit = evaluate.synthetic_latency(sizes=[200, 400, 800], repeats=2)
    assert len(fit["timings"]) == 3 and fit["slope_ms_per_item"] > 0
