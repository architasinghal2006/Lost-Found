# Evaluation for the two research questions and the scale ablation (SRS Appendix B).
#   python evaluate.py rq1   [--queries data/rq1_example.csv] [--corpus corpus.csv --photos DIR]
#   python evaluate.py rq2   [--trials 200]
#   python evaluate.py scale --distractors DIR [--queries ...] [--corpus ... --photos ...]
import argparse
import csv
import io
import os
import random
import re
import statistics
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from PIL import Image

import auth
import claim_views
import claims
import db
import embedder
import items
import search

RESULTS = Path("results")
C7_WARNING = ("C-7 LIMITATION: distractors are studio catalogue photos, the primary set is informal phone photos. "
              "The model may tell them apart by image style alone, which would make these numbers look better "
              "than real corpus growth would.")
STUB_WARNING = ("NOTE: running with the STUB embedder (a hashed bag of words over seed captions). These numbers "
                "only show that the pipeline works. Real RQ1 results need LF_USE_CLIP=1 and the 60-object / "
                "180-description photo set.")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def rank_of(ranked_ids, correct_id):
    return ranked_ids.index(correct_id) + 1 if correct_id in ranked_ids else len(ranked_ids) + 1


def recall_at(ranks, k):
    return sum(1 for r in ranks if r <= k) / len(ranks)


def mrr(ranks):
    return sum(1 / r for r in ranks) / len(ranks)


def tokens(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def overlap(a, b):
    # Jaccard token overlap, used for the lexical baseline and the description-gap measurement (A-1).
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


# ---------------------------------------------------------------------------
# RQ1: retrieval
# ---------------------------------------------------------------------------

def corpus_from_db(path):
    conn = db.connect(path)
    rows = conn.execute("SELECT * FROM items WHERE status='active'").fetchall()
    conn.close()
    return [{"id": str(r["id"]), "embedding": embedder.from_blob(r["embedding"]), "description": r["public_description"],
             "category": r["category"], "zone": r["zone"], "date_found": r["date_found"]} for r in rows]


def corpus_from_csv(csv_path, photos_dir, model):
    # Columns: item_id, photo, public_description, category, zone, date_found
    candidates = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            image = (Path(photos_dir) / row["photo"]).read_bytes()
            candidates.append({"id": str(row["item_id"]), "embedding": model.embed_image(image),
                               "description": row["public_description"], "category": row["category"],
                               "zone": row["zone"], "date_found": row["date_found"]})
    return candidates


def load_queries(path):
    # Columns: query_text, correct_item_id, and optional owner metadata: category, zone, date_lost
    with open(path, newline="", encoding="utf-8") as f:
        return [dict(row, correct_item_id=str(row["correct_item_id"])) for row in csv.DictReader(f)]


def lexical_ranking(query, candidates):
    scored = sorted(candidates, key=lambda c: (-overlap(query, c["description"]), c["id"]))
    return [c["id"] for c in scored]


def run_rq1(queries, candidates, model):
    by_id = {c["id"]: c for c in candidates}
    rows = []
    for q in queries:
        vector = model.embed_text(q["query_text"])
        semantic = search.rank_candidates(vector, candidates, rerank=False)
        reranked = search.rank_candidates(vector, candidates, rerank=True, category=q.get("category") or None,
                                          zone=q.get("zone") or None, date_lost=q.get("date_lost") or None)
        rank_sem = rank_of([r["item_id"] for r in semantic], q["correct_item_id"])
        rank_rr = rank_of([r["item_id"] for r in reranked], q["correct_item_id"])
        change = "better" if rank_rr < rank_sem else "worse" if rank_rr > rank_sem else "same"
        correct = by_id.get(q["correct_item_id"])
        rows.append({
            "query_text": q["query_text"], "correct_item_id": q["correct_item_id"],
            "rank_lexical": rank_of(lexical_ranking(q["query_text"], candidates), q["correct_item_id"]),
            "rank_semantic": rank_sem, "rank_rerank": rank_rr, "change": change,
            "token_overlap": round(overlap(q["query_text"], correct["description"]) if correct else 0.0, 3),
        })
    return rows


def print_rq1(rows):
    print(f"\nRQ1 retrieval over {len(rows)} queries")
    print(f"{'condition':<22}{'R@1':>7}{'R@5':>7}{'R@10':>7}{'MRR':>7}")
    for label, key in [("lexical baseline", "rank_lexical"), ("semantic only", "rank_semantic"),
                       ("semantic + re-rank", "rank_rerank")]:
        ranks = [r[key] for r in rows]
        print(f"{label:<22}{recall_at(ranks, 1):>7.2f}{recall_at(ranks, 5):>7.2f}"
              f"{recall_at(ranks, 10):>7.2f}{mrr(ranks):>7.2f}")
    overlaps = [r["token_overlap"] for r in rows]
    print(f"\nDescription gap (A-1): mean token overlap {statistics.mean(overlaps):.2f}, "
          f"zero overlap in {sum(1 for o in overlaps if o == 0)}/{len(rows)} queries")
    changes = [r["change"] for r in rows]
    print(f"Re-ranking vs semantic only: better {changes.count('better')}, same {changes.count('same')}, "
          f"worse {changes.count('worse')}")
    for r in rows:
        if r["change"] == "worse":
            print(f"  WORSE: '{r['query_text']}' rank {r['rank_semantic']} -> {r['rank_rerank']}")


def write_csv(path, rows):
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Per-query results written to {path}")


# ---------------------------------------------------------------------------
# RQ2: verification, simulated through the real claims.py rules
# ---------------------------------------------------------------------------

KNOWLEDGE_LEVELS = [1.0, 0.8, 0.6, 0.4, 0.2, 0.0]
GUESS_RATE = 0.1   # chance of guessing an unknown answer correctly
TYPO_RATE = 0.3    # chance a known answer is typed with one small typo
WORDS = ["red", "blue", "green", "black", "silver", "cat", "bird", "star", "lion", "maple", "river", "honda",
         "sony", "paris", "tiger", "violet", "copper", "falcon", "orbit", "mango", "delta", "pixel", "ruby", "koala"]


def new_world():
    conn = db.connect(":memory:")
    db.init_db(conn)
    return conn


@contextmanager
def temporary_uploads():
    # Simulated items need somewhere to save their photo; use a throwaway folder and restore the setting.
    previous = os.environ.get("LF_UPLOADS")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LF_UPLOADS"] = tmp
        try:
            yield
        finally:
            if previous is None:
                del os.environ["LF_UPLOADS"]
            else:
                os.environ["LF_UPLOADS"] = previous


def sim_user(conn, name):
    user_id = auth.register(conn, name, "simulated")
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def sim_item(conn, finder, rng, weights=None):
    weights = weights or [rng.randint(1, 5) for _ in range(5)]
    truth = {f"Detail {i}?": f"{rng.choice(WORDS)} {rng.choice(WORDS)}" for i in range(len(weights))}
    attributes = [(label, answer, w) for (label, answer), w in zip(truth.items(), weights)]
    item_id = items.register_item(conn, embedder.StubEmbedder(), finder, blank_photo(), "simulated item", "other",
                                  "library", "2026-10-01", attributes)
    return item_id, truth


def blank_photo():
    out = io.BytesIO()
    Image.new("RGB", (4, 4)).save(out, format="PNG")
    return out.getvalue()


def typo(text, rng):
    i = rng.randrange(len(text))
    return text[:i] + text[i + 1:]


def answer_for(truth, known, rng):
    if truth in known:
        return typo(truth, rng) if rng.random() < TYPO_RATE else truth
    if rng.random() < GUESS_RATE:
        return truth
    return f"{rng.choice(WORDS)} {rng.choice(WORDS)}"


def run_claim(conn, item_id, truth, user, knowledge, rng):
    # The claimant knows a random share of the attributes; returns None if claiming was refused.
    labels = list(truth)
    known = {truth[label] for label in rng.sample(labels, round(knowledge * len(labels)))}
    try:
        claim_id = claims.open_claim(conn, item_id, user)
    except claims.ClaimError:
        return None
    claims.start_verification(conn, claim_id, user)
    while (question := claim_views.current_question(conn, claim_id, user)) is not None:
        reply = answer_for(truth[question["question_text"]], known, rng)
        claims.answer_question(conn, claim_id, question["id"], user, reply)
    claim = claim_views.get_claim(conn, claim_id)
    record = conn.execute("SELECT score FROM verification_records WHERE claim_id=?", (claim_id,)).fetchone()
    spent = conn.execute("SELECT consumed_total FROM budgets WHERE claim_id=?", (claim_id,)).fetchone()[0]
    return {"state": claim["state"], "cause": claim["closure_cause"], "score": record["score"] if record else None,
            "spent": spent, "questions": conn.execute("SELECT COUNT(*) FROM challenges WHERE claim_id=?",
                                                      (claim_id,)).fetchone()[0]}


def simulate_claims(trials=200, seed=42):
    rng = random.Random(seed)
    conn = new_world()
    finder = sim_user(conn, "finder")
    results = {}
    with temporary_uploads():
        for level in KNOWLEDGE_LEVELS:
            claimant = sim_user(conn, f"knows_{int(level * 100)}")
            group = []
            for _ in range(trials):
                item_id, truth = sim_item(conn, finder, rng)
                group.append(run_claim(conn, item_id, truth, claimant, level, rng))
            # Withdrawn claims are not verification outcomes (OI-03), so they are left out.
            results[level] = [r for r in group if r["cause"] != "CLAIMANT_WITHDRAWN"]
    return results


def roc_table(owner_scores, opportunist_scores):
    table = []
    for i in range(21):
        t = round(i * 0.05, 2)
        table.append({"threshold": t,
                      "tpr": sum(s >= t for s in owner_scores) / len(owner_scores),
                      "fpr": sum(s >= t for s in opportunist_scores) / len(opportunist_scores)})
    return table


def choose_threshold(table, max_fpr=0.05):
    # Lowest threshold whose false-accept rate is within the cap: the most owners pass at that risk.
    return next(row for row in table if row["fpr"] <= max_fpr)


def sequential_claimants(claimants=10, seed=42):
    # The sequential-claimant attack: many identities, one item. Total disclosure must stay within the allowance.
    rng = random.Random(seed)
    conn = new_world()
    finder = sim_user(conn, "finder")
    with temporary_uploads():
        item_id, truth = sim_item(conn, finder, rng, weights=[5, 4, 3, 2, 1])
        report = []
        for n in range(claimants):
            result = run_claim(conn, item_id, truth, sim_user(conn, f"identity_{n}"), 0.0, rng)
            report.append({"identity": n, "outcome": result["state"] if result else "refused",
                           "spent": result["spent"] if result else 0})
    consumed = conn.execute("SELECT consumed_total FROM budgets WHERE item_id=?", (item_id,)).fetchone()[0]
    costs = conn.execute("SELECT SUM(disclosure_cost) FROM challenges").fetchone()[0]
    return {"claims": report, "item_consumed": consumed, "sum_of_costs": costs, "allowance": claims.ITEM_BUDGET}


def print_rq2(results, seq):
    print(f"\nRQ2 verification (seed 42, guess rate {GUESS_RATE}, typo rate {TYPO_RATE}, "
          f"claimant budget {claims.CLAIM_BUDGET}, item budget {claims.ITEM_BUDGET})")
    print(f"{'knows':>6}{'n':>5}{'pass':>7}{'review':>8}{'reject':>8}{'avg spent':>11}{'avg score':>11}")
    for level, group in results.items():
        states = [r["state"] for r in group]
        print(f"{int(level * 100):>5}%{len(group):>5}{states.count('VERIFIED') / len(group):>7.2f}"
              f"{states.count('DISPUTED') / len(group):>8.2f}{states.count('REJECTED') / len(group):>8.2f}"
              f"{statistics.mean(r['spent'] for r in group):>11.1f}{statistics.mean(r['score'] for r in group):>11.2f}")

    print("\nScore distribution (each # = 2% of the group)")
    for level, group in results.items():
        bins = [0] * 11
        for r in group:
            bins[min(int(r["score"] * 10), 10)] += 1
        print(f"{int(level * 100):>4}% " + " ".join(f"{i / 10:.1f}:{'#' * round(50 * b / len(group)):<3}"
                                                   for i, b in enumerate(bins) if b))

    owners = [r["score"] for level, g in results.items() if level > 0 for r in g]
    opportunists = [r["score"] for r in results[0.0]]
    table = roc_table(owners, opportunists)
    print("\nROC: owners (20-100% knowledge pooled) vs opportunists (public listing only, 0%)")
    print("threshold  TPR   FPR")
    for row in table[::2]:
        print(f"   {row['threshold']:.2f}   {row['tpr']:.2f}  {row['fpr']:.2f}")
    chosen = choose_threshold(table)
    print(f"Selected threshold: {chosen['threshold']:.2f} (lowest with FPR <= 0.05; TPR {chosen['tpr']:.2f}, "
          f"FPR {chosen['fpr']:.2f}). Configured pass threshold: 0.75.")
    print("False rejection rate of owners (cost side):")
    for level, group in results.items():
        if level > 0:
            scores = [r["score"] for r in group]
            print(f"  knows {int(level * 100):>3}%: FRR {sum(s < chosen['threshold'] for s in scores) / len(scores):.2f} "
                  f"at selected, {sum(s < 0.75 for s in scores) / len(scores):.2f} at 0.75")

    print(f"\nSequential-claimant attack: {len(seq['claims'])} identities, one item")
    for c in seq["claims"]:
        print(f"  identity {c['identity']}: {c['outcome']}, spent {c['spent']}")
    ok = seq["item_consumed"] <= seq["allowance"] and seq["item_consumed"] == seq["sum_of_costs"]
    print(f"Item disclosure {seq['item_consumed']} / {seq['allowance']}, sum of issued costs {seq['sum_of_costs']} "
          f"-> INV-2 {'HOLDS' if ok else 'VIOLATED'}")
    assert ok, "INV-2 violated"


# ---------------------------------------------------------------------------
# Scale ablation and latency
# ---------------------------------------------------------------------------

def load_distractors(folder, model):
    files = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    cache_path = RESULTS / f"distractors_{model.name}.npz"
    cache = dict(np.load(cache_path)) if cache_path.exists() else {}
    candidates = []
    for n, path in enumerate(files):
        if path.name not in cache:
            cache[path.name] = model.embed_image(path.read_bytes())
            if n % 100 == 0:
                print(f"  embedded {n}/{len(files)} distractors")
        candidates.append({"id": f"distractor:{path.name}", "embedding": cache[path.name], "description": "",
                           "category": None, "zone": None, "date_found": None})
    RESULTS.mkdir(exist_ok=True)
    np.savez(cache_path, **cache)
    return candidates


def run_scale(queries, base, distractors, model, sizes=(0, 250, 500, 1000, 2500), seed=42):
    random.Random(seed).shuffle(distractors)
    print("\n" + C7_WARNING)
    print(f"\nScale ablation: {len(base)} primary items + distractors")
    print(f"{'corpus':>7}{'R@1 sem':>9}{'R@1 rr':>8}{'R@10 sem':>10}{'R@10 rr':>9}{'MRR sem':>9}{'MRR rr':>8}{'ms/query':>10}")
    for size in sizes:
        size = min(size, len(distractors))
        corpus = base + distractors[:size]
        rows = run_rq1(queries, corpus, model)
        vectors = [model.embed_text(q["query_text"]) for q in queries]
        start = time.perf_counter()
        for vector in vectors:
            search.rank_candidates(vector, corpus, rerank=True)
        ms = 1000 * (time.perf_counter() - start) / len(queries)
        sem, rr = [r["rank_semantic"] for r in rows], [r["rank_rerank"] for r in rows]
        print(f"{len(corpus):>7}{recall_at(sem, 1):>9.2f}{recall_at(rr, 1):>8.2f}{recall_at(sem, 10):>10.2f}"
              f"{recall_at(rr, 10):>9.2f}{mrr(sem):>9.2f}{mrr(rr):>8.2f}{ms:>10.1f}")
        if size == len(distractors):
            break  # every distractor is already in
    print(C7_WARNING)


def synthetic_latency(sizes=(1000, 5000, 20000, 50000, 100000), repeats=5, budget_ms=100):
    # Brute-force query time against corpus size, using random unit vectors (NFR-PERF-005).
    rng = np.random.default_rng(42)
    timings = []
    for n in sizes:
        vectors = rng.standard_normal((n, embedder.DIM)).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        corpus = [{"id": i, "embedding": v, "category": "x", "zone": "y", "date_found": "2026-10-01"}
                  for i, v in enumerate(vectors)]
        query = vectors[0]
        start = time.perf_counter()
        for _ in range(repeats):
            search.rank_candidates(query, corpus, rerank=True, category="x", zone="z", date_lost="2026-09-30")
        timings.append((n, 1000 * (time.perf_counter() - start) / repeats))
    slope, intercept = np.polyfit([n for n, _ in timings], [t for _, t in timings], 1)
    crossover = (budget_ms - intercept) / slope if slope > 0 else float("inf")
    return {"timings": timings, "slope_ms_per_item": slope, "intercept_ms": intercept,
            "budget_ms": budget_ms, "crossover_items": crossover}


def print_latency(fit):
    print("\nBrute-force latency vs corpus size (synthetic 512-d vectors, this machine)")
    for n, ms in fit["timings"]:
        print(f"  {n:>7} items: {ms:8.1f} ms/query")
    print(f"Linear fit: {fit['intercept_ms']:.1f} ms + {fit['slope_ms_per_item'] * 1000:.3f} ms per 1000 items "
          f"(mostly the Python loop that builds and sorts result rows; the matrix product itself is far cheaper)")
    print(f"Brute force exceeds a {fit['budget_ms']} ms budget at about {fit['crossover_items']:,.0f} items. "
          f"The SRS latency target is [TBD]; an approximate index was not measured, so this is the point where one "
          f"could start to pay off, not proof that it would.")


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Lost & Found evaluation")
    parser.add_argument("task", choices=["rq1", "rq2", "scale"])
    parser.add_argument("--queries", default="data/rq1_example.csv")
    parser.add_argument("--db", default=os.environ.get("LF_DB", "lostfound.db"))
    parser.add_argument("--corpus", help="CSV of primary items (instead of the database)")
    parser.add_argument("--photos", help="folder with the photos named in --corpus")
    parser.add_argument("--distractors", help="folder of distractor images (scale task)")
    parser.add_argument("--trials", type=int, default=200)
    args = parser.parse_args()

    if args.task == "rq2":
        print_rq2(simulate_claims(args.trials), sequential_claimants())
        return

    model = embedder.get_embedder()
    if model.name == "stub":
        print(STUB_WARNING)
    base = corpus_from_csv(args.corpus, args.photos, model) if args.corpus else corpus_from_db(args.db)
    queries = load_queries(args.queries)
    if args.task == "rq1":
        rows = run_rq1(queries, base, model)
        print_rq1(rows)
        write_csv(RESULTS / "rq1_per_query.csv", rows)
    else:
        if args.distractors:
            run_scale(queries, base, load_distractors(args.distractors, model), model)
        else:
            print("\nNo --distractors folder given, so the retrieval part of the ablation was skipped.")
        print_latency(synthetic_latency())
        print(C7_WARNING)


if __name__ == "__main__":
    main()
