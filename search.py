from datetime import date

import numpy as np

import embedder

# Volatile re-ranking weights (SRS OI-07). A soft prior: bonuses only, never a filter (OI-08).
CATEGORY_BONUS = 0.05
ZONE_BONUS = 0.04
DATE_BONUS = 0.03
DATE_WINDOW_DAYS = 14


def metadata_bonus(item, category, zone, date_lost):
    bonus = 0.0
    if category and item["category"] == category:
        bonus += CATEGORY_BONUS
    if zone and item["zone"] == zone:
        bonus += ZONE_BONUS
    if date_lost:
        days_after_loss = (date.fromisoformat(item["date_found"]) - date.fromisoformat(date_lost)).days
        if 0 <= days_after_loss <= DATE_WINDOW_DAYS:
            bonus += DATE_BONUS
    return bonus


def rank_candidates(query_vector, candidates, rerank=True, category=None, zone=None, date_lost=None):
    # candidates: list of dicts with id, embedding, category, zone, date_found.
    if not candidates:
        return []
    matrix = np.stack([c["embedding"] for c in candidates])
    baseline = matrix @ query_vector  # cosine similarity, since every vector has length 1
    results = []
    for candidate, base in zip(candidates, baseline):
        final = float(base)
        if rerank:
            final += metadata_bonus(candidate, category, zone, date_lost)
        results.append({"item_id": candidate["id"], "baseline_score": float(base), "final_score": final})
    results.sort(key=lambda r: (-r["final_score"], r["item_id"]))
    return results


def load_active_candidates(conn):
    # Filter before scoring: resolved items are never scored (FR-028, NFR-PERF-006).
    rows = conn.execute("SELECT id, embedding, category, zone, date_found FROM items WHERE status='active'")
    return [{"id": r["id"], "embedding": embedder.from_blob(r["embedding"]), "category": r["category"],
             "zone": r["zone"], "date_found": r["date_found"]} for r in rows]


def search(conn, model, query, rerank=True, category=None, zone=None, date_lost=None):
    query_vector = model.embed_text(query)
    results = rank_candidates(query_vector, load_active_candidates(conn), rerank, category, zone, date_lost)
    best = max((r["baseline_score"] for r in results), default=0.0)
    low_confidence = best < model.viability_threshold
    return results, low_confidence
