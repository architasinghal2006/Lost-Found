# Lost & Found: retrieval and ownership verification

A solo software engineering project, built from `SRS SE FINAL_final.pdf` (IEEE 830).

- A **finder** registers a found item: a photo, public details, and private details that are never shown to anyone.
- An **owner** searches in their own words. The text is matched against the photo (CLIP, or a stub), then re-ranked
  on category, zone and date.
- A **claimant** proves ownership by answering questions generated from the private details. Every question costs
  disclosure budget, both per claim (10) and per item (20). The budgets are never refunded or reset.
- The item closes by **two-sided handover**, **finder withdrawal** or **scheduled expiry**. These are exactly the
  three closure paths the SRS allows.

The design is in [docs/spec.md](docs/spec.md) and the build plan in [docs/plan.md](docs/plan.md).
The live demo script is in [DEMO.md](DEMO.md).

## Setup on Windows

You need Python 3.11 or newer (developed on 3.13). In PowerShell, from this folder:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
```

## Running

```powershell
python seed.py
uvicorn main:app --host 0.0.0.0 --port 8000
```

`seed.py` creates `lostfound.db` with demo users, 13 items with drawn placeholder photos, and one item that is
40 days old. If a database already exists, it is renamed to `lostfound.db.bak` rather than deleted.

At startup the server creates the schema if needed and runs the expiry sweep, which expires the aged item. It
then prints the addresses to open:

```
  Lost & Found is running (stub embedder). Expired at startup: [13]
  On this laptop:  http://localhost:8000
  On your phone:   http://192.168.1.23:8000  (same Wi-Fi or hotspot)
```

**Phone access.** The first time uvicorn runs, Windows asks whether Python may use the network. Allow it on
**private** networks. If you missed the prompt, run this in an administrator PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Lost and Found 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

If your Wi-Fi network is marked *Public*, switch it to *Private* (Settings → Network → your network), or add
`-Profile Public` to the rule while you demo.

Demo accounts (all created by `seed.py`):

| Username | Password | Role in the demo |
|---|---|---|
| admin | admin123 | administrator |
| finder | finder123 | registered every seed item |
| owner | owner123 | genuine owner of the keys (item 4) |
| mallory | mallory123 | fraudster |
| trudy | trudy123 | second fraudster (sequential-claimant attack) |

## Tests

```powershell
python -m pytest -q
```

There are 357 tests. They need no network and no GPU (NFR-MNT-006). Notable ones:

| File | What it proves |
|---|---|
| `tests/test_architecture.py` | The 7 architecture rules: disjoint stores, single reader (grep), metered read (rollback + INV-2), one-way crossing, soft delete, guarded updates, append-only events. Also FR-047 and FR-090 |
| `tests/test_transitions.py` | SRS Appendix B families, parameterised over the §3.7 table: every legal transition succeeds, every undeclared one raises, every unauthorised actor is rejected |
| `tests/test_claims.py`, `tests/test_closure.py` | Budgets, verdict bands, T7, T11, handover, cascade, freeze, idempotency, atomicity under injected faults, reopen |
| `tests/test_web.py` | Walks every page as every role and asserts no private answer is ever rendered; admin routes return 403 for normal users |

## Switching on real CLIP

CLIP ViT-B/32 runs on the CPU through `open_clip`. **It has not been tested in this build environment**; see
"Not verified" below.

```powershell
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-clip.txt
python -c "import embedder; embedder.ClipEmbedder()"   # downloads the weights once (~350 MB), then they are cached
$env:LF_USE_CLIP = "1"
python seed.py                                          # re-embed everything with CLIP
uvicorn main:app --host 0.0.0.0 --port 8000             # same PowerShell window, so the variable is set
```

- **Always re-seed (or re-register) after switching embedders.** Stub vectors and CLIP vectors both have 512
  numbers, but they live in different spaces, so mixing them gives nonsense rankings.
- After the first download, CLIP loads from the local cache, so the app makes no outbound calls at runtime
  (SRS §3.1.4).
- The seed photos are simple drawings, so CLIP results on them mean little. Real results need real photos.

## Evaluation

```powershell
python evaluate.py rq1      # retrieval on the seeded DB with data/rq1_example.csv
python evaluate.py rq2      # verification simulation, about 10 seconds
python evaluate.py scale --distractors D:\path\to\abo_images   # scale ablation + latency
```

**RQ1** compares three conditions: lexical (token overlap with the public description), semantic only, and
semantic + re-rank. It reports R@1, R@5, R@10 and MRR, the description-gap overlap (A-1), and the per-query
rank changes, including where re-ranking made a rank worse. Per-query ranks for every condition are written
to `results/rq1_per_query.csv` (FR-046).

> **The shipped numbers are illustrative only.** `data/rq1_example.csv` has 19 queries over the 12 seed items, and
> the default embedder is the stub (a hashed bag of words). Real RQ1 numbers need your 60-object / 180-description
> set and `LF_USE_CLIP=1`.

To evaluate your real set without putting it in the app, run:

```powershell
$env:LF_USE_CLIP = "1"
python evaluate.py rq1 --corpus my_items.csv --photos D:\photos --queries my_queries.csv
```

- `my_items.csv` columns: `item_id, photo, public_description, category, zone, date_found`.
- `my_queries.csv` columns: `query_text, correct_item_id`, plus optional owner metadata `category, zone, date_lost`
  (these feed the re-ranking).
- Category and zone values should come from the lists in `items.py`.

**RQ2** simulates claimants who know 100/80/60/40/20/0 % of an item's private details. They run through the
real `claims.py` functions (same budgets, scoring and thresholds) on an in-memory database. Random seed is 42,
200 trials per group.
- Unknown answers are guessed correctly 10 % of the time.
- Known answers get a one-character typo 30 % of the time.

It prints:
- pass / review / reject rates and the average budget spent per group;
- score histograms;
- an ROC table (owners pooled against 0 % opportunists);
- the lowest threshold with a false-accept rate of 5 % or less, and the false-rejection rate per group at that
  threshold and at 0.75;
- a sequential-claimant attack that asserts INV-2.

Claims withdrawn by the claimant (`CLAIMANT_WITHDRAWN`) are excluded from these figures.

**Scale ablation** reads distractor images from a local folder; nothing is downloaded. It adds them to the
corpus in steps up to 2,500 and reports retrieval quality and search time. A synthetic run also measures
brute-force latency against corpus size with a linear fit. Embeddings are cached in `results/`. The **C-7
domain-gap warning** (studio catalogue vs phone photos) is printed wherever ablation results appear.

## Docker

```powershell
docker compose up --build
```

- The database and uploads live in the `lf-instance` volume, and the container seeds only on its first start.
- Inside Docker the printed "phone" address is the container's own; use your laptop's address from `ipconfig`
  instead.
- For CLIP, set `WITH_CLIP: "1"` and `LF_USE_CLIP: "1"` in `docker-compose.yml`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LF_DB` | `lostfound.db` | SQLite file |
| `LF_UPLOADS` | `static/uploads` | where photos are saved |
| `LF_USE_CLIP` | unset | `1` = real CLIP, otherwise the stub |
| `LF_PORT` | `8000` | only used for the printed addresses |

Volatile SRS values are constants at the top of `claims.py` (budgets, claim timeout), `scoring.py` (match and
verdict thresholds), `search.py` (re-ranking weights) and `items.py` (expiry age, categories, zones).

## Code map

| File | Responsibility |
|---|---|
| `db.py` | Schema, triggers (append-only events, no item deletes, budgets never go down), `transaction()` with `BEGIN IMMEDIATE` |
| `auth.py` | PBKDF2 password hashes, sessions, roles `user` / `admin` |
| `events.py` | Append-only event log; timeline filtered by viewer (public / claimant / staff) |
| `items.py` | Registration and validation, finder withdrawal, expiry sweep |
| `embedder.py` | Stub and CLIP embedders behind `embed_text` / `embed_image` |
| `search.py` | Brute-force cosine + metadata re-ranking (on/off per query) |
| `transitions.py` | The SRS §3.7 transition table as one dictionary |
| `scoring.py` | Answer similarity (difflib) and verdict bands |
| `claims.py` | Claims, metered challenges, budgets, handover, closure, reopen. **The only reader of `private_attributes`** |
| `claim_views.py` | Read-only queries for pages (never touches `private_attributes`) |
| `ratelimit.py` | Per-user sliding-window limit on claim and answer requests |
| `main.py` | Routes and startup |
| `seed.py`, `evaluate.py` | Demo data; RQ1 / RQ2 / scale |

## SRS decisions made during implementation

All are recorded in [docs/spec.md §9](docs/spec.md). The ones that change the SRS text:

- **OI-03 resolved:** claimant withdrawal (T11) goes to `REJECTED` with `closure_cause = CLAIMANT_WITHDRAWN`. It is
  kept distinct from verification failures and excluded from the RQ2 fraud/FRR figures. *Update the SRS §3.7 and
  Appendix A to match.*
- **OI-01:** the state names are now fixed in code exactly as in the SRS table, so the [TBC] can be removed.
- **D-9 (SRS inconsistency):** INV-1 forbids open claims on a resolved item, but T10 does not cover
  `MANUAL_REVIEW` / `DISPUTED`, and only an admin may move those. The implementation therefore refuses closure
  while such a claim exists, so the admin decides first.
- **The 0.50–0.75 verdict band goes to `DISPUTED` (T12).** `MANUAL_REVIEW` is reached only through T7, when the
  item budget is exhausted mid-claim. This follows the table rather than the original prompt.
- **Locking (FR-058):** SQLite has no row locks. `BEGIN IMMEDIATE` serialises every writer, which is stronger.
- **Seed photos are PNG, not SVG.** CLIP cannot read SVG, and accepting SVG uploads would allow script injection.

## Known limitations

- Authentication is prototype-grade: PBKDF2, an HttpOnly + SameSite=Lax cookie, and no CSRF tokens. The SRS
  declares this out of scope (§3.6, OI-19).
- The rate limiter is in memory and resets on restart, which is acceptable for a single process (C-2).
- One photo per item.
- Timing side channels are not addressed (declared in SRS §3.6).
