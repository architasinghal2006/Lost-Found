# Lost & Found: Implementation Spec

Source of truth: `SRS SE FINAL_final.pdf` (IEEE 830). This spec says how the prototype meets it.
Where the SRS leaves something [TBD], [TBC] or open, the decision is recorded in section 9.

## 1. Stack

- Python 3, FastAPI, Jinja2 server-rendered pages, SQLite through the standard `sqlite3` module (no ORM).
- Embedder interface with two implementations:
  - `StubEmbedder` (default): deterministic, hash-based, no downloads. Words are hashed to random
    unit vectors and summed. For images it reads the PNG `caption` text chunk written by `seed.py`
    when present, otherwise it hashes the raw bytes. It is a toy, so its numbers mean nothing.
  - `ClipEmbedder`: CLIP ViT-B/32 on CPU via `open_clip`, switched on with `LF_USE_CLIP=1`.
- Brute-force cosine similarity over a numpy matrix. No ANN index (NFR-PERF-005).
- pytest.

## 2. Modules

| File | Responsibility |
|---|---|
| `db.py` | Schema, connection, `transaction()` helper (`BEGIN IMMEDIATE` ... `COMMIT` / `ROLLBACK`) |
| `auth.py` | Users, PBKDF2 password hashes, sessions. Roles: `user`, `admin` |
| `events.py` | `log_event` (append-only) and `list_events` scoped to the viewer (UI-016) |
| `items.py` | Register item, finder withdrawal, scheduled expiry. Validation of public fields |
| `embedder.py` | Embedder interface, stub, CLIP, `get_embedder()` |
| `search.py` | Cosine ranking of a text query against item photo embeddings + metadata re-ranking |
| `transitions.py` | The §3.7 transition table as one dict keyed by `(state, trigger)` |
| `scoring.py` | Pure functions: text normalisation, similarity, verdict |
| `claims.py` | Claims, challenges, budgets, handover, closure, reopen. **Only reader of `private_attributes`** |
| `ratelimit.py` | In-memory per-user sliding window counter |
| `main.py` | Routes, auth dependencies, startup (schema, expiry sweep, LAN address) |
| `seed.py` | Demo users, items, drawn PNG placeholder photos |
| `evaluate.py` | RQ1, RQ2, scale ablation |

Dependency direction is one way: `main → items → claims → events/db`. `search` never imports
`claims` (FR-047, NFR-MNT-001), and `claims` never imports `search`.

## 3. Data model (SQLite)

Stores, matching the Level-1 DFD:

- **D1, findability:** `items` (public fields + `embedding` BLOB + status/closure fields).
- **D2, ownership proof:** `private_attributes` (`label`, `answer`, `weight` 1-5). It has no embedding
  and no public field, and `items` has no attribute column. Only `claims.py` contains SQL for this table.
- Claims: `claims`, `challenges` (`question_text`, `disclosure_cost`), `challenge_responses`
  (`response_text`, `similarity`, `matched`), `verification_records` (`score`, `threshold_applied`, `outcome`).
- D4, budgets: `budgets` (`item_id` or `claim_id`, `allowance`, `consumed_total`, `frozen`, `frozen_value`).
- D5, audit: `item_events` (`item_id`, `claim_id`, `event_type`, `actor`, `visibility`, `detail`, `created_at`).
- `users`, `sessions`.

Item fields: `status` is `active` or `resolved`. `resolved_at` and `closure_reason`
(`HANDED_OVER`, `WITHDRAWN`, `EXPIRED`) are set together on closure (FR-094, INV-5).

Database triggers raise on `UPDATE`/`DELETE` of `item_events` (rule 7, NFR-AUD-002) and on
`DELETE` of `items` (rule 5, soft delete only).

Disclosure classes (FR-013):

| Class | Fields |
|---|---|
| Public | photo, public_description, category, zone, date_found, status, closure_reason |
| Internal | embedding, similarity scores |
| Withheld | private_attributes.label/answer (answer is never shown to anyone) |
| Controlled-release | challenges.question_text, shown only to the claimant it was issued to |

## 4. Architecture rules (each has a test)

1. D1 and D2 are disjoint tables. Search never reads D2.
2. Only `claims.py` contains SQL that names `private_attributes` (`db.py` creates it). A test greps the code.
3. Metered read: an attribute's label is read only inside the transaction that debits both budgets
   and inserts the challenge row. Its answer is read only for scoring a challenge that was already
   issued and paid for. Choosing the next attribute reads only `id` and `weight`.
4. Only verdicts leave verification. No page, event or error message contains an answer, for any
   role. Even the finder cannot read attributes back after registering; they can only replace an
   unlocked one.
5. Soft delete only. No `DELETE` statement in application code, plus a trigger.
6. Every closure uses `UPDATE items ... WHERE id=? AND status='active'` and checks `rowcount`. All writes
   run under `BEGIN IMMEDIATE`. SQLite has no row locks; `BEGIN IMMEDIATE` serialises every writer,
   which is stronger than FR-058's row-level locking.
7. `item_events` is append-only (trigger + no UPDATE/DELETE in code) and shown as the item timeline.

## 5. Claims and verification

States and transitions are exactly the SRS §3.7 table, held in `transitions.TRANSITIONS`:

| # | From | Trigger | To | Actors |
|---|---|---|---|---|
| T1 | (new) | claim_filed | PENDING | C |
| T2 | PENDING | challenge_sent | VERIFYING | V |
| T3 | VERIFYING | score_pass | VERIFIED | V |
| T4 | VERIFYING | score_fail | REJECTED | V |
| T5 | VERIFYING | timeout | EXPIRED | S |
| T6 | VERIFIED | handoff_done | CLOSED | F, C |
| T7 | VERIFYING | budget_exhausted | MANUAL_REVIEW | V |
| T8 | MANUAL_REVIEW | admin_approve | VERIFIED | A |
| T9 | MANUAL_REVIEW | admin_reject | REJECTED | A |
| T10 | PENDING, VERIFYING, VERIFIED | item_resolved_elsewhere | CLOSED_ITEM_RESOLVED | S |
| T11 | PENDING, VERIFYING | claimant_withdraws | REJECTED (cause `CLAIMANT_WITHDRAWN`) | C |
| T12 | VERIFYING | contested | DISPUTED | V, A |
| T13 | DISPUTED | admin_resolve_approve / admin_resolve_reject | VERIFIED / REJECTED | A |

Actor codes: C claimant, F finder (an admin acting for the finder counts as F), A admin,
S scheduler/system, V verification subsystem. A missing `(state, trigger)` pair raises
`IllegalTransition` and leaves state unchanged. A wrong actor raises `NotAuthorised`.

Terminal states: REJECTED, EXPIRED, CLOSED, CLOSED_ITEM_RESOLVED.

Flow:

1. **Open claim (T1).** The item must be active, the user must not be its finder or an admin, and
   the user must have no earlier claim on the item except ones cascaded by T10, because a rejected
   claimant cannot retry. Automated claiming must not be halted (FR-085). Creates a claim budget of 10.
2. **Issue a challenge (T2 on the first one).** Candidates are attributes not yet asked in this claim.
   The system picks the highest weight with `weight <= claimant remaining` and `weight <= item remaining`.
   Both budgets are debited by the weight in the same transaction as the challenge insert, using
   `UPDATE budgets SET consumed_total = consumed_total + ? WHERE id=? AND frozen=0 AND consumed_total + ? <= allowance`
   with a rowcount check (hard limit, NFR-SEC-005). The attribute is then locked (FR-024).
   Asking again while an issued challenge is unanswered returns that same challenge (idempotent).
3. **Answer.** Scored with `difflib.SequenceMatcher` on lowercased, whitespace-normalised text.
   Similarity of 0.8 or more is a match. Only the similarity and the match flag are logged. Empty text
   is a non-match (FR-081). In the same transaction the system then advances:
   - if no unasked attribute is affordable by the claimant: compute the verdict;
   - else if the claimant could afford one but the item budget cannot: T7, MANUAL_REVIEW;
   - else issue the next challenge.
4. **Verdict** = matched weight / issued weight (unanswered counts as wrong). The bands are
   `>= 0.75` T3 VERIFIED, `>= 0.50` T12 DISPUTED (admin decides, FR-064), and below that T4 REJECTED.
   A `verification_records` row stores the score, the threshold and the outcome.
5. **Admin adjudication** (T8/T9/T13) needs a non-empty justification (FR-065).
6. **Handover (T6, FR-104).** On a VERIFIED claim, the claimant confirms, and the finder (or an admin
   on the finder's behalf) confirms. An admin can never confirm the claimant side. When both have
   confirmed, the item closes with reason `HANDED_OVER`.
7. **Withdrawal by claimant (T11)** goes to REJECTED with `closure_cause = CLAIMANT_WITHDRAWN`. There is
   no refund.

The claimant never sees budget numbers, remaining questions or per-question scores (UI-010,
FR-089, NFR-USE-005). States are shown in plain language with the next action (NFR-USE-001).

## 6. Closure (FR-090: exactly three paths)

All three paths go through one function, `claims.close_item_in_tx(conn, item_id, reason, actor, winning_claim_id)`,
inside one transaction (FR-101, FR-133):

1. Refuse if any claim on the item is in MANUAL_REVIEW or DISPUTED (see decision D-9).
2. Guarded update to `resolved`, setting `resolved_at` and `closure_reason`. If rowcount is 0, return
   without changes and without an event (FR-098).
3. T6 on the winning claim, if any. T10 on every other PENDING/VERIFYING/VERIFIED claim (FR-062).
4. Freeze the item budget and every claim budget on the item: `frozen=1, frozen_value=consumed_total` (FR-097).
5. Append an `item_closed` event.

| Path | Who | Reason code |
|---|---|---|
| Handover | both confirmations (admin may stand in for the finder) | `HANDED_OVER` |
| Withdrawal | finder or admin, `POST /items/{id}/close` with reason + explicit confirm (UI-013) | `WITHDRAWN` |
| Expiry | scheduler on startup / admin "run sweep" | `EXPIRED` |

**Reopen** is admin only, with a justification (FR-113). The guarded update `WHERE status='resolved'`
sets the item back to active and restores the item budget to `consumed_total = frozen_value` (FR-116,
INV-6). Claims stay terminal.

**Expiry sweep.** First, VERIFYING claims whose last challenge is more than 7 days old move to T5 EXPIRED.
Then active items registered more than 30 days ago close as `EXPIRED`, unless a claim is in
VERIFYING, VERIFIED, MANUAL_REVIEW or DISPUTED (FR-112).

## 7. Search

- `search(conn, embedder, query, rerank, category, zone, date_lost)` loads active items only
  (filtered before scoring, FR-028, NFR-PERF-006), computes the baseline cosine for every one (FR-032)
  and returns `(item_id, baseline_score, final_score)` sorted by `(-final, item_id)`, so the order is
  deterministic (FR-044).
- Re-ranking (soft prior, OI-08) adds bonuses to the baseline: category match `+0.05`, zone match
  `+0.04`, date proximity `+0.03` when the item was found 0-14 days after the date lost. When it is
  off, `final == baseline` (FR-045).
- Low-confidence notice when the best baseline is below the embedder's viability threshold (FR-039).
- The home page shows public fields only, with rank (UI-004).

## 8. Evaluation (`evaluate.py`)

- **RQ1** `python evaluate.py rq1 [--queries CSV] [--corpus CSV --photos DIR]`. The queries CSV has columns
  `query_text, correct_item_id` plus optional owner metadata `category, zone, date_lost`. Three conditions:
  lexical (token Jaccard against the public description), semantic only, and semantic + re-rank.
  It reports R@1, R@5, R@10 and MRR, writes per-query ranks for every condition with
  better/worse/same flags (FR-046), and reports the description-gap token overlap (A-1).
  The default corpus is the seeded DB. An example CSV built from the seed data ships, and its numbers
  are illustrative only.
- **RQ2** `python evaluate.py rq2`. Simulated claimants who know 100/80/60/40/20/0 % of the attributes
  run through the real `claims.py` functions on an in-memory DB. Unknown attributes are guessed correctly
  with probability 0.1. Known answers get small typos. The fixed random seed is 42. It reports the pass /
  dispute / reject rate, the average budget spent, a score histogram, an ROC table (owners pooled
  20-100 % vs 0 % opportunists), a selected threshold (lowest with FPR <= 5 %) and the FRR per group
  at that threshold and at 0.75. Claims with `closure_cause = CLAIMANT_WITHDRAWN` are excluded from the
  fraud and FRR figures. A sequential-claimant simulation asserts INV-2 and that the item's total
  disclosure never exceeds its allowance.
- **Scale ablation** `python evaluate.py scale --distractors DIR`. Distractor images come from a local folder
  (no download) and are added to the RQ1 corpus at growing sizes. It reports R@k for both modes and
  per-query latency. A synthetic-vector run measures latency against N with a linear fit. The C-7
  domain-gap warning is printed wherever ablation results appear.

## 9. Decisions on open / [TBD] items

| ID | Decision |
|---|---|
| OI-01 | State identifiers fixed as in section 5 (the SRS names, unchanged). |
| **OI-03** | **Resolved: claimant withdrawal (T11) goes to `REJECTED` with `closure_cause = CLAIMANT_WITHDRAWN`, kept distinct from verification failures and excluded from the RQ2 fraud/FRR figures. The SRS should be updated to match.** |
| OI-07 | Additive bonuses: category 0.05, zone 0.04, date 0.03 (volatile). |
| OI-08 | Soft prior (bonus only, never a filter). |
| OI-09 | Finder assigns weight 1-5 per attribute. |
| OI-10 | Match threshold 0.8. |
| OI-11 | VERIFIED >= 0.75, DISPUTED 0.50-0.75, REJECTED < 0.50. To be recalibrated from the RQ2 ROC. |
| OI-12 | Claimant budget 10 per claim, item budget 20 cumulative. |
| OI-13 | Highest affordable weight first. |
| OI-14 | Items expire after 30 days. VERIFYING claims time out after 7 days. The sweep runs at startup and on admin request. |
| OI-18 | If embedding fails, registration is refused (no invisible item). |
| OI-19 | Session cookie (HttpOnly, SameSite=Lax), PBKDF2-SHA256 hashes. Not production grade. |
| OI-28 | Embedding is synchronous at registration. |
| Reason codes | `HANDED_OVER`, `WITHDRAWN`, `EXPIRED`. |
| FR-039 | Viability threshold: stub 0.15, CLIP 0.22 (volatile). |
| D-9 | SRS inconsistency: INV-1 forbids open claims on a resolved item, but T10 does not cover MANUAL_REVIEW/DISPUTED and only an admin may move them. Resolution: closure is refused while such a claim exists, and the admin must adjudicate first. |
| D-10 | One photo per item (meets "at least one", FR-011). At least one private attribute is required, otherwise verification is impossible. |
| D-11 | Seed placeholders are drawn as PNG (Pillow), not SVG, because CLIP cannot read SVG and the stub reads the PNG caption chunk. Uploads accept JPEG/PNG/WebP/GIF only (an SVG upload could carry script). |
| D-12 | Admins cannot open claims. A claimant may re-claim only if all their earlier claims on the item were cascaded by T10. |
| D-13 | `challenge_responses.response_text` is stored (SRS entity) but never rendered, because a matched response is close to the ground truth. |

## 10. Out of scope

Payments, notifications, custody tracking, mobile apps, production identity, CSRF tokens (SameSite=Lax
only), timing side channels (SRS §3.6 declares these gaps).
